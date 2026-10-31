# Changelog

## [Unreleased]

## [3.2.0] - 2026-11-01

### Added
- `--gpu auto|on|off`: NVIDIA NVDEC decoding. `auto` decodes the start of each file on the CPU, which still catches
  damaged slice data and measures decoder memory, and the other points on the GPU. Sampled scans of 4K HEVC go from
  ~70–80 s to ~7 s per file on an RTX 4060. Enable it with `docker-compose.gpu.yml`.
- Interactive scans run in the scheduler, and your terminal watches them. Leaving (`q`, Ctrl+C, a dropped
  connection) doesn't stop the scan. Running `media-health` again, or **w** in the menu, brings its live dashboard
  back, and `s` stops it. `--background` (`--bg`) queues a scan without watching it. The scheduler now checks for
  queued scans every 2 s instead of every 15 s.
- `.env` configuration (`MEDIA_PATH`, `PUID`/`PGID`, `TZ`, `MEM_LIMIT`, `CPUS`), `pyproject.toml`, tests, CI and
  release workflows, and README screenshots generated from demo data.

### Changed
- Decode points start at the keyframe before the point (`-noaccurate_seek`). That is 2–3× less CPU work per point on
  long-GOP 4K.
- A decoder that reports 100 serious errors at one point is stopped, and the file is BAD. Headerless streams took
  3–10 minutes each; they now take seconds.
- `CHECK_VERSION` 2: sampled and full results are rechecked once. Quick results stay valid.
- The `scan` compose service is gone. Use `docker exec -it media-health media-health …`.

### Fixed
- Damaged slice data (`cu_qp_delta … outside the valid range`, `CABAC_MAX_BIN`, `ac-tex damaged`, …) and Matroska
  element overruns now count as serious errors. Before, they were only noted as "minor decoder messages", so a
  visibly corrupt file could pass as OK.
- ffmpeg output like `[hevc @ 0x…]` was read as rich markup and disappeared from the dashboard and problem lists.
- The dashboard footer suggested Ctrl+P Ctrl+Q to detach, but a `docker exec` session can't be reattached.

### Removed
- The one-off import of the v1/v2 `state.json` cache.

## [3.1.0] - 2026-10-31

- SQLite database instead of `state.json`, fingerprint reuse for renamed/hardlinked files, name-check
  (mislabelled files), scheduler with plain-English schedules, interactive menu, live dashboard, bash completion.

## [2.x] and [1.x] - 2026-10-31

- Earlier versions: a single script with a JSON cache and probe + zero-block + decode-sample checks.
