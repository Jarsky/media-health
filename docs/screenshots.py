"""Regenerate the README screenshots (SVG) from a demo database: python docs/screenshots.py
Uses fictional/open-movie titles, so it never shows your own library."""
import argparse, io, os, sys, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rich.console                                             # noqa: E402
import media_health as mh                                       # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "screenshots")
W, H = 120, 44
_Real = rich.console.Console


def rec(**kw):
    return _Real(record=True, width=W, height=H, force_terminal=True, color_system="truecolor", file=io.StringIO())


def save(c, name, title):
    c.save_svg(os.path.join(OUT, f"{name}.svg"), title=title)
    print("wrote", name)


FILMS = [
    ("Big Buck Bunny (2008)/Big.Buck.Bunny.2008.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.mkv", "OK", [], []),
    ("Sintel (2010)/Sintel.2010.2160p.UHD.BluRay.x265.10bit.HDR.DTS-HD.MA.5.1-GROUP.mkv", "BAD",
     ["decoder stopped after 100 errors at 21m (224 MB) - stream won't decode",
      "104 decode errors (start: [hevc @ 0x63ed] VPS 0 does not exist)"], []),
    ("Tears of Steel (2012)/Tears.of.Steel.2012.2160p.UHD.BluRay.HDR.x265.DTS-GROUP.mkv", "WARN", [],
     ["says UHD BluRay but video is 8-bit (yuv420p, SDR) - not a real UHD disc source", "says HDR but video is SDR"]),
    ("Elephants Dream (2006)/Elephants.Dream.2006.1080p.BluRay.REMUX.AVC.LPCM.2.0.mkv", "BAD",
     ["zeroed 1MiB blocks at [0.412, 0.471]"], []),
    ("Cosmos Laundromat (2015)/Cosmos.Laundromat.2015.1080p.WEB-DL.DDP5.1.Atmos.x264.mkv", "WARN",
     ["7 decode errors (38m: [h264 @ 0x55d1] concealing 1200 DC, 1200 AC, 1200 MV errors in P frame)"], []),
    ("Spring (2019)/Spring.2019.2160p.WEB-DL.DV.HDR.HEVC.DDP5.1.mkv", "OK", [], []),
    ("Agent 327 (2017)/Agent.327.2017.720p.BluRay.x264.AAC.mkv", "WARN", [], ["says 720p but video is 1920x1080"]),
]


def demo_db(path):
    db = mh.DB(path)
    for p, st, probs, names in FILMS:
        db.save({"path": f"/media/movies/{p}", "size": 30_000_000_000, "mtime": 1, "mode": "sample", "status": st,
                 "base_status": "OK" if names and not probs else st, "problems": probs, "name_issues": names,
                 "res": "3840x2160", "vcodec": "hevc", "hdr": "HDR10", "check_version": mh.CHECK_VERSION,
                 "checked": "2026-11-01T02:00:00"})
    for n, res, codec, hdr in [(2400, "1920x1080", "h264", ""), (1650, "3840x2160", "hevc", "HDR10"),
                               (900, "1920x1080", "hevc", ""), (310, "3840x2160", "hevc", "")]:
        for k in range(n // 50):
            db.save({"path": f"/media/tv/{codec}{res}{hdr}{k}.mkv", "size": 1, "mtime": 1, "mode": "quick",
                     "status": "OK", "res": res, "vcodec": codec, "hdr": hdr, "checked": "2026-11-01T02:00:00"})
    db.conn().execute("INSERT INTO runs (started, finished, mode, command, checked, ok, warn, bad, mislabelled, cached, reused)"
                      " VALUES ('2026-11-01T02:00:00','2026-11-01T02:41:10','sample','/media --newer-than 2d',"
                      "412, 404, 6, 2, 3, 5288, 17)")
    db.conn().execute("INSERT INTO meta VALUES ('scheduler_heartbeat', ?)", (str(time.time()),))
    mh.schedule_add(db, "daily 2am", ["/media", "--quick"])
    mh.schedule_add(db, "thu 3am", ["/media"])
    mh.schedule_add(db, "daily 1am", ["/media", "--newer-than", "2d"])
    db.commit()
    return db


def dashboard():
    args = mh.build_parser().parse_args(["/media", "--workers", "4"])
    mh._GPU["ok"] = True
    s = mh.Stats(args, "sample")
    s.phase, s.found, s.queued, s.done, s.cached, s.reused, s.filtered = "scanning", 5449, 3712, 1288, 1720, 17, 0
    s.counts.update(OK=1261, WARN=21, BAD=6); s.mislabelled = 14; s.bytes_done = 31.4e12
    s.start = s.scan_start = time.time() - 3 * 3600 - 412
    for p, st, probs, names in FILMS[1:5]:
        s.recent.append((st, p, mh.first_problem({"problems": probs, "name_issues": names})))
    now = time.time()
    for i, (p, stage, info) in enumerate([
            (FILMS[0][0], "decode 3/5 @ 42m gpu", "3840x2160 hevc HDR10"),
            (FILMS[5][0], "decode 1/5 @ start", "3840x2160 hevc HDR10 DV8"),
            (FILMS[6][0], "zero-check", "1920x1080 h264"),
            (FILMS[4][0], "decode 5/5 @ 86m gpu", "1920x1080 h264")]):
        mh.ACTIVE[i] = {"path": p, "start": now - 3 - i * 4, "stage": stage, "info": info, "size": (40 - i * 9) * 1e9}
    d = mh.Dashboard(s, rec(), watching=True)
    d.console.print(d)
    save(d.console, "dashboard", "media-health · live scan")
    mh.ACTIVE.clear()


def patched(fn, name, title, stdin=""):
    c = rec()
    rich.console.Console = lambda *a, **k: c
    sys.stdin = io.StringIO(stdin)
    try:
        fn()
    except (EOFError, KeyboardInterrupt, SystemExit):
        pass
    finally:
        rich.console.Console = _Real
        sys.stdin = sys.__stdin__
    save(c, name, title)


def main():
    ap = argparse.ArgumentParser(); ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = demo_db(os.path.join(tmp, "media-health.db"))
        dashboard()
        patched(lambda: mh.menu(reports=tmp, media="/media"), "menu", "media-health · menu", "q\n")
        patched(lambda: mh.print_problems(rich.console.Console(), db.rows("status != 'OK'",
                order="CASE status WHEN 'BAD' THEN 0 ELSE 1 END, path"), ["/media/movies"]),
                "problems", "media-health · problem files")
        patched(lambda: mh.print_schedules(rich.console.Console(), db), "schedules", "media-health · schedules")
        patched(lambda: mh.rich_help(mh.build_parser()), "help", "media-health -h")


if __name__ == "__main__":
    main()
