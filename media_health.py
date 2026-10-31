#!/usr/bin/env python3
"""media-health: scan a media library for damaged, broken or mislabelled files.

Per file:
  probe   ffprobe must parse the container and find a video stream
  zeros   1 MiB samples at evenly spaced offsets must not be all zero bytes
  decode  a few seconds decoded at several points: ffmpeg errors + decoder peak RSS
  full    (--full) decode the whole video stream instead of samples
  gpu     (--gpu) NVDEC for the decode points after the start: ~8x faster on 4K HEVC
  name    what the filename claims (2160p, UHD BluRay, HDR, DV, 10bit, x265, REMUX,
          TrueHD, DTS...) must match the actual streams
  profile codec / HDR / Dolby Vision / audio, reported as info

Results live in an SQLite database (<reports>/media-health.db). A file is skipped when
its path, size and mtime are unchanged and it was already checked at least as deeply;
a renamed/moved/hardlinked copy of an already-checked file is recognised by its
fingerprint and the result reused. --force rescans everything.

Run without arguments on a terminal for an interactive menu. During a scan a terminal
shows a live full-screen dashboard; without one (cron, docker logs) it prints a status
line every 30 s plus problem files only.
"""
import argparse, csv, datetime as dt, fcntl, fnmatch, hashlib, json, os, re, shlex, signal, sqlite3
import subprocess, sys, threading, time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

VERSION = "3.3.0"
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}
CHECK_VERSION = 2   # bump to invalidate cached results when the probe/zero/decode checks change
QUICK_SINCE = 1     # oldest CHECK_VERSION whose quick results are still valid (quick checks unchanged since)
FLOOD = 100         # serious errors from one decode point before the decoder is stopped early
NAME_VERSION = 1    # bump when name-check rules change: re-evaluated from stored data, no rescan
RANK = ["OK", "WARN", "BAD"]
DEPTH = {"quick": 0, "sample": 1, "full": 2}
SKIP_DIRS = ("Screens", "Extras", "Featurettes", "Samples", "Sample")

# Decoder chatter that happens on healthy files right after a seek
# (open-GOP leading frames, missing refs before the first keyframe).
BENIGN = re.compile(
    r"Could not find ref with POC|Skipping invalid undecodable NALU|"
    r"Invalid NAL unit size \(0 > |co located POCs unavailable|"
    r"no frame!|reference picture missing during reorder|Missing reference picture|"
    r"mmco: unref short failure|number of reference frames .* exceeds max|"
    r"Last message repeated|"
    # --gpu: ffmpeg falls back to software for a codec the GPU can't decode
    r"Failed setup for format cuda|hwaccel initialisation returned error|"
    r"doesn't support hardware accelerated",
    re.I)
SERIOUS = re.compile(
    r"VPS \d+ does not exist|SPS \d+ does not exist|PPS id out of range|"
    r"Invalid data found|EBML header parsing failed|corrupt|concealing|"
    r"error while decoding|decode_slice_header error|Error parsing NAL unit|"
    r"moov atom not found|Truncated|invalid as first byte|Packet corrupt|"
    # damaged slice data (CABAC residuals): only the software decoder reports these
    r"is outside the valid range|CABAC_MAX_BIN|cabac decode of qscale diff failed|"
    r"ac-tex damaged|out of range intra chroma pred mode|negative number of zero coeffs|"
    r"exceeds containing master element",
    re.I)


def worse(a, b):
    return max(a, b, key=RANK.index)


# ---------------------------------------------------------------- live worker status

ACTIVE = {}             # thread id -> {"path", "size", "start", "stage", "info"}
ACTIVE_LOCK = threading.Lock()


def set_stage(stage=None, **kw):
    with ACTIVE_LOCK:
        a = ACTIVE.get(threading.get_ident())
        if a is not None:
            if stage:
                a["stage"] = stage
            a.update(kw)


# ---------------------------------------------------------------- checks

def run(cmd, timeout, abort=None):
    """Run cmd, return (rc, stdout, stderr, peak_rss_mb, timed_out, aborted).
    abort(line) -> True kills it early; stderr is then read line by line and stdout discarded."""
    # Own session: Ctrl+C on the terminal reaches only the scanner, which then lets the
    # running checks finish. Otherwise the children die and good files get recorded as BAD.
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL if abort else subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, errors="replace", preexec_fn=lambda: os.nice(10), start_new_session=True)
    timed_out = aborted = False
    if abort is None:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            p.kill(); out, err = p.communicate()
        return p.returncode, out, err, getattr(p, "_mh_rss", None), timed_out, aborted

    def on_timeout():
        nonlocal timed_out
        timed_out = True
        p.kill()
    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    lines = []
    try:
        for line in p.stderr:
            lines.append(line)
            if not aborted and abort(line):
                aborted = True
                p.kill()
        p.wait()
    finally:
        timer.cancel()
    return p.returncode, "", "".join(lines), getattr(p, "_mh_rss", None), timed_out, aborted


# Capture per-child rusage: patch Popen._try_wait to use os.wait4.
def _try_wait(self, wait_flags):
    try:
        pid, sts, ru = os.wait4(self.pid, wait_flags)
    except ChildProcessError:
        return self.pid, 0
    if pid == self.pid:
        self._mh_rss = ru.ru_maxrss // 1024
    return pid, sts
subprocess.Popen._try_wait = _try_wait


def probe(path):
    rc, out, err, _, timed_out, _ = run(["ffprobe", "-v", "error", "-print_format", "json",
                                      "-show_format", "-show_streams", path], 120)
    if rc != 0 or not out.strip():
        why = err.strip().splitlines()[-3:]
        if timed_out:
            why.append("ffprobe timed out after 120s")
        elif not why:
            why = [f"ffprobe exit {rc}" + (f" (signal {-rc})" if rc < 0 else "")]
        return None, why
    return json.loads(out), err.strip().splitlines()[:3]


def profile(info):
    fmt = info.get("format", {})
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")), None)
    a = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    p = {"duration": float(fmt.get("duration") or v and v.get("duration") or 0),
         "bitrate_mbps": round(int(fmt.get("bit_rate") or 0) / 1e6, 1)}
    if v:
        trc = v.get("color_transfer", "")
        dv = next((sd for sd in v.get("side_data_list", []) if "DOVI" in sd.get("side_data_type", "")), None)
        p.update({
            "vcodec": v.get("codec_name", ""), "vprofile": v.get("profile", ""),
            "res": f'{v.get("width")}x{v.get("height")}', "pix_fmt": v.get("pix_fmt", ""),
            "hdr": "HDR10" if trc == "smpte2084" else "HLG" if trc == "arib-std-b67" else "",
            "dv_profile": dv.get("dv_profile") if dv else "",
            "dv_compat": dv.get("dv_bl_signal_compatibility_id") if dv else "",
            "interlaced": v.get("field_order", "progressive") not in ("progressive", "unknown", ""),
        })
    # codec:profile per track, e.g. "truehd:,dts:DTS-HD MA,eac3:Dolby Digital Plus + Dolby Atmos"
    p["audio"] = ",".join(sorted({f"{s.get('codec_name', '')}:{s.get('profile', '')}" for s in a}))
    return p, v is not None


def zero_scan(path, size, samples):
    """Return list of fractional offsets whose 1 MiB sample is all zeros."""
    bad, chunk = [], 1 << 20
    if size < chunk * 2:
        return bad
    offs = [0] + [int(size * i / (samples + 1)) for i in range(1, samples + 1)] + [size - chunk]
    with open(path, "rb") as f:
        for o in offs:
            f.seek(o)
            b = f.read(chunk)
            if b and not b.strip(b"\0"):
                bad.append(round(o / size, 3))
    return bad


def fingerprint(path, size):
    """Content identity without reading the whole file: size + first/middle/last 1 MiB."""
    h = hashlib.blake2b(str(size).encode(), digest_size=16)
    chunk = 1 << 20
    with open(path, "rb") as f:
        for o in sorted({0, max(size // 2 - chunk // 2, 0), max(size - chunk, 0)}):
            f.seek(o)
            h.update(f.read(chunk))
    return h.hexdigest()


def decode(path, start, seconds, timeout, gpu=False):
    """Decode the video stream (from start, for seconds; whole file if both None).
    gpu = NVDEC. Faster, but it can't see damaged slice data: only headers/containers."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error"]
    cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] if gpu else ["-threads", "2"]
    if start is not None:
        # start at the keyframe before `start` instead of decoding up to it and discarding:
        # same amount of checked video, a lot less work on long-GOP 4K
        cmd += ["-noaccurate_seek", "-ss", f"{start:.1f}"]
    cmd += ["-i", path, "-map", "0:v:0", "-an", "-sn", "-dn"]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-f", "null", "-"]
    n = [0]

    def flood(line):                # a stream without headers errors on every packet until EOF
        n[0] += bool(SERIOUS.search(line))
        return n[0] >= FLOOD
    rc, _, err, rss, timed_out, flooded = run(cmd, timeout, abort=flood)
    with ACTIVE_LOCK:
        DECODES["gpu" if gpu else "cpu"] += 1
    lines = [l for l in err.splitlines() if l.strip()]
    serious = [l for l in lines if SERIOUS.search(l)]
    other = [l for l in lines if not BENIGN.search(l) and not SERIOUS.search(l)]
    return rc, serious, other, rss or 0, timed_out, flooded


_GPU = {}
DECODES = {"cpu": 0, "gpu": 0}  # decode points run this scan, for the dashboard


def gpu_available():
    """True if ffmpeg can open a CUDA device (NVIDIA runtime + driver present). Cached."""
    if "ok" not in _GPU:
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-init_hw_device", "cuda", "-f", "lavfi", "-i",
                                "nullsrc=s=64x64:d=0.1", "-f", "null", "-"], capture_output=True, timeout=30)
            _GPU["ok"] = p.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _GPU["ok"] = False
    return _GPU["ok"]


def decoder_label(args):
    """What decodes this scan: none (quick), cpu, cpu (no GPU), cpu+gpu or gpu."""
    if args.quick:
        return "none"
    pts = gpu_points(args)
    if pts == "none" and args.gpu != "off" and not gpu_available():
        return "cpu (no GPU)"
    return {"none": "cpu", "after-start": "cpu+gpu", "all": "gpu"}[pts]


def gpu_points(args):
    """Which decode points use NVDEC: 'none', 'after-start' or 'all'.
    auto: sampled scans decode the start on the CPU (catches damaged slice data and measures
    decoder memory) and the other points on the GPU; --full stays on the CPU. on: everything on the GPU."""
    if args.quick or args.gpu == "off" or not gpu_available():
        return "none"
    if args.gpu == "on":
        return "all"
    return "none" if args.full else "after-start"


# ---------------------------------------------------------------- name check

def _tok(pattern):
    # tokens are delimited by . _ - space ( ) [ ] or string ends
    return re.compile(r"(?<![a-z0-9])(?:" + pattern + r")(?![a-z0-9])", re.I)

NAME_RES = [(2160, _tok(r"2160[pi]|4k|uhd")), (1080, _tok(r"1080[pi]")), (720, _tok(r"720p")),
            (576, _tok(r"576[pi]")), (480, _tok(r"480[pi]"))]
NAME_UHD_BD = re.compile(r"(?<![a-z0-9])uhd[ ._-]?blu-?ray|blu-?ray[ ._-]?uhd(?![a-z0-9])", re.I)
NAME_HDR = _tok(r"hdr10\+?|hdr10plus|hdr")
NAME_HLG = _tok(r"hlg")
NAME_DV = _tok(r"dv|dovi|dolby[ ._-]?vision")
NAME_10BIT = _tok(r"10[ ._-]?bit|hi10p?")
NAME_REMUX = _tok(r"remux")
NAME_VCODEC = [("hevc", _tok(r"x265|hevc|h[ ._]?265")), ("h264", _tok(r"x264|avc|h[ ._]?264")),
               ("av1", _tok(r"av1")), ("vc1", _tok(r"vc[ ._-]?1")),
               ("mpeg2video", _tok(r"mpeg[ ._-]?2")), ("mpeg4", _tok(r"xvid|divx"))]
NAME_ENCODER = _tok(r"x264|x265")
_CH = r"(?:[ ._]?[1-9][ ._][01])?"   # optional channel layout glued on: DDP5.1, AAC2.0, TrueHD.7.1
NAME_AUDIO = [("truehd", _tok(r"truehd" + _CH)), ("atmos", _tok(r"atmos")), ("dtsx", _tok(r"dts[ ._:-]?x" + _CH)),
              ("dts", _tok(r"(?:dts(?:[ ._-]?hd)?(?:[ ._-]?ma)?|dts[ ._-]?es)" + _CH)),
              ("eac3", _tok(r"(?:ddp|dd\+|eac3|e[ ._-]?ac[ ._-]?3)" + _CH)),
              ("ac3", _tok(r"(?:dd|ac3)" + _CH)),
              ("aac", _tok(r"aac" + _CH)), ("flac", _tok(r"flac" + _CH)), ("pcm", _tok(r"l?pcm" + _CH))]


def audio_families(audio):
    """Stored audio field -> set of codec families. Handles the old format (profile or
    codec name per track) and the new one (codec:profile per track)."""
    fam = set()
    for track in filter(None, (audio or "").lower().split(",")):
        t = track.replace(" ", "")
        if "truehd" in t: fam.add("truehd")
        if "dts" in t: fam.add("dts")
        if "dts:x" in t or "dts-x" in t: fam.add("dtsx")
        if "eac3" in t or "dolbydigitalplus" in t: fam.add("eac3")
        elif "ac3" in t: fam.add("ac3")
        if "atmos" in t: fam.add("atmos")
        if "aac" in t or t in ("lc", "he-aac", "he-aacv2", "lc:"): fam.add("aac")
        if "flac" in t: fam.add("flac")
        if "pcm" in t: fam.add("pcm")
    return fam


def name_issues(path, r):
    """Compare what the file name claims with the probed streams. Returns a list of strings."""
    name = os.path.splitext(os.path.basename(path))[0]
    out = []
    try:
        w, h = (int(x) for x in (r.get("res") or "0x0").split("x"))
    except ValueError:
        w = h = 0
    pix = r.get("pix_fmt") or ""
    ten_bit = bool(re.search(r"10|12", pix))
    hdr, dv = r.get("hdr") or "", r.get("dv_profile") not in ("", None)
    vcodec = r.get("vcodec") or ""
    if not w:
        return out

    claimed = next((res for res, rx in NAME_RES if rx.search(name)), None)
    if claimed == 2160 and not (w >= 3200 or h >= 1700):
        out.append(f"says 2160p/4K but video is {w}x{h}")
    elif claimed == 1080 and not (1700 <= w < 3200 or 1000 <= h < 1700):
        out.append(f"says 1080p but video is {w}x{h}")
    elif claimed == 720 and not (1200 <= w < 1700 or 700 <= h < 1000):
        out.append(f"says 720p but video is {w}x{h}")
    elif claimed in (576, 480) and w >= 1100:
        out.append(f"says {claimed}p but video is {w}x{h}")

    if NAME_UHD_BD.search(name) and not ten_bit:
        out.append(f"says UHD BluRay but video is 8-bit ({pix}{', SDR' if not hdr and not dv else ''}) - not a real UHD disc source")
    if NAME_HDR.search(name) and not (hdr == "HDR10" or dv):
        out.append(f"says HDR but video is {'HLG' if hdr == 'HLG' else 'SDR'}")
    if NAME_HLG.search(name) and hdr != "HLG":
        out.append("says HLG but video has no HLG transfer")
    if NAME_DV.search(name) and not dv:
        out.append("says Dolby Vision but there is no DV metadata")
    if NAME_10BIT.search(name) and not ten_bit:
        out.append(f"says 10bit but video is {pix}")

    claimed_codec = next((c for c, rx in NAME_VCODEC if rx.search(name)), None)
    if claimed_codec and vcodec and claimed_codec != vcodec:
        out.append(f"says {claimed_codec} but video is {vcodec}")

    if NAME_REMUX.search(name):
        if NAME_ENCODER.search(name):
            out.append("says REMUX but is also tagged x264/x265 (a re-encode)")
        br = r.get("bitrate_mbps") or 0
        floor = 25 if (w >= 3200 or h >= 1700) else 12 if (w >= 1700 or h >= 1000) else 0
        if floor and br and br < floor:
            out.append(f"says REMUX but bitrate is only {br} Mbps (expected > {floor})")

    fam = audio_families(r.get("audio"))
    if fam:
        for f, rx in NAME_AUDIO:
            if not rx.search(name):
                continue
            if f == "atmos":
                ok = bool(fam & {"truehd", "eac3", "atmos"})       # Atmos rides on TrueHD or DD+
            elif f == "dtsx":
                ok = "dts" in fam                                  # DTS:X flag isn't always exposed
            elif f == "ac3":
                ok = bool(fam & {"ac3", "eac3"})
            else:
                ok = f in fam
            if not ok:
                out.append(f"says {f.upper()} audio but tracks are {', '.join(sorted(fam)) or 'none'}")
    return out


def apply_name_check(r, mislabel):
    """Fold name issues into the result's status. Base (check) status/problems stay separate."""
    issues = name_issues(r["path"], r) if mislabel != "off" else []
    r["name_issues"] = issues
    r["name_version"] = NAME_VERSION
    status = r.get("base_status") or r.get("status") or "OK"
    if issues and mislabel in ("warn", "bad"):
        status = worse(status, "WARN" if mislabel == "warn" else "BAD")
    r["status"] = status
    return r


CODEC_NAME = {"hevc": "HEVC", "h264": "H.264", "av1": "AV1", "vc1": "VC-1", "mpeg2video": "MPEG2", "mpeg4": "XviD"}
# the main audio track = the best kind present; secondary tracks never go in the name
AUDIO_RANK = ["truehd", "dts", "flac", "pcm", "eac3", "ac3", "aac"]
AUDIO_NAME = {"truehd": "TrueHD", "dts": "DTS", "flac": "FLAC", "pcm": "LPCM", "eac3": "DDP", "ac3": "DD", "aac": "AAC"}
_CH_TAIL = re.compile(r"[ ._]?[1-9][ ._][01]$")


def _cut(name, m):
    """Remove a matched token and one separator next to it."""
    a, b = m.span()
    if a > 0 and name[a - 1] in " ._-":
        a -= 1
    elif b < len(name) and name[b] in " ._-":
        b += 1
    return name[:a] + name[b:]


def _replace(name, rx, new):
    """Replace the first match of rx with new (None = drop it) and drop any later matches."""
    ms = list(rx.finditer(name))
    for m in reversed(ms[1:]):
        name = _cut(name, m)
    if not ms:
        return name
    return _cut(name, ms[0]) if new is None else name[:ms[0].start()] + new + name[ms[0].end():]


def suggest_name(path, r):
    """The file name with its wrong claims corrected, or None when there's nothing to fix.
    Claims the streams don't back up are dropped (HDR, HLG, DV, 10bit, UHD on an 8-bit source, REMUX);
    a wrong resolution, video codec or main audio format is replaced with the real one."""
    if not name_issues(path, r):
        return None
    name, ext = os.path.splitext(os.path.basename(path))
    orig = name
    try:
        w, h = (int(x) for x in (r.get("res") or "0x0").split("x"))
    except ValueError:
        return None
    ten_bit = bool(re.search(r"10|12", r.get("pix_fmt") or ""))
    hdr, dv = r.get("hdr") or "", r.get("dv_profile") not in ("", None)
    vcodec = r.get("vcodec") or ""
    sep = "." if name.count(".") > name.count(" ") else " "

    claimed = next(((res, rx) for res, rx in NAME_RES if rx.search(name)), None)
    real = 2160 if w >= 3200 or h >= 1700 else 1080 if w >= 1700 or h >= 1000 else 720 if w >= 1200 or h >= 700 \
        else 576 if h >= 560 else 480
    if claimed and claimed[0] != real and any(i.startswith(f"says {claimed[0]}p") for i in name_issues(path, r)):
        name = _replace(name, claimed[1], f"{real}p")
    if NAME_UHD_BD.search(name) and not ten_bit:
        name = _replace(name, _tok("uhd"), None)
    if NAME_HDR.search(name) and not (hdr == "HDR10" or dv):
        name = _replace(name, NAME_HDR, "HLG" if hdr == "HLG" and not NAME_HLG.search(name) else None)
    if NAME_HLG.search(name) and hdr != "HLG":
        name = _replace(name, NAME_HLG, "HDR" if hdr == "HDR10" and not NAME_HDR.search(name) else None)
    if NAME_DV.search(name) and not dv:
        name = _replace(name, NAME_DV, None)
    if NAME_10BIT.search(name) and not ten_bit:
        name = _replace(name, NAME_10BIT, None)
    said = next(((c, rx) for c, rx in NAME_VCODEC if rx.search(name)), None)
    if said and vcodec and said[0] != vcodec:
        name = _replace(name, said[1], CODEC_NAME.get(vcodec, vcodec.upper()))
    if NAME_REMUX.search(name):
        br = r.get("bitrate_mbps") or 0
        floor = 25 if real == 2160 else 12 if real == 1080 else 0
        if NAME_ENCODER.search(name) or (floor and br and br < floor):
            name = _replace(name, NAME_REMUX, None)

    fam = audio_families(r.get("audio"))
    main = next((f for f in AUDIO_RANK if f in fam), None)
    if main:
        audio = (r.get("audio") or "").lower()
        new = AUDIO_NAME[main]
        if main == "dts" and "dts-hd ma" in audio:
            new = f"DTS-HD{sep}MA"
        elif main == "dts" and "dts:x" in audio.replace(" ", ""):
            new = "DTS-X"
        placed = any(rx.search(name) for f, rx in NAME_AUDIO if f == main)
        for f, rx in NAME_AUDIO:
            m = rx.search(name)
            if not m:
                continue
            ok = (bool(fam & {"truehd", "eac3", "atmos"}) if f == "atmos" else "dts" in fam if f == "dtsx"
                  else bool(fam & {"ac3", "eac3"}) if f == "ac3" else f in fam)
            if ok:
                continue
            if f == "atmos" or placed:
                name = _replace(name, rx, None)
            else:
                ch = _CH_TAIL.search(m.group(0))
                name = name[:m.start()] + new + (ch.group(0) if ch else "") + name[m.end():]
                placed = True
    name = name.strip(" ._-")
    return name + ext if name != orig else None


def check(path, args):
    st = os.stat(path)
    r = {"path": path, "size_gb": round(st.st_size / 1e9, 2), "base_status": "OK",
         "problems": [], "notes": [], "peak_rss_mb": 0}
    t0 = time.time()
    set_stage("probe")
    info, perr = probe(path)
    if info is None:
        r["base_status"] = "BAD"; r["problems"].append("probe failed: " + " | ".join(perr))
        set_stage("zero-check")
        zeros = zero_scan(path, st.st_size, args.zero_samples)
        if zeros:
            r["problems"].append(f"zeroed at {zeros}")
        r["secs"] = round(time.time() - t0, 1)
        return r
    p, has_video = profile(info)
    r.update(p)
    set_stage(info=" ".join(x for x in (p.get("res"), p.get("vcodec"), p.get("hdr"),
                                        f"DV{p['dv_profile']}" if p.get("dv_profile") else "") if x))
    if not has_video:
        r["base_status"] = "BAD"; r["problems"].append("no video stream")
    if perr:
        r["notes"].append("probe: " + " | ".join(perr))

    set_stage("zero-check")
    zeros = zero_scan(path, st.st_size, args.zero_samples)
    if zeros:
        r["base_status"] = "BAD"; r["problems"].append(f"zeroed 1MiB blocks at {zeros}")

    dur = p.get("duration") or 0
    if has_video and not args.quick:
        if args.full:
            points = [None]
        else:
            n = args.decode_samples
            points = [None] + ([dur * i / (n + 1) for i in range(1, n + 1)] if dur > 60 else [])
        serious_n, other_n, sample_errs = 0, 0, []
        on_gpu = gpu_points(args)
        for i, pt in enumerate(points, 1):
            where = "full" if args.full else ("start" if pt is None else f"{int(pt // 60)}m")
            gpu = on_gpu == "all" or (on_gpu == "after-start" and i > 1)
            set_stage(f"decode {i}/{len(points)} @ {where}" + (" gpu" if gpu else ""))
            rc, serious, other, rss, timed_out, flooded = decode(
                path, pt, None if args.full else args.decode_seconds,
                args.timeout_full if args.full else args.timeout, gpu)
            if not gpu:             # an NVDEC process's RSS is mostly the CUDA context, not the decoder
                r["peak_rss_mb"] = max(r["peak_rss_mb"], rss)
            if timed_out or flooded:
                why = "timed out" if timed_out else f"stopped after {FLOOD} errors"
                r["base_status"] = "BAD"; r["problems"].append(f"decoder {why} at {where} ({rss} MB) - stream won't decode")
                serious_n += len(serious)
                sample_errs.append(f"{where}: {(serious or ['timeout'])[0][:160]}")
                break  # already BAD; the other points would only fail the same way
            elif rc < 0 or rc == 137:
                r["base_status"] = "BAD"; r["problems"].append(f"decoder killed at {where} (rc {rc}, {rss} MB) - out of memory")
            elif rc != 0 and not serious:
                serious = [f"ffmpeg exit {rc}"] + other[-2:]
            if serious:
                serious_n += len(serious)
                sample_errs.append(f"{where}: {serious[0][:160]}")
            other_n += len(other)
        if serious_n:
            r["base_status"] = "BAD" if serious_n >= args.bad_threshold else worse(r["base_status"], "WARN")
            r["problems"].append(f"{serious_n} decode errors ({'; '.join(sample_errs[:3])})")
        if other_n:
            r["notes"].append(f"{other_n} minor decoder messages")
        if on_gpu == "all" or (on_gpu == "after-start" and len(points) > 1):
            r["notes"].append("decoded on GPU (NVDEC)" + (" after the start" if on_gpu == "after-start" else ""))
        if r["peak_rss_mb"] > args.rss_warn:
            r["base_status"] = worse(r["base_status"], "WARN")
            r["problems"].append(f"decoder peak memory {r['peak_rss_mb']} MB")

    # informational: things that make Plex transcode or break on some clients
    if p.get("dv_profile") in (5, 7) or (p.get("dv_profile") and p.get("dv_compat") in (0, None)):
        r["notes"].append(f"Dolby Vision profile {p['dv_profile']} (no HDR10 fallback for non-DV clients -> tone-map transcode)")
    if p.get("interlaced"):
        r["notes"].append("interlaced")
    if p.get("vcodec") in ("mpeg4", "msmpeg4v3", "wmv3", "vc1", "mpeg2video"):
        r["notes"].append(f"legacy codec {p['vcodec']}")
    r["secs"] = round(time.time() - t0, 1)
    return r


# ---------------------------------------------------------------- database

COLS = ["path", "size", "mtime", "fingerprint", "mode", "check_version", "name_version",
        "status", "base_status", "problems", "name_issues", "notes",
        "vcodec", "vprofile", "res", "pix_fmt", "hdr", "dv_profile", "audio", "bitrate_mbps",
        "duration", "peak_rss_mb", "secs", "reused_from", "checked", "first_seen"]
JSON_COLS = ("problems", "name_issues", "notes")


def current(row):
    """Was this stored result made by the current checks?"""
    v = row["check_version"] or 0
    return v == CHECK_VERSION or (row["mode"] == "quick" and QUICK_SINCE <= v <= CHECK_VERSION)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, fingerprint TEXT, mode TEXT,
  check_version INTEGER, name_version INTEGER, status TEXT, base_status TEXT,
  problems TEXT, name_issues TEXT, notes TEXT, vcodec TEXT, vprofile TEXT, res TEXT,
  pix_fmt TEXT, hdr TEXT, dv_profile TEXT, audio TEXT, bitrate_mbps REAL, duration REAL,
  peak_rss_mb INTEGER, secs REAL, reused_from TEXT, checked TEXT, first_seen TEXT);
CREATE INDEX IF NOT EXISTS files_fp ON files(fingerprint);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started TEXT, finished TEXT, mode TEXT, command TEXT,
  found INTEGER, queued INTEGER, checked INTEGER, ok INTEGER, warn INTEGER, bad INTEGER,
  mislabelled INTEGER, cached INTEGER, reused INTEGER, filtered INTEGER, pruned INTEGER,
  stopped INTEGER);
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY, label TEXT, cron TEXT, args TEXT, enabled INTEGER DEFAULT 1,
  run_now INTEGER DEFAULT 0, created TEXT, last_start TEXT, last_end TEXT, last_exit INTEGER,
  last_log TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    """SQLite store. One connection per thread; only the main thread writes."""

    def __init__(self, path):
        self.path = path
        self.local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()

    def conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=60)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self.local.c = c
        return c

    @staticmethod
    def to_result(row):
        r = dict(row)
        for k in JSON_COLS:
            try:
                r[k] = json.loads(r.get(k) or "[]")
            except ValueError:
                r[k] = []
        r["size_gb"] = round((r.get("size") or 0) / 1e9, 2)
        return r

    def get(self, path):
        row = self.conn().execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return self.to_result(row) if row else None

    def by_fingerprint(self, fp, size, mode):
        rows = self.conn().execute("SELECT * FROM files WHERE fingerprint=? AND size=?", (fp, size)).fetchall()
        rows = [r for r in rows if current(r) and DEPTH.get(r["mode"], -1) >= DEPTH[mode]]
        return self.to_result(max(rows, key=lambda r: DEPTH[r["mode"]])) if rows else None

    def save(self, r):
        r = dict(r)
        r["size"] = r.get("size") if r.get("size") is not None else int((r.get("size_gb") or 0) * 1e9)
        for k in JSON_COLS:
            r[k] = json.dumps(r.get(k) or [])
        r["dv_profile"] = "" if r.get("dv_profile") in (None, "") else str(r["dv_profile"])
        r["first_seen"] = r.get("first_seen") or r.get("checked")
        vals = [r.get(k) for k in COLS]
        self.conn().execute(
            f"INSERT INTO files ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))}) "
            f"ON CONFLICT(path) DO UPDATE SET " + ",".join(f"{k}=excluded.{k}" for k in COLS if k not in ("path", "first_seen")),
            vals)

    def commit(self):
        self.conn().commit()

    def prune(self, root, seen):
        root = root.rstrip("/")
        c = self.conn()
        gone = [r["path"] for r in c.execute("SELECT path FROM files WHERE path LIKE ? ESCAPE '\\'",
                                            (root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%",))
                if r["path"] not in seen]
        c.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
        return len(gone)

    def rows(self, where="1=1", params=(), order="path", limit=None, offset=0):
        q = f"SELECT * FROM files WHERE {where} ORDER BY {order}"
        if limit:
            q += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        return [self.to_result(r) for r in self.conn().execute(q, params)]

    def count(self, where="1=1", params=()):
        return self.conn().execute(f"SELECT COUNT(*) FROM files WHERE {where}", params).fetchone()[0]

    def run_start(self, mode, command):
        cur = self.conn().execute("INSERT INTO runs (started, mode, command) VALUES (?,?,?)",
                                  (dt.datetime.now().isoformat(timespec="seconds"), mode, command))
        self.commit()
        return cur.lastrowid

    def run_end(self, rid, s, pruned):
        self.conn().execute(
            "UPDATE runs SET finished=?, found=?, queued=?, checked=?, ok=?, warn=?, bad=?, mislabelled=?, "
            "cached=?, reused=?, filtered=?, pruned=?, stopped=? WHERE id=?",
            (dt.datetime.now().isoformat(timespec="seconds"), s.found, s.queued, s.done, s.counts["OK"],
             s.counts["WARN"], s.counts["BAD"], s.mislabelled, s.cached, s.reused, s.filtered, pruned,
             int(s.stopping), rid))
        self.commit()


# ---------------------------------------------------------------- filters

AGE_UNITS = {"s": 1, "sec": 1, "min": 60, "h": 3600, "d": 86400, "w": 604800,
             "mo": 2629800, "y": 31557600}


def parse_age(text):
    """'2d', '36h', '3w', '6mo', '1y', '90min' -> seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-z]+)\s*", (text or "").lower())
    if not m or m.group(2) not in AGE_UNITS:
        raise argparse.ArgumentTypeError(
            f"bad age {text!r}: use a number + unit (min, h, d, w, mo, y), e.g. 2d, 6mo, 1y")
    return float(m.group(1)) * AGE_UNITS[m.group(2)]


ONLY = {"bad": "status='BAD'", "warn": "status='WARN'", "mislabelled": "name_issues != '[]'"}


def parse_only(text):
    """'bad,warn' -> ['bad', 'warn']"""
    kinds = [k.strip().lower() for k in (text or "").split(",") if k.strip()]
    bad = [k for k in kinds if k not in ONLY]
    if not kinds or bad:
        raise argparse.ArgumentTypeError(f"--only takes {', '.join(ONLY)} (comma-separated), got {text!r}")
    return kinds


def flagged(db, kinds, roots):
    """Paths of files the database flags as any of kinds, under roots."""
    scope = " OR ".join(["path = ? OR path LIKE ?"] * len(roots))
    params = [x for r in roots for x in (r.rstrip("/"), r.rstrip("/") + "/%")]
    where = f"({' OR '.join(ONLY[k] for k in kinds)}) AND ({scope})"
    return [r["path"] for r in db.conn().execute(f"SELECT path FROM files WHERE {where} ORDER BY path", params)]


def name_matcher(pattern):
    """Case-insensitive glob on the full path; plain text means 'contains'."""
    if not pattern:
        return None
    pat = pattern.lower()
    if not any(ch in pat for ch in "*?["):
        pat = f"*{pat}*"
    return lambda p: fnmatch.fnmatchcase(p.lower(), pat)


def walk(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root; continue
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS)
            for f in sorted(files):
                if os.path.splitext(f)[1].lower() in VIDEO_EXT and "sample" not in f.lower():
                    yield os.path.join(d, f)


# ---------------------------------------------------------------- run state + display

class Stats:
    def __init__(self, args, mode):
        self.args, self.mode = args, mode
        self.phase = "discovering"
        self.found = self.cached = self.queued = self.done = self.reused = self.filtered = 0
        self.mislabelled = 0
        self.counts = {"OK": 0, "WARN": 0, "BAD": 0}
        self.bytes_done = 0
        self.start = time.time()
        self.scan_start = None
        self.recent = deque(maxlen=8)   # (status, path, first problem)
        self.report = ""
        self.stopping = False
        self.decoder = decoder_label(args)
        self._decodes = None
        self._workers = None            # set on a snapshot loaded by a watching terminal

    SNAP = ("mode", "phase", "found", "cached", "queued", "done", "reused", "filtered", "mislabelled", "counts",
            "bytes_done", "start", "scan_start", "report", "stopping", "decoder")

    def decodes(self):
        if self._decodes is not None:
            return self._decodes
        with ACTIVE_LOCK:
            return dict(DECODES)

    def workers(self):
        if self._workers is not None:
            return self._workers
        with ACTIVE_LOCK:
            return sorted((dict(a) for a in ACTIVE.values()), key=lambda a: a["start"])

    def snapshot(self):
        """Everything the dashboard shows, as JSON-able data for terminals that watch this scan."""
        a = self.args
        return {**{k: getattr(self, k) for k in self.SNAP}, "recent": list(self.recent), "workers": self.workers(),
                "decodes": self.decodes(), "args": {"paths": a.paths, "workers": a.workers, "reports": a.reports}}

    @classmethod
    def from_snapshot(cls, d):
        s = cls.__new__(cls)
        for k in cls.SNAP:
            setattr(s, k, d[k])
        s.args = argparse.Namespace(**d["args"])
        s.recent, s._workers, s._decodes = d["recent"], d["workers"], d.get("decodes") or {"cpu": 0, "gpu": 0}
        return s

    def eta(self):
        if not self.done or not self.scan_start:
            return None
        rate = self.done / max(time.time() - self.scan_start, 1)
        return (self.queued - self.done) / rate if rate else None


def hms(sec):
    if sec is None:
        return "--:--:--"
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def short(path, roots):
    for r in roots:
        r = r.rstrip("/")
        if path.startswith(r + "/"):
            return path[len(r) + 1:]
    return path


def first_problem(r):
    return ((r.get("problems") or []) + [f"name: {x}" for x in r.get("name_issues") or []] or [""])[0]


NEON = ["#ff2bd6", "#d63bff", "#9b5cff", "#6a7dff", "#3fa0ff", "#22d3ee", "#22f5c8"]
_FIG = {}


def banner(width, subtitle=""):
    """Figlet header in a neon gradient; falls back to a smaller font on narrow terminals."""
    from rich.text import Text
    if not _FIG:
        import pyfiglet
        for font in ("ansi_shadow", "small"):
            try:
                _FIG[font] = pyfiglet.figlet_format("MEDIA HEALTH", font=font, width=400).rstrip("\n")
            except Exception:
                pass
    art = _FIG.get("ansi_shadow", "")
    if not art or max(map(len, art.splitlines())) > width - 4:
        art = _FIG.get("small", "MEDIA HEALTH")
    t = Text(justify="center")
    lines = [l for l in art.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        t.append(line + "\n", style=f"bold {NEON[i * len(NEON) // max(len(lines), 1)]}")
    if subtitle:
        t.append(subtitle, style="grey62")
    return t


DECODER_TAG = {"none": "[grey62]none (quick)[/]", "cpu": "CPU", "cpu (no GPU)": "CPU [grey50](no GPU found)[/]",
               "cpu+gpu": "[bold #76b900]NVDEC[/] + CPU", "gpu": "[bold #76b900]NVDEC[/]"}


def status_tag(status):
    return {"BAD": "[bold red]BAD[/]", "WARN": "[bold yellow]WARN[/]"}.get(status, "[green]OK[/]")


class Dashboard:
    """Full-screen live view (rich). Imported lazily so plain mode needs no extras."""

    def __init__(self, stats, console=None, watching=False):
        from rich.console import Console
        self.console = console or Console()
        self.s = stats
        self.watching = watching        # another process runs the scan; this terminal only shows it

    def header(self, width):
        a = self.s.args
        return banner(width, f"v{VERSION}  ·  mode {self.s.mode}  ·  decode {self.s.decoder}  ·  {a.workers} workers  ·  "
                             f"{', '.join(a.paths)}")

    def stats_panel(self):
        from rich.table import Table
        from rich.panel import Panel
        from rich.progress_bar import ProgressBar
        from rich.console import Group
        s = self.s
        el = time.time() - s.start
        rate = s.done / max(time.time() - s.scan_start, 1) * 60 if s.scan_start else 0
        cells = [
            ("Found", f"[bold]{s.found:,}[/]"), ("Queued", f"[bold]{s.queued:,}[/]"),
            ("Checked", f"[bold]{s.done:,}[/]"), ("Remaining", f"[bold]{max(s.queued - s.done, 0):,}[/]"),
            ("OK", f"[bold green]{s.counts['OK']:,}[/]"), ("Warn", f"[bold yellow]{s.counts['WARN']:,}[/]"),
            ("Bad", f"[bold red]{s.counts['BAD']:,}[/]"), ("Mislabelled", f"[bold #ff8c42]{s.mislabelled:,}[/]"),
            ("Unchanged", f"[grey62]{s.cached:,}[/]"), ("Reused", f"[grey62]{s.reused:,}[/]"),
            ("Decoder", DECODER_TAG.get(s.decoder, s.decoder)),
            ("GPU / CPU", f"{s.decodes()['gpu']:,} / {s.decodes()['cpu']:,}"),   # decode points run on each
            ("Data read", f"{s.bytes_done / 1e12:,.2f} TB" if s.bytes_done > 1e12 else f"{s.bytes_done / 1e9:,.1f} GB"),
            ("Elapsed", hms(el)), ("ETA", hms(s.eta())), ("Rate", f"{rate:,.1f}/min"),
        ]
        per_row = 4 if self.console.size.width >= 110 else 2
        g = Table.grid(expand=True, padding=(0, 2))
        for _ in range(per_row):
            g.add_column(justify="right", style="grey62", ratio=1, no_wrap=True)
            g.add_column(justify="left", ratio=1, no_wrap=True)
        for i in range(0, len(cells), per_row):
            row = [x for pair in cells[i:i + per_row] for x in pair]
            g.add_row(*row, *[""] * (per_row * 2 - len(row)))
        pct = s.done / s.queued if s.queued else (1 if s.phase == "done" else 0)
        bar = Table.grid(expand=True)
        bar.add_column(ratio=1); bar.add_column(width=8, justify="right")
        bar.add_row(ProgressBar(total=max(s.queued, 1), completed=s.done if s.queued else pct, complete_style="#ff2bd6",
                                finished_style="green", pulse=s.phase == "discovering" and not s.queued),
                    f"[bold]{pct * 100:5.1f}%[/]")
        title = {"discovering": "[bold #22d3ee]scanning · still discovering files…[/]" if s.queued
                 else "[bold #22d3ee]discovering files…[/]", "scanning": "[bold #22f5c8]scanning[/]",
                 "stopping": "[bold yellow]stopping — finishing current files[/]",
                 "done": "[bold green]finished[/]" if not s.stopping else "[bold yellow]stopped[/]"}[s.phase]
        return Panel(Group(g, "", bar), title=title, border_style="#9b5cff", box=self.box())

    def workers_panel(self, height):
        from rich.table import Table
        from rich.panel import Panel
        from rich.markup import escape
        wide = self.console.size.width >= 110
        t = Table(expand=True, box=None, show_edge=False, pad_edge=False, header_style="grey62")
        t.add_column("Stage", width=22 if wide else 16, no_wrap=True, style="#22d3ee")
        t.add_column("Time", width=5, justify="right", no_wrap=True)
        t.add_column("Size", width=8, justify="right", no_wrap=True)
        if wide:
            t.add_column("Video", width=22, no_wrap=True, overflow="ellipsis", style="grey62")
        t.add_column("File", ratio=1, no_wrap=True, overflow="ellipsis")
        rows = self.s.workers()
        for a in rows[:height]:
            cols = [a["stage"], hms(time.time() - a["start"])[-5:], f"{a['size'] / 1e9:.1f} GB"]
            cols += [a.get("info", "")] if wide else []
            t.add_row(*cols, escape(os.path.basename(a["path"])))
        if not rows:
            t.add_row("[grey50]idle[/]", *[""] * (4 if wide else 3))
        return Panel(t, title="[bold]now checking[/]", border_style="#3fa0ff", box=self.box())

    def recent_panel(self):
        from rich.table import Table
        from rich.panel import Panel
        from rich.markup import escape          # ffmpeg lines start with "[hevc @ 0x...]"
        t = Table(expand=True, box=None, show_edge=False, pad_edge=False, show_header=False)
        t.add_column(width=4, no_wrap=True)
        t.add_column(ratio=2, no_wrap=True, overflow="ellipsis")
        t.add_column(ratio=3, no_wrap=True, overflow="ellipsis", style="grey62")
        for status, path, prob in reversed(self.s.recent):
            t.add_row(status_tag(status), escape(os.path.basename(path)), escape(prob))
        if not self.s.recent:
            t.add_row("", "[grey50]no problems found yet[/]", "")
        return Panel(t, title="[bold]latest problems[/]", border_style="#ff2bd6", box=self.box())

    def footer(self):
        from rich.text import Text
        t = Text(justify="center", style="grey50")
        if self.watching:
            t.append("q / Ctrl+C leave (the scan keeps going, media-health brings you back)  ·  s stop the scan  ·  ")
        else:
            t.append("Ctrl+C stop (progress saved)  ·  ")
        t.append(self.s.report or f"{self.s.args.reports}/latest.csv", style="grey70")
        return t

    @staticmethod
    def box():
        from rich import box
        return box.ROUNDED

    def __rich__(self):
        from rich.console import Group
        w, h = self.console.size
        workers = max(1, min(self.s.args.workers, h - 30))
        return Group(self.header(w), self.stats_panel(), self.workers_panel(workers),
                     self.recent_panel(), self.footer())

    def final(self, bad, out):
        c = self.console
        self.s.phase = "done"
        c.print(self.header(c.size.width))
        c.print(self.stats_panel())
        print_problems(c, bad, self.s.args.paths, title_extra=" in this scope", limit=40)
        if out:
            c.print(f"[grey62]Full report:[/] {out}")


def problems_under(db, paths):
    scope = " OR ".join(["path LIKE ?"] * len(paths))
    params = tuple(p.rstrip("/") + "%" for p in paths)
    return db.rows(f"status != 'OK' AND ({scope})", params, order="CASE status WHEN 'BAD' THEN 0 ELSE 1 END, path")


def print_problems(c, rows, roots, title_extra="", limit=40, offset=0, total=None):
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.markup import escape
    if not rows:
        c.print("[green]No problem files.[/]")
        return
    total = total if total is not None else len(rows)
    t = Table(expand=True, box=box.ROUNDED, header_style="grey62", border_style="#ff2bd6")
    t.add_column("", width=4); t.add_column("File", ratio=2, overflow="fold")
    t.add_column("Problem", ratio=3, overflow="fold", style="grey62")
    for r in rows[:limit]:
        probs = [escape(p[:300]) for p in r.get("problems") or []] + \
                [f"[#ff8c42]name:[/] {escape(x)}" for x in r.get("name_issues") or []]
        better = suggest_name(r["path"], r)
        tip = [f"[#22d3ee]rename:[/] [white]{escape(better)}[/]"] if better else []
        t.add_row(status_tag(r["status"]), escape(short(r["path"], roots)), "\n".join(probs[:3] + tip))
    shown = f"{offset + 1}-{offset + min(limit, len(rows))} of {total}" if total > limit or offset else f"{total}"
    c.print(Panel(t, title=f"[bold]{shown} file(s) need attention{title_extra}[/]", border_style="#ff2bd6",
                  box=box.ROUNDED))


# ---------------------------------------------------------------- scan

EXAMPLES = [
    ("media-health", "interactive menu"),
    ("media-health /media --quick", "fast sweep of the whole library (~1 s/file)"),
    ("media-health /media/movies --min-gb 15", "sampled decode check of big (4K remux) files"),
    ("media-health /media/tv_shows --newer-than 7d", "only what arrived in the last week"),
    ("media-health /media --name '*GROUP*' --force", "re-check one release group, ignoring the database"),
    ("media-health '/media/movies/Sintel (2010)' --full", "decode every frame of a suspect"),
    ("media-health /media --background", "sampled check in the scheduler: keeps going after you log out"),
    ("media-health /media --quick --mislabel bad", "treat mislabelled files as BAD"),
]


class HelpParser(argparse.ArgumentParser):
    """argparse with a rich help screen on a terminal (plain text otherwise)."""

    def print_help(self, file=None):
        if (file or sys.stdout).isatty():
            try:
                return rich_help(self)
            except ImportError:
                pass
        super().print_help(file)


def rich_help(parser):
    from rich.console import Console
    from rich.markup import escape
    from rich.panel import Panel
    from rich.table import Table
    from rich import box
    c = Console()
    c.print(banner(c.size.width, f"v{VERSION}  ·  find damaged, broken or mislabelled media files"))
    c.print(Panel("[bold]media-health[/] [#22d3ee][PATH ...][/] [grey62][OPTIONS][/]\n"
                  "[grey62]no arguments on a terminal opens the interactive menu · PATH defaults to /media[/]",
                  title="[bold]usage[/]", border_style="#9b5cff", box=box.ROUNDED))
    titles = {"positional arguments": "paths", "options": "general"}
    for group in parser._action_groups:
        acts = [a for a in group._group_actions if a.help != argparse.SUPPRESS]
        if not acts:
            continue
        t = Table(box=None, show_header=False, padding=(0, 2), expand=True)
        t.add_column(style="bold #22d3ee", no_wrap=True)
        t.add_column(ratio=1)
        for a in acts:
            flags = ", ".join(a.option_strings) or "PATH ..."
            if a.option_strings and a.nargs != 0:
                flags += " " + ("{" + ",".join(a.choices) + "}" if a.choices else (a.metavar or a.dest.upper()))
            t.add_row(escape(flags), escape(a.help or "") + (
                f" [grey50](default {escape(str(a.default))})[/]" if a.option_strings and a.default not in (None, False, 0)
                and a.default != argparse.SUPPRESS and "default" not in (a.help or "") else ""))
        c.print(Panel(t, title=f"[bold]{titles.get(group.title, group.title)}[/]", border_style="#3fa0ff", box=box.ROUNDED))
    ex = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    ex.add_column(style="bold", no_wrap=True); ex.add_column(style="grey62", ratio=1)
    for cmd, what in EXAMPLES:
        ex.add_row(escape(cmd), what)
    c.print(Panel(ex, title="[bold]examples[/]", border_style="#ff2bd6", box=box.ROUNDED))
    c.print("[grey50]AGE = number + min, h, d, w, mo, y · Tab completes options, values and paths · "
            "database: <reports>/media-health.db · README: https://github.com/Jarsky/media-health[/]")


def build_parser():
    ap = HelpParser(prog="media-health", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                    epilog="examples:\n" + "\n".join(f"  {c:<52} {w}" for c, w in EXAMPLES))
    ap.add_argument("paths", nargs="*", default=["/media"], help="files or folders to scan (default /media)")
    ap.add_argument("--reports", default="/reports", metavar="DIR", help="where the database and CSV reports live")
    ap.add_argument("--workers", type=int, default=2, metavar="N", help="files checked in parallel")
    ap.add_argument("--plain", action="store_true", help="no dashboard, even on a terminal (one status line per 30 s)")
    ap.add_argument("--version", action="version", version=f"media-health {VERSION}")
    ap.add_argument("--completion", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--motd", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    sc = ap.add_argument_group("schedule (run by the media-health container)")
    sc.add_argument("--schedule-add", metavar="WHEN",
                    help="schedule this same command instead of running it now. WHEN: 'daily 2am', 'thu 3am', "
                         "'mon,thu 03:30', 'weekdays 1am', 'weekends 4am', 'monthly 1 2am', 'hourly', 'every 6h' "
                         "or cron '0 2 * * *'")
    sc.add_argument("--schedule-list", action="store_true", help="list schedules with their next run and last result")
    sc.add_argument("--schedule-rm", type=int, metavar="ID", help="delete a schedule")
    sc.add_argument("--schedule-pause", type=int, metavar="ID", help="pause a schedule")
    sc.add_argument("--schedule-resume", type=int, metavar="ID", help="resume a paused schedule")
    sc.add_argument("--schedule-run", type=int, metavar="ID", help="run a schedule now")
    sc.add_argument("--background", "--bg", action="store_true",
                    help="run this scan once in the scheduler instead of this shell: it keeps going after you log out "
                         "(follow it with docker logs -f media-health)")
    g = ap.add_argument_group("depth")
    g.add_argument("--quick", action="store_true", help="probe + zero-block + name check only, no decoding (~1 s/file)")
    g.add_argument("--full", action="store_true", help="decode every frame (slow: roughly real time per file)")
    g.add_argument("--force", "--recheck", action="store_true", dest="force",
                   help="rescan everything, ignoring the database (incl. fingerprint reuse)")
    g.add_argument("--gpu", choices=("auto", "on", "off"), default="auto",
                   help="NVDEC decoding (needs the NVIDIA runtime). auto = sampled scans decode the start on the CPU "
                        "and the other points on the GPU, --full stays on the CPU; on = everything on the GPU (fastest, "
                        "but blind to damaged slice data); off = CPU only")
    f = ap.add_argument_group("filters")
    f.add_argument("--name", metavar="TEXT", help="only paths matching this (case-insensitive; plain text = contains, * ? globs ok)")
    f.add_argument("--match", metavar="REGEX", help="only paths matching this regex")
    f.add_argument("--newer-than", type=parse_age, metavar="AGE", help="only files modified within AGE (e.g. 2d, 3w, 6mo, 1y)")
    f.add_argument("--older-than", type=parse_age, metavar="AGE", help="only files modified more than AGE ago")
    f.add_argument("--min-gb", type=float, default=0, metavar="GB", help="only files at least this big (e.g. 15 for 4K remuxes)")
    f.add_argument("--limit", type=int, metavar="N", help="stop after queueing N files")
    f.add_argument("--only", type=parse_only, metavar="KINDS",
                   help="recheck only files the database already flags under PATH: bad, warn and/or mislabelled, "
                        "comma-separated (implies --force; pair with --full to confirm suspects)")
    t = ap.add_argument_group("tuning")
    t.add_argument("--mislabel", choices=("warn", "bad", "info", "off"), default="warn",
                   help="status for files whose name doesn't match the streams (info = report only)")
    t.add_argument("--zero-samples", type=int, default=16, metavar="N", help="1 MiB zero-check samples per file")
    t.add_argument("--decode-samples", type=int, default=4, metavar="N", help="decode points besides the start")
    t.add_argument("--decode-seconds", type=int, default=5, metavar="S", help="seconds decoded at each point")
    t.add_argument("--bad-threshold", type=int, default=3, metavar="N", help="serious decode errors before a file is BAD")
    t.add_argument("--rss-warn", type=int, default=1500, metavar="MB", help="WARN if a decoder uses more memory than this")
    t.add_argument("--timeout", type=int, default=180, metavar="S", help="per decode point before the file is BAD")
    t.add_argument("--timeout-full", type=int, default=6 * 3600, metavar="S", help="for a --full decode")
    return ap


VALUE_HINTS = {"--schedule-add": ["daily 2am", "thu 3am", "mon,thu 03:30", "weekdays 1am", "weekends 4am",
                                  "monthly 1 2am", "hourly", "every 6h", "0 2 * * *"],
               "--newer-than": ["1d", "2d", "7d", "14d", "30d", "3mo", "6mo", "1y"],
               "--older-than": ["1d", "7d", "30d", "6mo", "1y", "2y", "5y"],
               "--workers": ["1", "2", "3", "4", "6", "8"], "--min-gb": ["1", "5", "10", "15", "30", "50"],
               "--limit": ["10", "50", "100", "500", "1000"],
               "--only": ["bad", "warn", "mislabelled", "bad,warn", "bad,warn,mislabelled"]}


def completion_script(parser):
    """bash completion generated from the parser, so it always matches the real options."""
    opts, choices, free, dirs = [], {}, [], []
    for a in parser._actions:
        if not a.option_strings or a.help == argparse.SUPPRESS:
            continue
        opts += a.option_strings
        if a.nargs == 0:
            continue
        for o in a.option_strings:
            if a.choices:
                choices[o] = list(a.choices)
            elif o in VALUE_HINTS:
                choices[o] = VALUE_HINTS[o]
            elif a.metavar == "DIR":
                dirs.append(o)
            else:
                free.append(o)
    exts = "|".join(sorted(e.lstrip(".") for e in VIDEO_EXT))
    # values can contain spaces ('daily 2am'), which compgen -W can't keep together: match them in
    # _mh_values, offering 'quoted' forms if the user started a quote, else backslash-escaped ones
    cases = "\n".join(f'''    {o}) _mh_values {" ".join(shlex.quote(v) for v in vals)}; return;;'''
                      for o, vals in choices.items())
    return f"""# media-health bash completion (generated by: media-health --completion)
shopt -s extglob
_mh_values() {{
  local v q typed="${{cur#\\'}}"; typed="${{typed//\\\\/}}"
  COMPREPLY=()
  for v in "$@"; do
    [[ "$v" == "$typed"* ]] || continue
    if [[ "$cur" == \\'* ]]; then COMPREPLY+=("'$v'"); else printf -v q '%q' "$v"; COMPREPLY+=("$q"); fi
  done
}}
_media_health() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}" prev="${{COMP_WORDS[COMP_CWORD-1]}}"
  case "$prev" in
{cases}
    {"|".join(dirs) or "--no-dir-opts"}) local IFS=$'\\n'; compopt -o filenames; COMPREPLY=($(compgen -d -- "$cur")); return;;
    {"|".join(free) or "--no-free-opts"}) return;;
  esac
  if [[ "$cur" == -* ]]; then
    COMPREPLY=($(compgen -W "{' '.join(opts)}" -- "$cur")); return
  fi
  local IFS=$'\\n'                  # paths can contain spaces
  compopt -o filenames
  [[ -z "$cur" ]] && cur=/media/
  COMPREPLY=($(compgen -d -- "$cur"; compgen -f -X '!*.@({exts}|{exts.upper()})' -- "$cur"))
}}
complete -F _media_health media-health

# first word on the line: m<Tab> (or any prefix of it) completes to media-health,
# everything else completes commands as usual
_media_health_initial() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [[ -n "$cur" && "media-health" == "$cur"* ]]; then COMPREPLY=(media-health); return; fi
  if [[ "$cur" == */* ]]; then local IFS=$'\\n'; compopt -o filenames; COMPREPLY=($(compgen -f -- "$cur")); return; fi
  COMPREPLY=($(compgen -c -- "$cur" | sort -u))
}}
complete -I -F _media_health_initial 2>/dev/null
"""


def motd():
    from rich.console import Console
    from rich.table import Table
    c = Console()
    c.print(banner(c.size.width, f"v{VERSION}"))
    t = Table(box=None, show_header=False, padding=(0, 2))
    t.add_column(style="bold #22d3ee", no_wrap=True); t.add_column(style="grey62")
    for k, v in [("media-health", "open the menu"), ("media-health -h", "all options and examples"),
                 ("m<Tab>", "completes media-health · Tab also completes options, values and paths"),
                 ("media-health --schedule-list", "scheduled scans (add: any scan options + --schedule-add 'daily 2am')"),
                 ("sqlite3 /reports/media-health.db", "query the results directly")]:
        t.add_row(k, v)
    c.print(t, justify="center")
    c.print("[grey50]scans run in the scheduler: closing this shell doesn't stop them · "
            "run media-health again to get back to a running scan[/]", justify="center")


def tracked_check(path, size, args, mode, db):
    """Fingerprint lookup, then the real checks; reports progress to the dashboard."""
    tid = threading.get_ident()
    with ACTIVE_LOCK:
        ACTIVE[tid] = {"path": path, "start": time.time(), "stage": "fingerprint", "info": "", "size": size}
    try:
        fp = fingerprint(path, size)
        if not args.force:
            hit = db.by_fingerprint(fp, size, mode)
            if hit:
                hit.update(path=path, fingerprint=fp, reused_from=hit["path"] if hit["path"] != path else hit.get("reused_from"))
                if hit["path"] != path:
                    hit["notes"] = [n for n in hit.get("notes", []) if not n.startswith("identical to")] + \
                                   [f"identical to {hit['reused_from']} (result reused)"]
                hit.pop("first_seen", None)
                hit["_reused"] = True
                return hit
        r = check(path, args)
        r.update(fingerprint=fp, mode=mode, check_version=CHECK_VERSION)
        return r
    finally:
        with ACTIVE_LOCK:
            ACTIVE.pop(tid, None)


def scan(args, argv_text="", interactive=False):
    os.makedirs(args.reports, exist_ok=True)
    db = DB(os.path.join(args.reports, "media-health.db"))
    mode = "full" if args.full else "quick" if args.quick else "sample"
    rx = re.compile(args.match, re.I) if args.match else None
    nm = name_matcher(args.name)
    if args.only:
        args.force = True               # the point is to check these again
        targets = flagged(db, args.only, args.paths)
        gone = [p for p in targets if not os.path.exists(p)]
        db.conn().executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone]); db.commit()
        targets = [p for p in targets if p not in set(gone)]
    with ACTIVE_LOCK:
        DECODES.update(cpu=0, gpu=0)
    now = time.time()
    s = Stats(args, mode)
    filtered_run = any([rx, nm, args.newer_than, args.older_than, args.min_gb, args.limit, args.only])

    def on_term(*_):
        s.stopping = True
    old_int = signal.signal(signal.SIGINT, on_term)
    old_term = signal.signal(signal.SIGTERM, on_term)
    old_hup = signal.signal(signal.SIGHUP, on_term)   # shell closed: stop cleanly, keep progress

    dash = None
    if not args.plain and sys.stdout.isatty():
        try:
            dash = Dashboard(s)
        except ImportError:
            dash = None
    live = None
    if dash:
        from rich.live import Live
        live = Live(dash, console=dash.console, screen=True, refresh_per_second=4)
        live.start()
    else:
        print(f"media-health {VERSION}  mode={mode} gpu={gpu_points(args)} workers={args.workers} "
              f"paths={' '.join(args.paths)}", flush=True)
    lock = None
    if not os.environ.get("MH_HAVE_LOCK"):           # scheduled runs inherit the scheduler's lock
        lock = try_scan_lock(args.reports)
        if lock is None:
            msg = "note: another scan is already running; this one runs alongside it"
            print(msg, flush=True) if not dash else dash.console.log(msg)
    if os.environ.get("MH_SCHEDULE_ID"):
        argv_text = f"[schedule {os.environ['MH_SCHEDULE_ID']}] {argv_text}"
    rid = db.run_start(mode, argv_text)
    status_at = [0.0]

    def publish(force=False):
        if force or time.time() - status_at[0] >= 1:
            status_at[0] = time.time()
            write_status(args.reports, s, rid)
    publish(force=True)

    last_commit = last_line = time.time()
    seen = set()

    def plain_status(force=False):
        nonlocal last_line
        if dash or (not force and time.time() - last_line < 30):
            return
        last_line = time.time()
        phase = "scanning (still discovering)" if s.phase == "discovering" and s.queued else s.phase
        print(f"[{dt.datetime.now():%H:%M:%S}] {phase}: {s.done:,}/{s.queued:,} checked "
              f"({s.counts['OK']:,} ok, {s.counts['WARN']:,} warn, {s.counts['BAD']:,} bad, "
              f"{s.mislabelled:,} mislabelled, {s.cached:,} unchanged, {s.reused:,} reused) ETA {hms(s.eta())}"
              f" · decode {s.decoder} (gpu {s.decodes()['gpu']:,} / cpu {s.decodes()['cpu']:,} points)",
              flush=True)

    # Discovery runs in its own thread so the totals fill in quickly while workers
    # already start on the first files.
    todo, renamecheck = deque(), deque()
    discovered = threading.Event()
    complete_walk = [False]

    def discover():
        try:
            for path in (walk(targets) if args.only else walk(args.paths)):
                if s.stopping:
                    return
                seen.add(path)
                if (rx and not rx.search(path)) or (nm and not nm(path)):
                    s.filtered += 1
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                age = now - st.st_mtime
                if (st.st_size < args.min_gb * 1e9 or (args.newer_than and age > args.newer_than)
                        or (args.older_than and age < args.older_than)):
                    s.filtered += 1
                    continue
                s.found += 1
                prev = db.get(path)
                if (prev and not args.force and prev["size"] == st.st_size and prev["mtime"] == int(st.st_mtime)
                        and current(prev) and DEPTH.get(prev["mode"], -1) >= DEPTH[mode]):
                    s.cached += 1
                    if prev.get("name_version") != NAME_VERSION:
                        renamecheck.append(prev)      # re-evaluate the name check from stored data
                    continue
                todo.append((path, st.st_size, int(st.st_mtime)))
                s.queued += 1
                if args.limit and s.queued >= args.limit:
                    return
            complete_walk[0] = True
        finally:
            discovered.set()
            if not s.stopping:
                s.phase = "scanning"

    threading.Thread(target=discover, daemon=True, name="discover").start()

    def record(r):
        apply_name_check(r, args.mislabel)
        db.save(r)
        s.done += 1
        s.counts[r["status"]] += 1
        s.mislabelled += bool(r["name_issues"])
        s.bytes_done += int((r.get("size_gb") or 0) * 1e9)
        if r["status"] != "OK" or (r["name_issues"] and args.mislabel == "info"):
            s.recent.append((r["status"], r["path"], first_problem(r)))
            if not dash:
                print(f"{r['status']:4} {r['path']}\n     " + "\n     ".join(
                    (r.get("problems") or []) + [f"name: {x}" for x in r["name_issues"]]), flush=True)

    pruned = len(gone) if args.only else 0  # --only forgets flagged files that no longer exist
    try:
        with ThreadPoolExecutor(args.workers) as ex:
            pending = {}
            while True:
                while renamecheck:
                    db.save(apply_name_check(renamecheck.popleft(), args.mislabel))
                while not s.stopping and todo and len(pending) < args.workers * 2:
                    path, size, mtime = todo.popleft()
                    s.scan_start = s.scan_start or time.time()
                    pending[ex.submit(tracked_check, path, size, args, mode, db)] = (path, size, mtime)
                if s.stopping:
                    s.phase = "stopping"
                    for f in [f for f in pending if f.cancel()]:
                        pending.pop(f)
                if not pending:
                    if s.stopping or (discovered.is_set() and not todo and not renamecheck):
                        break
                    publish()
                    time.sleep(0.2)
                    continue
                done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    path, size, mtime = pending.pop(fut)
                    try:
                        r = fut.result()
                    except Exception as e:  # unreadable file, permission, I/O error
                        r = {"path": path, "base_status": "BAD", "problems": [f"{type(e).__name__}: {e}"],
                             "notes": [], "mode": mode, "check_version": CHECK_VERSION}
                    if r.pop("_reused", False):
                        s.reused += 1
                    else:
                        r["reused_from"] = None
                    r.update(size=size, mtime=mtime, checked=dt.datetime.now().isoformat(timespec="seconds"))
                    record(r)
                if time.time() - last_commit > 5:
                    db.commit(); last_commit = time.time()
                plain_status()
                publish()
        # forget files that no longer exist, but only after a complete, unfiltered walk
        if complete_walk[0] and not filtered_run and not s.stopping:
            pruned = sum(db.prune(p, seen) for p in args.paths if os.path.isdir(p))
        db.commit()
    finally:
        if live:
            try:
                live.stop()
            except Exception:       # terminal already gone (shell closed)
                pass
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGHUP, old_hup)

    db.run_end(rid, s, pruned)
    out = write_report(db, args.reports)
    s.report = out
    s.phase = "done"
    publish(force=True)
    bad = problems_under(db, args.paths)
    if dash:
        try:
            dash.final(bad, out)
        except OSError:
            pass
    else:
        plain_status(force=True)
        print(f"{len(bad)} problem file(s) under {' '.join(args.paths)}; database has {db.count():,} files"
              + (f"; pruned {pruned} deleted files" if pruned else "") + f" -> {out}")
    return 1 if any(r["status"] == "BAD" for r in bad) else 0


def write_report(db, reports):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    cols = ["status", "path", "size_gb", "problems", "name_issues", "notes", "vcodec", "vprofile", "res",
            "pix_fmt", "hdr", "dv_profile", "audio", "bitrate_mbps", "peak_rss_mb", "mode", "reused_from", "checked",
            "suggested_name"]
    out = os.path.join(reports, f"media-health-{stamp}.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader()
        for r in db.rows(order="CASE status WHEN 'BAD' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, path"):
            w.writerow({**r, **{k: " ; ".join(r.get(k) or []) for k in JSON_COLS},
                        "suggested_name": suggest_name(r["path"], r) if r.get("name_issues") else ""})
    latest = os.path.join(reports, "latest.csv")
    if os.path.lexists(latest):
        os.remove(latest)
    os.symlink(os.path.basename(out), latest)
    return out


# ---------------------------------------------------------------- schedules

DOW_NAMES = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
MON_NAMES = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                          "sep", "oct", "nov", "dec"), 1)}


class Cron:
    """Standard 5-field cron (minute hour day-of-month month day-of-week), local time.
    Supports * , - / and day/month names; dom and dow both restricted = either matches."""

    def __init__(self, expr):
        f = expr.split()
        if len(f) != 5:
            raise ValueError(f"cron needs 5 fields, got {len(f)}: {expr!r}")
        self.expr = " ".join(f)
        self.minute = self._field(f[0], 0, 59)
        self.hour = self._field(f[1], 0, 23)
        self.dom = self._field(f[2], 1, 31)
        self.month = self._field(f[3], 1, 12, MON_NAMES)
        self.dow = {d % 7 for d in self._field(f[4], 0, 7, DOW_NAMES)}
        self.dom_any, self.dow_any = f[2] in ("*", "?"), f[4] in ("*", "?")

    @staticmethod
    def _field(spec, lo, hi, names=None):
        def num(x):
            x = x.lower()
            if names and x[:3] in names:
                return names[x[:3]]
            v = int(x)
            if not lo <= v <= hi:
                raise ValueError(f"{v} out of range {lo}-{hi}")
            return v
        out = set()
        for part in spec.split(","):
            rng, _, step = part.partition("/")
            step = int(step) if step else 1
            if rng in ("*", "?"):
                a, b = lo, hi
            elif "-" in rng:
                a, b = (num(x) for x in rng.split("-", 1))
            else:
                a = num(rng); b = hi if step > 1 else a
            out.update(range(a, b + 1, step))
        if not out:
            raise ValueError(f"empty cron field {spec!r}")
        return out

    def _day_ok(self, t):
        dom_ok, dow_ok = t.day in self.dom, (t.weekday() + 1) % 7 in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, t):
        t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        end = t + dt.timedelta(days=366 * 5)
        while t < end:
            if t.month not in self.month:
                t = (t.replace(day=1, hour=0, minute=0) + dt.timedelta(days=32)).replace(day=1)
            elif not self._day_ok(t):
                t = t.replace(hour=0, minute=0) + dt.timedelta(days=1)
            elif t.hour not in self.hour:
                t = t.replace(minute=0) + dt.timedelta(hours=1)
            elif t.minute not in self.minute:
                t += dt.timedelta(minutes=1)
            else:
                return t
        raise ValueError(f"cron {self.expr!r} never fires")


def parse_when(text):
    """Plain English or cron -> (cron expression, label).
    daily 2am · 2:30am · thu 3am · mon,thu 03:30 · weekdays 1am · weekends 4:15am ·
    monthly 1 2am · hourly · every 6h · or 5 cron fields like '0 2 * * *'."""
    t = re.sub(r"\s+", " ", (text or "").strip().lower())
    if len(t.split()) == 5 and re.fullmatch(r"[0-9a-z*?,/\- ]+", t) and not re.search(r"\d(am|pm)", t):
        return Cron(t).expr, t
    if t in ("hourly", "every hour"):
        return "0 * * * *", "hourly"
    m = re.fullmatch(r"every (\d+) ?h(?:ours?)?", t)
    if m:
        return f"0 */{int(m.group(1))} * * *", f"every {int(m.group(1))}h"
    t = re.sub(r"\s*\band\b\s*", ",", t)
    words = [w for w in re.sub(r"\b(at|every|on)\b", " ", t).split() if w]
    tm = next((w for w in reversed(words) if re.fullmatch(r"\d{1,2}(:\d{2})?(am|pm)?", w)
               and not (words.index(w) > 0 and words[words.index(w) - 1] == "monthly" and ":" not in w
                        and not w.endswith(("am", "pm")))), None)
    if not tm:
        raise ValueError(f"no time in {text!r} (e.g. 'daily 2am', 'thu 03:30', or cron '0 2 * * *')")
    words.remove(tm)
    mm = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", tm)
    hour, minute, ampm = int(mm.group(1)), int(mm.group(2) or 0), mm.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"bad time {tm!r}")
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        raise ValueError(f"bad time {tm!r}")
    hhmm = f"{hour:02d}:{minute:02d}"
    rest = " ".join(words)
    if rest in ("", "daily", "day", "everyday", "every day", "nightly"):
        return f"{minute} {hour} * * *", f"daily {hhmm}"
    if rest in ("weekdays", "weekday"):
        return f"{minute} {hour} * * 1-5", f"weekdays {hhmm}"
    if rest in ("weekends", "weekend"):
        return f"{minute} {hour} * * 0,6", f"weekends {hhmm}"
    m = re.fullmatch(r"monthly(?: (\d{1,2})(?:st|nd|rd|th)?)?", rest)
    if m:
        day = int(m.group(1) or 1)
        if not 1 <= day <= 31:
            raise ValueError(f"bad day of month {day}")
        return f"{minute} {hour} {day} * *", f"monthly day {day} {hhmm}"
    days = []
    for w in re.split(r"[ ,]+", rest.replace("weekly", "").strip()):
        if not w:
            continue
        if "-" in w and all(x[:3] in DOW_NAMES for x in w.split("-")):
            a, b = (DOW_NAMES[x[:3]] for x in w.split("-"))
            days += list(range(a, b + 1)) if a <= b else list(range(a, 7)) + list(range(0, b + 1))  # fri-sun wraps
        elif w[:3] in DOW_NAMES:
            days.append(DOW_NAMES[w[:3]])
        else:
            raise ValueError(f"don't understand {w!r} in {text!r}")
    if not days:
        raise ValueError(f"which days? {text!r}")
    names = ",".join(d.title() for d, i in sorted(DOW_NAMES.items(), key=lambda x: (x[1] - 1) % 7) if i in set(days))
    return f"{minute} {hour} * * {','.join(str(d) for d in sorted(set(days)))}", f"{names} {hhmm}"


def schedules(db, enabled_only=False):
    q = "SELECT * FROM schedules" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    out = []
    for r in db.conn().execute(q):
        r = dict(r)
        r["args"] = json.loads(r["args"] or "[]")
        try:
            r["next"] = Cron(r["cron"]).next_after(dt.datetime.now()) if r["enabled"] else None
        except ValueError:
            r["next"] = None
        out.append(r)
    return out


def schedule_add(db, when, args):
    cron, label = parse_when(when)
    build_parser().parse_args(args)           # validate the scan options now, not at 2am
    cur = db.conn().execute("INSERT INTO schedules (label, cron, args, created) VALUES (?,?,?,?)",
                            (label, cron, json.dumps(args), dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()
    return cur.lastrowid, cron, label


def schedule_once(db, args):
    """--background: a schedule row without a cron, run as soon as the scheduler is free, then deleted."""
    build_parser().parse_args(args)
    cur = db.conn().execute("INSERT INTO schedules (label, cron, args, run_now, created) VALUES ('once', '', ?, 1, ?)",
                            (json.dumps(args), dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()
    return cur.lastrowid


def schedule_set(db, sid, **kw):
    n = db.conn().execute(f"UPDATE schedules SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?",
                          (*kw.values(), sid)).rowcount
    db.commit()
    return n


def scheduler_status(db):
    row = db.conn().execute("SELECT value FROM meta WHERE key='scheduler_heartbeat'").fetchone()
    if not row:
        return None
    return time.time() - float(row[0])


def print_schedules(c, db):
    from rich.table import Table
    from rich import box
    from rich.markup import escape
    rows = schedules(db)
    age = scheduler_status(db)
    state = ("[green]running[/]" if age is not None and age < 120 else
             "[red]not running[/] [grey62](start it: docker compose up -d)[/]")
    t = Table(box=box.ROUNDED, border_style="#22f5c8", header_style="grey62", expand=True,
              title=f"schedules · scheduler {state}")
    for col in ("ID", "When", "Next run", "Command", "Last run", "Result"):
        t.add_column(col, overflow="fold" if col == "Command" else "ellipsis", no_wrap=col != "Command",
                     ratio=3 if col == "Command" else None)
    for r in rows:
        nxt = (r["next"].strftime("%a %d %b %H:%M") if r["next"] else
               "[#22d3ee]once, now[/]" if not r["cron"] else "[yellow]paused[/]")
        last = (r["last_start"] or "").replace("T", " ")[:16] or "never"
        res = {None: "", 0: "[green]ok[/]", 1: "[red]bad files[/]"}.get(r["last_exit"], f"[yellow]exit {r['last_exit']}[/]")
        if r["last_start"] and not r["last_end"]:
            res = "[#22d3ee]running[/]"
        t.add_row(str(r["id"]), f"{escape(r['label'])}\n[grey50]{r['cron']}[/]", nxt,
                  escape("media-health " + shlex.join(r["args"])), last, res)
    if not rows:
        t.add_row("", "[grey50]no schedules yet[/]", "", "", "", "")
    c.print(t)


SCAN_LOCK = ".scan.lock"


def try_scan_lock(reports):
    """Exclusive lock while a scan runs, so the scheduler never overlaps a manual scan.
    Returns the open file (keep it) or None if another scan holds it."""
    f = open(os.path.join(reports, SCAN_LOCK), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


# ---------------------------------------------------------------- watching a scan from another terminal

STATUS = ".status.json"
STALE = 30          # a scan that hasn't updated its status for this long is gone


def write_status(reports, s, rid):
    """The dashboard's data for terminals that watch this scan; picks up a stop request from them."""
    path = os.path.join(reports, STATUS)
    stop = os.path.join(reports, f".stop-{rid}")
    if os.path.exists(stop):
        s.stopping = True
        if s.phase == "done":
            os.remove(stop)
    d = dict(s.snapshot(), run=rid, schedule=os.environ.get("MH_SCHEDULE_ID"), updated=time.time())
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(d, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def read_status(reports):
    try:
        with open(os.path.join(reports, STATUS)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def running_scan(reports):
    """Status of the scan running right now, or None."""
    st = read_status(reports)
    return st if st and st.get("phase") != "done" and time.time() - st.get("updated", 0) < STALE else None


class Keys:
    """Single keypresses without Enter (cbreak mode, so Ctrl+C still works)."""

    def __enter__(self):
        self.fd = self.old = None
        if sys.stdin.isatty():
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def get(self, timeout):
        import select
        if self.fd is None:
            time.sleep(timeout)
            return ""
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 1).decode(errors="ignore").lower() if ready else ""


def follow(reports, sid=None, since=0):
    """Show the live dashboard of the running scan, or of background run `sid` once it starts.
    Leaving (q, Esc, Ctrl+C, closing the terminal) only stops watching; s asks the scan to stop.
    Returns 1 if it finished with BAD files under its paths, else 0."""
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    c = Console()
    db = DB(os.path.join(reports, "media-health.db"))
    run, st = None, None
    try:
        with Keys() as keys, Live(console=c, screen=True, refresh_per_second=4) as live:
            while True:
                cur = read_status(reports)
                if run is None and cur and time.time() - cur.get("updated", 0) < STALE and (
                        cur.get("schedule") == str(sid) and cur.get("start", 0) >= since if sid is not None
                        else cur.get("phase") != "done"):
                    run = cur["run"]
                if run is not None:
                    if not cur or cur.get("run") != run:
                        break                           # a later scan has taken over the status file
                    st = cur
                    live.update(Dashboard(Stats.from_snapshot(st), c, watching=True))
                    if st["phase"] == "done" or time.time() - st["updated"] > STALE:
                        break
                else:
                    if sid is None:
                        return 0                        # nothing running
                    if not db.conn().execute("SELECT 1 FROM schedules WHERE id=?", (sid,)).fetchone():
                        live.stop()
                        c.print(f"[red]background run {sid} ended before it started[/] [grey62]- see docker logs "
                                "media-health[/]")
                        return 2
                    other = running_scan(reports)
                    wait_for = (f"\n[grey62]waiting for the scan that's running now: {other['done']:,}/"
                                f"{other['queued']:,} checked in {', '.join(other['args']['paths'])}[/]" if other else "")
                    live.update(Panel(f"[bold #22d3ee]queued[/] as background run {sid} - the scheduler starts it within "
                                      f"a few seconds{wait_for}\n\n[grey50]q leave (it still runs) · s cancel[/]",
                                      title="[bold]media-health[/]", border_style="#9b5cff"))
                k = keys.get(0.5)
                if k in ("q", "\x1b"):
                    break
                if k == "s":
                    if run is not None:
                        open(os.path.join(reports, f".stop-{run}"), "w").close()
                    else:
                        n = db.conn().execute("DELETE FROM schedules WHERE id=? AND cron='' AND last_start IS NULL",
                                              (sid,)).rowcount
                        db.commit()
                        if n:
                            live.stop()
                            c.print(f"[yellow]cancelled background run {sid}[/]")
                            return 0
    except KeyboardInterrupt:
        pass
    if not st or st["phase"] != "done":
        c.print("[grey62]left the scan running · [bold]media-health[/] brings you back · "
                "docker logs -f media-health shows its log[/]" if st else "[grey62]left the queue[/]")
        return 0
    s = Stats.from_snapshot(st)
    bad = problems_under(db, s.args.paths)
    Dashboard(s, c).final(bad, s.report)
    return 1 if any(r["status"] == "BAD" for r in bad) else 0


def launch(argv, reports="/reports"):
    """Interactive scans run in the scheduler, so they outlive this terminal, which watches them.
    Returns the exit code, or None when the scheduler isn't running (then the caller scans here)."""
    db = DB(os.path.join(reports, "media-health.db"))
    if (scheduler_status(db) or 1e9) > 120:
        return None
    since = time.time()
    return follow(reports, schedule_once(db, argv), since)


def serve(reports="/reports"):
    """Scheduler loop for the long-running media-health container."""
    os.makedirs(os.path.join(reports, "logs"), exist_ok=True)
    db = DB(os.path.join(reports, "media-health.db"))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log = lambda msg: print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
    log(f"media-health {VERSION} scheduler started · open a shell: docker exec -it media-health bash")
    due, nexts, child, waiting_logged = deque(), {}, None, False

    while not stop.is_set():
        now = dt.datetime.now()
        db.conn().execute("INSERT INTO meta (key, value) VALUES ('scheduler_heartbeat', ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
        db.commit()
        rows = {r["id"]: r for r in schedules(db, enabled_only=True) if r["next"]}
        for sid in list(nexts):
            if sid not in rows or nexts[sid][0] != rows[sid]["cron"]:
                nexts.pop(sid)                      # removed, paused or edited
        for sid, r in rows.items():
            if sid not in nexts:
                nexts[sid] = (r["cron"], Cron(r["cron"]).next_after(now))
                log(f"schedule {sid} ({r['label']}): next run {nexts[sid][1]:%a %d %b %H:%M}")
            if nexts[sid][1] <= now:
                nexts[sid] = (r["cron"], Cron(r["cron"]).next_after(now))
                if sid not in due:
                    due.append(sid)
        for r in db.conn().execute("SELECT id FROM schedules WHERE run_now=1").fetchall():
            if r["id"] not in due:
                due.append(r["id"])
            schedule_set(db, r["id"], run_now=0)

        if child:
            proc, sid, logf, lock = child
            if proc.poll() is not None:
                schedule_set(db, sid, last_end=dt.datetime.now().isoformat(timespec="seconds"), last_exit=proc.returncode)
                log(f"schedule {sid} finished (exit {proc.returncode})")
                logf.close(); lock.close(); child = None
                db.conn().execute("DELETE FROM schedules WHERE id=? AND cron=''", (sid,)); db.commit()   # --background
        if not child and due:
            lock = try_scan_lock(reports)
            if lock is None:
                if not waiting_logged:
                    log("another scan is running; scheduled run waits for it")
                    waiting_logged = True
            else:
                waiting_logged = False
                sid = due.popleft()
                r = db.conn().execute("SELECT * FROM schedules WHERE id=?", (sid,)).fetchone()
                if r:
                    args = json.loads(r["args"] or "[]")
                    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
                    logpath = os.path.join(reports, "logs", f"schedule-{sid}-{stamp}.log")
                    logf = open(logpath, "w")
                    env = dict(os.environ, MH_SCHEDULE_ID=str(sid), MH_HAVE_LOCK="1")
                    # the child inherits the lock fd; the lock is held until it exits
                    proc = subprocess.Popen(["media-health", "--plain", *args], stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, env=env, start_new_session=True,
                                            pass_fds=(lock.fileno(),))
                    threading.Thread(target=_tee, args=(proc.stdout, logf, sid), daemon=True).start()
                    schedule_set(db, sid, last_start=dt.datetime.now().isoformat(timespec="seconds"),
                                 last_end=None, last_exit=None, last_log=logpath)
                    log(f"schedule {sid} ({r['label']}) started: media-health {shlex.join(args)} -> {logpath}")
                    child = (proc, sid, logf, lock)
                else:
                    lock.close()
        stop.wait(2)            # a queued interactive scan starts almost at once

    if child:
        proc, sid, logf, lock = child
        log(f"stopping: letting schedule {sid} finish its current files")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=110)
        except subprocess.TimeoutExpired:
            proc.kill()
        schedule_set(db, sid, last_end=dt.datetime.now().isoformat(timespec="seconds"), last_exit=proc.returncode)
    _prune_logs(os.path.join(reports, "logs"))
    log("scheduler stopped")
    return 0


def _tee(stream, logf, sid):
    for line in stream:
        logf.write(line); logf.flush()
        print(f"  [#{sid}] {line}", end="", flush=True)


def _prune_logs(path, days=60):
    cutoff = time.time() - days * 86400
    for f in os.listdir(path):
        fp = os.path.join(path, f)
        if f.startswith("schedule-") and os.path.getmtime(fp) < cutoff:
            os.remove(fp)


# ---------------------------------------------------------------- interactive menu

def menu(reports="/reports", media="/media"):
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, Confirm, IntPrompt
    from rich.table import Table
    from rich import box
    c = Console()
    os.makedirs(reports, exist_ok=True)
    db = DB(os.path.join(reports, "media-health.db"))

    def db_line():
        n = db.count()
        bad, warn = db.count("status='BAD'"), db.count("status='WARN'")
        mis = db.count("name_issues != '[]'")
        last = db.conn().execute("SELECT started, mode, checked FROM runs WHERE finished IS NOT NULL "
                                 "ORDER BY id DESC LIMIT 1").fetchone()
        txt = (f"[grey62]database:[/] [bold]{n:,}[/] files  ·  [bold red]{bad:,}[/] bad  ·  "
               f"[bold yellow]{warn:,}[/] warn  ·  [bold #ff8c42]{mis:,}[/] mislabelled")
        age = scheduler_status(db)
        nsched = len([r for r in schedules(db) if r["enabled"]])
        txt += (f"  ·  [grey62]scheduler[/] {'[green]on[/]' if age is not None and age < 120 else '[red]off[/]'}"
                f" [grey62]({nsched} active)[/]  ·  [grey62]GPU[/] "
                + ("[bold #76b900]NVDEC ready[/]" if gpu_available() else "[grey62]none (CPU decoding)[/]"))
        if last:
            txt += f"  ·  [grey62]last run {last['started'].replace('T', ' ')} ({last['mode']}, {last['checked']:,} checked)[/]"
        return txt

    def ask_age(prompt):
        while True:
            v = Prompt.ask(prompt, default="")
            if not v:
                return ""
            try:
                parse_age(v)
                return v
            except argparse.ArgumentTypeError as e:
                c.print(f"[red]{e}[/]")

    def guided(for_schedule=False):
        dirs = sorted(d for d in os.listdir(media) if os.path.isdir(os.path.join(media, d)) and not d.startswith("."))
        t = Table(box=None, show_header=False, padding=(0, 2))
        t.add_row("[bold #22d3ee]0[/]", f"everything under {media}")
        for i, d in enumerate(dirs, 1):
            t.add_row(f"[bold #22d3ee]{i}[/]", d)
        c.print(Panel(t, title="[bold]what to scan[/]", border_style="#3fa0ff", box=box.ROUNDED))
        while True:
            pick = Prompt.ask("Folders (numbers, comma-separated) or a path", default="0")
            paths = []
            for part in [p.strip() for p in pick.split(",") if p.strip()]:
                if part.isdigit() and int(part) <= len(dirs):
                    paths.append(media if part == "0" else os.path.join(media, dirs[int(part) - 1]))
                elif os.path.exists(part):
                    paths.append(part)
                else:
                    c.print(f"[red]not found: {part}[/]"); paths = []; break
            if paths:
                break
        c.print("\n[bold]Depth[/]  [#22d3ee]q[/] quick: probe + zeros + name check, ~1 s/file   "
                "[#22d3ee]s[/] sample: + decode 5 points   [#22d3ee]f[/] full: decode every frame (slow)")
        depth = Prompt.ask("Depth", choices=["q", "s", "f"], default="s")
        name = Prompt.ask("Name filter (text or glob, blank = all)", default="")
        newer = ask_age("Only files modified within (e.g. 2d, 3w, 6mo, 1y; blank = any age)")
        older = ask_age("Only files older than (blank = any age)")
        min_gb = Prompt.ask("Minimum size in GB (0 = any)", default="0")
        workers = IntPrompt.ask("Workers", default=2)
        mislabel = Prompt.ask("Mislabelled files count as", choices=["warn", "bad", "info", "off"], default="warn")
        force = Confirm.ask("Force a full rescan (ignore the database)?", default=False)
        argv = list(paths) + {"q": ["--quick"], "s": [], "f": ["--full"]}[depth]
        if name: argv += ["--name", name]
        if newer: argv += ["--newer-than", newer]
        if older: argv += ["--older-than", older]
        if min_gb not in ("", "0"): argv += ["--min-gb", min_gb]
        if workers != 2: argv += ["--workers", str(workers)]
        if mislabel != "warn": argv += ["--mislabel", mislabel]
        if force: argv += ["--force"]
        c.print(Panel(f"[bold]media-health {shlex.join(argv)}[/]\n[grey62]same as: docker exec media-health media-health "
                      f"{shlex.join(argv)}[/]", title="[bold]ready[/]", border_style="#22f5c8", box=box.ROUNDED))
        if for_schedule:
            return argv
        return argv if Confirm.ask("Start the scan?", default=True) else None

    def schedule_menu():
        presets = [("daily 2am", [media, "--quick"], "quick sweep of everything, every night at 2am"),
                   ("thu 3am", [media], "sampled decode check of everything, Thursdays 3am"),
                   ("daily 1am", [media, "--newer-than", "2d"], "sampled check of files from the last 2 days, nightly 1am"),
                   ("monthly 1 3am", [media, "--quick", "--force"], "full quick re-sweep ignoring the database, monthly")]
        while True:
            c.clear()
            c.print(banner(c.size.width, "schedules"))
            print_schedules(c, db)
            c.print("[bold #22d3ee]a[/] add   [bold #22d3ee]p[/] pause/resume   [bold #22d3ee]n[/] run now   "
                    "[bold #22d3ee]d[/] delete   [bold #22d3ee]b[/] back", justify="center")
            act = Prompt.ask("Action", choices=["a", "p", "n", "d", "b", ""], default="b", show_choices=False)
            if act in ("b", ""):
                return
            if act == "a":
                t = Table(box=None, show_header=False, padding=(0, 2))
                for i, (w, _, what) in enumerate(presets, 1):
                    t.add_row(f"[bold #22d3ee]{i}[/]", f"[bold]{w}[/]", what)
                t.add_row("[bold #22d3ee]c[/]", "[bold]custom[/]", "pick the scan options and the time yourself")
                c.print(Panel(t, title="[bold]new schedule[/]", border_style="#22f5c8", box=box.ROUNDED))
                pick = Prompt.ask("Choose", choices=[str(i) for i in range(1, len(presets) + 1)] + ["c"], default="1")
                if pick == "c":
                    argv = guided(for_schedule=True)
                    while True:
                        when = Prompt.ask("When? (daily 2am · thu 3am · mon,thu 03:30 · weekdays 1am · "
                                          "monthly 1 2am · hourly · cron '0 2 * * *')")
                        try:
                            cron, label = parse_when(when); break
                        except ValueError as e:
                            c.print(f"[red]{e}[/]")
                else:
                    when, argv, _ = presets[int(pick) - 1]
                    cron, label = parse_when(when)
                nxt = Cron(cron).next_after(dt.datetime.now())
                if Confirm.ask(f"Add [bold]{label}[/] ({cron}, next {nxt:%a %d %b %H:%M}): media-health {shlex.join(argv)}?",
                               default=True):
                    sid, _, _ = schedule_add(db, when, argv)
                    c.print(f"[green]added schedule {sid}[/]")
                    time.sleep(1)
                continue
            ids = [str(r["id"]) for r in schedules(db)]
            if not ids:
                continue
            sid = int(Prompt.ask("Schedule ID", choices=ids))
            if act == "p":
                cur = db.conn().execute("SELECT enabled FROM schedules WHERE id=?", (sid,)).fetchone()["enabled"]
                schedule_set(db, sid, enabled=0 if cur else 1)
            elif act == "n":
                schedule_set(db, sid, run_now=1)
                c.print("[green]queued[/] [grey62]- the scheduler starts it in a few seconds; follow it with "
                        "docker logs -f media-health[/]"); time.sleep(2)
            elif act == "d" and Confirm.ask(f"Delete schedule {sid}?", default=False):
                db.conn().execute("DELETE FROM schedules WHERE id=?", (sid,)); db.commit()

    def show_problems():
        kinds = {"1": ("status='BAD'", "BAD"), "2": ("status='WARN'", "WARN"),
                 "3": ("name_issues != '[]'", "mislabelled"), "4": ("status != 'OK'", "all problems")}
        k = Prompt.ask("Show [1] bad  [2] warn  [3] mislabelled  [4] all", choices=list(kinds), default="4")
        where, label = kinds[k]
        only = {"1": "bad", "2": "warn", "3": "mislabelled", "4": "bad,warn"}[k]

        def rescan():
            depth = Prompt.ask(f"Recheck these {total:,} file(s): [bold #22d3ee]s[/] sampled or [bold #22d3ee]f[/] full "
                               "decode (slow on CPU; --gpu on makes it fast but blind to damaged slice data)",
                               choices=["s", "f"], default="s")
            start([media, "--only", only] + (["--full"] if depth == "f" else []))
            Prompt.ask("[grey62]Enter to go back[/]", default="", show_default=False)
        total, off, page = db.count(where), 0, max(c.size.height // 4, 8)
        while True:
            rows = db.rows(where, order="CASE status WHEN 'BAD' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END, path",
                           limit=page, offset=off)
            c.clear()
            print_problems(c, rows, [media], title_extra=f" ({label})", limit=page, offset=off, total=total)
            last = off + page >= total
            act = Prompt.ask("[grey62]" + ("end of list" if last else "Enter = next page") + ", r = rescan these, "
                             "q = back[/]", choices=["", "r", "q"], default="", show_choices=False,
                             show_default=False).lower()
            if act == "r":
                return rescan()
            if act == "q" or last:
                return
            off += page

    def show_stats():
        t = Table(box=box.ROUNDED, border_style="#9b5cff", header_style="grey62", title="by status / mode")
        t.add_column("Status"); t.add_column("quick", justify="right"); t.add_column("sample", justify="right")
        t.add_column("full", justify="right")
        for st in RANK:
            t.add_row(status_tag(st), *[f"{db.count('status=? AND mode=?', (st, m)):,}" for m in DEPTH])
        v = Table(box=box.ROUNDED, border_style="#3fa0ff", header_style="grey62", title="top video formats")
        v.add_column("Resolution"); v.add_column("Codec"); v.add_column("HDR"); v.add_column("Files", justify="right")
        for r in db.conn().execute("SELECT res, vcodec, COALESCE(NULLIF(hdr,''),'SDR') h, COUNT(*) n FROM files "
                                   "GROUP BY res, vcodec, h ORDER BY n DESC LIMIT 10"):
            v.add_row(r["res"] or "?", r["vcodec"] or "?", r["h"], f"{r['n']:,}")
        runs = Table(box=box.ROUNDED, border_style="#ff2bd6", header_style="grey62", title="recent runs")
        for col in ("Started", "Mode", "Checked", "Bad", "Warn", "Mislabel", "Unchanged", "Reused", "Command"):
            runs.add_column(col, justify="right" if col not in ("Started", "Mode", "Command") else "left",
                            overflow="ellipsis", no_wrap=col == "Command")
        for r in db.conn().execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8"):
            runs.add_row((r["started"] or "").replace("T", " "), r["mode"] or "", *[f"{r[k] or 0:,}" for k in
                         ("checked", "bad", "warn", "mislabelled", "cached", "reused")], r["command"] or "")
        c.print(t, v, runs)
        Prompt.ask("[grey62]Enter to go back[/]", default="", show_default=False)

    if running_scan(reports):
        follow(reports)                 # rejoin the scan that's running
        Prompt.ask("[grey62]Enter for the menu[/]", default="", show_default=False)

    def start(argv):
        rc = launch(argv, reports)
        if rc is None:
            c.print("[yellow]the scheduler isn't running, so this scan runs in this terminal: closing it stops the "
                    "scan (start the scheduler: docker compose up -d)[/]")
            time.sleep(2)
            scan(build_parser().parse_args(argv + ["--reports", reports]), shlex.join(argv), interactive=True)

    while True:
        c.clear()
        c.print(banner(c.size.width, f"v{VERSION}  ·  interactive mode"))
        c.print(db_line(), justify="center")
        live = running_scan(reports)
        m = Table(box=None, show_header=False, padding=(0, 2))
        if live:
            m.add_row("[bold #22f5c8]w[/]", f"[bold #22f5c8]Watch the running scan[/] ({live['done']:,}/{live['queued']:,} "
                                             f"checked in {', '.join(live['args']['paths'])})")
        for key, text in [("1", "New scan (guided)"),
                          ("2", f"Quick sweep of everything ({media}, probe + zeros + names)"),
                          ("3", f"Sampled decode check of {media} (skips files already checked)"),
                          ("4", "Show problem files"),
                          ("5", "Database stats & recent runs"),
                          ("6", "Schedules (daily / weekly / cron)"),
                          ("q", "Quit")]:
            m.add_row(f"[bold #22d3ee]{key}[/]", text)
        c.print(Panel(m, title="[bold]menu[/]", border_style="#9b5cff", box=box.ROUNDED, expand=False), justify="center")
        try:
            # Enter alone just redraws: attaching to a running container shows nothing until a key is pressed
            choice = Prompt.ask("Choose", choices=["1", "2", "3", "4", "5", "6", "w", "q", ""],
                                default="", show_choices=False, show_default=False)
            argv = None
            if choice == "":
                continue
            if choice == "q":
                return 0
            if choice == "w":
                follow(reports)
                Prompt.ask("[grey62]Enter to return to the menu[/]", default="", show_default=False)
                continue
            if choice == "1":
                argv = guided()
            elif choice == "2":
                argv = [media, "--quick"]
            elif choice == "3":
                argv = [media]
            elif choice == "4":
                show_problems(); continue
            elif choice == "5":
                show_stats(); continue
            elif choice == "6":
                schedule_menu(); continue
            if argv:
                start(argv)
                Prompt.ask("[grey62]Enter to return to the menu[/]", default="", show_default=False)
        except (KeyboardInterrupt, EOFError):
            c.print()
            return 0


def main():
    argv = sys.argv[1:]
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        return menu()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.completion:
        print(completion_script(parser))
        return 0
    if args.motd:
        motd()
        return 0
    if args.serve:
        return serve(args.reports)
    if args.schedule_add or args.schedule_list or args.background or any(
            x is not None for x in (args.schedule_rm, args.schedule_pause, args.schedule_resume, args.schedule_run)):
        return schedule_cli(args, argv)
    if sys.stdin.isatty() and sys.stdout.isatty() and not args.plain:
        rc = launch(argv, args.reports)         # runs in the scheduler; this terminal watches
        if rc is not None:
            return rc
        print("note: the scheduler isn't running, so this scan runs in this terminal (closing it stops the scan)",
              file=sys.stderr)
    return scan(args, shlex.join(argv))


def schedule_cli(args, argv):
    from rich.console import Console
    c = Console()
    os.makedirs(args.reports, exist_ok=True)
    db = DB(os.path.join(args.reports, "media-health.db"))
    # the scan command = everything except --schedule-add WHEN / --background
    rest, skip = [], False
    for a in argv:
        if skip:
            skip = False; continue
        if a == "--schedule-add":
            skip = True; continue
        if a.startswith("--schedule-add=") or a in ("--background", "--bg"):
            continue
        rest.append(a)
    if args.background:
        try:
            sid = schedule_once(db, rest)
        except SystemExit as e:
            c.print(f"[red]can't queue the scan: {e}[/]")
            return 2
        c.print(f"[green]queued as background run {sid}[/]: media-health {shlex.join(rest)}\n"
                "[grey62]the scheduler starts it in a few seconds (after any running scan); watch it with media-health or "
                "docker logs -f media-health[/]")
        if (scheduler_status(db) or 999) > 120:
            c.print("[red]the scheduler isn't running, so nothing will pick this up: docker compose up -d[/]")
        return 0
    if args.schedule_add:
        try:
            sid, cron, label = schedule_add(db, args.schedule_add, rest)
        except (ValueError, SystemExit) as e:
            c.print(f"[red]can't add schedule: {e}[/]")
            return 2
        c.print(f"[green]added schedule {sid}[/]: {label} [grey50]({cron})[/] -> media-health {shlex.join(rest)}")
    for sid, kw, verb in ((args.schedule_rm, None, "deleted"), (args.schedule_pause, {"enabled": 0}, "paused"),
                          (args.schedule_resume, {"enabled": 1}, "resumed"), (args.schedule_run, {"run_now": 1}, "queued to run now")):
        if sid is None:
            continue
        if kw is None:
            n = db.conn().execute("DELETE FROM schedules WHERE id=?", (sid,)).rowcount; db.commit()
        else:
            n = schedule_set(db, sid, **kw)
        c.print(f"schedule {sid} {verb}" if n else f"[red]no schedule {sid}[/]")
    print_schedules(c, db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
