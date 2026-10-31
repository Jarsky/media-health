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
