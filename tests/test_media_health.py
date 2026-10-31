import argparse
import datetime as dt
import io
import json

import pytest

import media_health as mh


@pytest.mark.parametrize("when, cron", [
    ("daily 2am", "0 2 * * *"),
    ("2:30am", "30 2 * * *"),
    ("thu 3am", "0 3 * * 4"),
    ("every thursday at 3am", "0 3 * * 4"),
    ("mon,thu 03:30", "30 3 * * 1,4"),
    ("mon and thu 3:30pm", "30 15 * * 1,4"),
    ("fri-sun 11pm", "0 23 * * 0,5,6"),
    ("weekdays 1am", "0 1 * * 1-5"),
    ("weekends 4:15am", "15 4 * * 0,6"),
    ("monthly 15 03:00", "0 3 15 * *"),
    ("hourly", "0 * * * *"),
    ("every 6h", "0 */6 * * *"),
    ("*/30 1-5 * * mon-fri", "*/30 1-5 * * mon-fri"),
])
def test_parse_when(when, cron):
    assert mh.parse_when(when)[0] == cron


@pytest.mark.parametrize("bad", ["", "sometime", "25:00", "13pm", "monthly 40 2am"])
def test_parse_when_rejects(bad):
    with pytest.raises(ValueError):
        mh.parse_when(bad)


def test_cron_next_after():
    t = dt.datetime(2026, 11, 1, 12, 0)                      # a Sunday
    assert mh.Cron("0 3 * * 4").next_after(t) == dt.datetime(2026, 11, 5, 3, 0)
    assert mh.Cron("0 2 1 * *").next_after(t) == dt.datetime(2026, 12, 1, 2, 0)
    assert mh.Cron("*/15 * * * *").next_after(t) == dt.datetime(2026, 11, 1, 12, 15)


@pytest.mark.parametrize("text, secs", [("90min", 5400), ("36h", 129600), ("2d", 172800), ("1y", 31557600)])
def test_parse_age(text, secs):
    assert mh.parse_age(text) == secs


def test_name_matcher():
    m = mh.name_matcher("sintel")
    assert m("/media/movies/Sintel (2010)/Sintel.mkv") and not m("/media/x.mkv")
    assert mh.name_matcher("*2160p*remux*")("/m/A.2160p.BluRay.REMUX.mkv")


UHD = {"res": "3840x2160", "pix_fmt": "yuv420p10le", "hdr": "HDR10", "dv_profile": "", "vcodec": "hevc",
       "audio": "truehd:,ac3:", "bitrate_mbps": 55}


def test_name_check_clean():
    assert mh.name_issues("/m/Film.2016.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.mkv", UHD) == []


def test_name_check_fake_uhd():
    r = dict(UHD, pix_fmt="yuv420p", hdr="", res="1920x1080", bitrate_mbps=8)
    issues = mh.name_issues("/m/Film.2016.2160p.UHD.BluRay.HDR.x265.DTS-GROUP.mkv", r)
    assert any("2160p" in i for i in issues)
    assert any("8-bit" in i for i in issues)
    assert any("HDR" in i for i in issues)
    assert any("DTS" in i for i in issues)


def test_mislabel_modes():
    r = dict(UHD, path="/m/Film.1080p.mkv", base_status="OK")
    assert mh.apply_name_check(dict(r), "warn")["status"] == "WARN"
    assert mh.apply_name_check(dict(r), "bad")["status"] == "BAD"
    assert mh.apply_name_check(dict(r), "info")["status"] == "OK"
    assert mh.apply_name_check(dict(r), "off")["name_issues"] == []


@pytest.mark.parametrize("line", [
    "[hevc @ 0x5f] VPS 0 does not exist",
    "[hevc @ 0x63] The cu_qp_delta 50 is outside the valid range [-32, 31].",
    "[hevc @ 0x63] CABAC_MAX_BIN : 32",
    "[h264 @ 0x1] concealing 120 DC, 120 AC, 120 MV errors in P frame",
    "[matroska,webm @ 0x5] Element at 0x1 ending at 0x2 exceeds containing master element ending at 0x3",
])
def test_serious(line):
    assert mh.SERIOUS.search(line)


@pytest.mark.parametrize("line", [
    "[hevc @ 0x5] Could not find ref with POC 12",
    "[h264 @ 0x5] reference picture missing during reorder",
    "[vc1 @ 0x5] Failed setup for format cuda: hwaccel initialisation returned error.",
])
def test_benign(line):
    assert mh.BENIGN.search(line) and not mh.SERIOUS.search(line)


def test_current_results():
    assert mh.current({"check_version": mh.CHECK_VERSION, "mode": "sample"})
    assert not mh.current({"check_version": 1, "mode": "sample"})
    assert mh.current({"check_version": mh.QUICK_SINCE, "mode": "quick"})


def test_db_roundtrip_and_once(tmp_path):
    db = mh.DB(str(tmp_path / "t.db"))
    db.save({"path": "/m/a.mkv", "size": 10, "mtime": 1, "mode": "quick", "status": "OK", "problems": ["x"]})
    db.commit()
    assert db.get("/m/a.mkv")["problems"] == ["x"]
    sid = mh.schedule_once(db, ["/m", "--quick"])
    row = next(r for r in mh.schedules(db) if r["id"] == sid)
    assert row["run_now"] == 1 and row["next"] is None and row["args"] == ["/m", "--quick"]


def test_completion_mentions_new_options():
    s = mh.completion_script(mh.build_parser())
    assert "--gpu) _mh_values auto on off" in s and "--background" in s


def _stats():
    args = mh.build_parser().parse_args(["/media/movies", "--quick", "--reports", "/r"])
    s = mh.Stats(args, "quick")
    s.found, s.queued, s.done = 10, 8, 3
    s.recent.append(("BAD", "/media/movies/x.mkv", "zeroed"))
    return s


def test_snapshot_roundtrip_draws_dashboard():
    from rich.console import Console
    snap = _stats().snapshot()
    s = mh.Stats.from_snapshot(json.loads(json.dumps(snap)))
    assert (s.done, s.decoder, s.args.paths) == (3, "none", ["/media/movies"])
    c = Console(record=True, width=120, height=40, file=io.StringIO())
    c.print(mh.Dashboard(s, c, watching=True))
    text = c.export_text()
    assert "x.mkv" in text and "s stop the scan" in text


def test_status_file_and_stop_request(tmp_path):
    s = _stats()
    mh.write_status(str(tmp_path), s, 7)
    assert mh.running_scan(str(tmp_path))["run"] == 7
    (tmp_path / ".stop-7").touch()
    mh.write_status(str(tmp_path), s, 7)
    assert s.stopping
    s.phase = "done"
    mh.write_status(str(tmp_path), s, 7)
    assert mh.running_scan(str(tmp_path)) is None and not (tmp_path / ".stop-7").exists()


def test_stale_status_is_not_running(tmp_path, monkeypatch):
    mh.write_status(str(tmp_path), _stats(), 1)
    monkeypatch.setattr(mh.time, "time", lambda: 1e12)
    assert mh.running_scan(str(tmp_path)) is None


SD8 = {"res": "1920x1080", "pix_fmt": "yuv420p", "hdr": "", "dv_profile": "", "vcodec": "hevc",
       "audio": "truehd:,ac3:", "bitrate_mbps": 9}


@pytest.mark.parametrize("name, r, want", [
    # fake UHD: resolution corrected; UHD, 10bit and HDR dropped (no "8bit"/"SDR" added); main audio replaced,
    # channel layout kept, secondary AC3 track not added
    ("Sintel.2010.2160p.UHD.BluRay.x265.10bit.HDR.DTS-HD.MA.5.1-GROUP.mkv", SD8,
     "Sintel.2010.1080p.BluRay.x265.TrueHD.5.1-GROUP.mkv"),
    # wrong video codec replaced
    ("Big.Buck.Bunny.2008.1080p.BluRay.REMUX.AVC.DTS-HD.MA.5.1-GRP.mkv",
     dict(SD8, vcodec="vc1", audio="dts:DTS-HD MA,ac3:", bitrate_mbps=30),
     "Big.Buck.Bunny.2008.1080p.BluRay.REMUX.VC-1.DTS-HD.MA.5.1-GRP.mkv"),
    # missing DV and Atmos dropped, TrueHD -> the DTS-HD MA that's really there; spaces kept as spaces
    ("Tears of Steel 2012 2160p UHD BluRay HDR DV x265 TrueHD 7.1 Atmos-GROUP.mkv",
     dict(SD8, res="3840x2160", pix_fmt="yuv420p10le", hdr="HDR10", audio="dts:DTS-HD MA", bitrate_mbps=40),
     "Tears of Steel 2012 2160p UHD BluRay HDR x265 DTS-HD MA 7.1-GROUP.mkv"),
    # " - " in a title is left alone
    ("Spider-Man - Into the Spider-Verse (2018) 720p WEB-DL DDP5.1 x264.mkv", dict(SD8, vcodec="h264", audio="eac3:"),
     "Spider-Man - Into the Spider-Verse (2018) 1080p WEB-DL DDP5.1 x264.mkv"),
    # REMUX that's an encode loses REMUX
    ("Agent.327.2017.1080p.BluRay.REMUX.x264.DD.5.1.mkv", dict(SD8, vcodec="h264", audio="ac3:", bitrate_mbps=6),
     "Agent.327.2017.1080p.BluRay.x264.DD.5.1.mkv"),
])
def test_suggest_name(name, r, want):
    got = mh.suggest_name("/m/" + name, r)
    assert got == want
    assert mh.name_issues("/m/" + got, r) == []


def test_suggest_name_nothing_to_fix():
    assert mh.suggest_name("/m/Film.2016.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.7.1.mkv", UHD) is None


def test_parse_only():
    assert mh.parse_only("bad, WARN") == ["bad", "warn"]
    for bad in ("", "ok", "bad,nope"):
        with pytest.raises(argparse.ArgumentTypeError):
            mh.parse_only(bad)


def test_flagged(tmp_path):
    db = mh.DB(str(tmp_path / "t.db"))
    for p, st, ni in [("/media/movies/a.mkv", "BAD", []), ("/media/movies/b.mkv", "WARN", ["x"]),
                      ("/media/movies/c.mkv", "OK", []), ("/media/tv/d.mkv", "BAD", []),
                      ("/media/movies2/e.mkv", "BAD", [])]:
        db.save({"path": p, "size": 1, "mtime": 1, "mode": "quick", "status": st, "name_issues": ni})
    db.commit()
    assert mh.flagged(db, ["bad"], ["/media/movies"]) == ["/media/movies/a.mkv"]
    assert mh.flagged(db, ["bad", "mislabelled"], ["/media/movies/"]) == ["/media/movies/a.mkv", "/media/movies/b.mkv"]
    assert mh.flagged(db, ["bad"], ["/media/tv/d.mkv"]) == ["/media/tv/d.mkv"]


def test_decoder_label(monkeypatch):
    p = mh.build_parser()
    monkeypatch.setitem(mh._GPU, "ok", True)
    assert mh.decoder_label(p.parse_args(["/m"])) == "cpu+gpu"
    assert mh.decoder_label(p.parse_args(["/m", "--full"])) == "cpu"
    assert mh.decoder_label(p.parse_args(["/m", "--gpu", "on"])) == "gpu"
    assert mh.decoder_label(p.parse_args(["/m", "--quick"])) == "none"
    monkeypatch.setitem(mh._GPU, "ok", False)
    assert mh.decoder_label(p.parse_args(["/m"])) == "cpu (no GPU)"
    assert mh.decoder_label(p.parse_args(["/m", "--gpu", "off"])) == "cpu"
