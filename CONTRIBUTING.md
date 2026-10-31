# Contributing

Issues and pull requests are welcome. Bug reports are most useful with:
- the `media-health` command you ran, and the line for the file from `reports/latest.csv`
- `ffprobe -v error -show_streams -show_format <file>` output, if a check looks wrong

## Git workflow

- `main` is always releasable. CI (lint, tests, Docker build + smoke scan) must pass before merging.
- Work on a short-lived branch named `feat/…`, `fix/…`, `docs/…` or `chore/…`, and open a PR into `main`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `feat: --gpu auto`,
  `fix: escape ffmpeg output in the dashboard`, `docs: GPU benchmark`.
- Squash-merge PRs so `main` gets one commit per change.

```bash
git switch -c fix/some-bug
# ... edit, then:
ruff check . && pytest
git commit -am "fix: some bug"
git push -u origin fix/some-bug        # open the PR on GitHub
```

## Changing the checks

- Changed what the probe, zero-block or decode checks find? Bump `CHECK_VERSION` in `media_health.py`. Every stored
  result older than that is then rechecked. If the quick checks (probe, zero blocks) didn't change, leave
  `QUICK_SINCE` alone so a library's quick results stay valid.
- Changed the name-check rules? Bump `NAME_VERSION`. Those are re-evaluated from stored stream details, without rescanning.
- New decoder messages go in `SERIOUS` or `BENIGN`, with a test line in `tests/test_media_health.py`.
- UI changes: run `python docs/screenshots.py` and commit the updated SVGs.

## Releases

1. Update `VERSION` in `media_health.py` ([semver](https://semver.org/): new check or option = minor, fix = patch).
2. Move the `Unreleased` notes in `CHANGELOG.md` under the new version.
3. Commit `chore: release vX.Y.Z`, then tag and push:
   ```bash
   git tag -a vX.Y.Z -m vX.Y.Z && git push origin main --tags
   ```
   The release workflow checks that the tag matches `VERSION`. It then publishes `ghcr.io/jarsky/media-health:X.Y.Z`
   (+ `X.Y` and `latest`) and creates the GitHub release from the changelog.
