# Changelog

All notable changes to this project are documented here.

## [1.3.0] — 2026-10-10

Desktop app and Windows installer.

### Added

- `LidarrSoulseek.exe` with a Status tab and a Wanted tab. The Status tab shows the same live transfers, cancel actions, and recent-history size choices as the page on port 8787, plus Start, Pause, Stop, and Skip.
- Pause holds the current download and the saved place in line. Resume continues it. Skip drops the item in progress and moves to the next one. Stop ends the run and keeps the database, wanted checkboxes, and artist place.
- Wanted tab: Sync loads Lidarr's wanted/missing list into memory. Every artist, album, and song has a checkbox. Select all follows the current view. Views are Everything, Incomplete artists, Incomplete albums, and Incomplete songs. Unchecked rows are stored in `state.sqlite3` and are the only ones the worker searches. A fresh install with no saved choices still searches everything.
- First-run setup window for the Lidarr URL, API key, Soulseek username and password, and folders. Settings in the app writes `config.toml`.
- Inno Setup installer (`scripts/build_windows.ps1`) with a Start Menu shortcut. The `Windows installer` workflow builds it. A `v<version>` tag, or a manual run of that workflow on `main`, publishes `LidarrSoulseek-Setup-<version>.exe` on the GitHub Release. Pull requests only upload a workflow artifact.

### Changed

- The status snapshot honors the recent-history limit, and completed downloads are kept up to 250 instead of 50.

## [1.2.0] — 2026-10-09

Big-list pass so a huge Lidarr wanted list spends less time building and more time downloading.

### Added

- `artist_cooldown_days` (default 7). After one visit, that artist is skipped for a week.
- `max_missing_pages` (default 20). Each cycle reads only that many Lidarr wanted/missing pages (page size 250). The next cycle continues, then wraps to page 1.

### Changed

- One artist per cycle. The worker no longer stays on the same artist until every album is done.
- Wanted-list scan no longer fetches every album's tracks. Partial-track fill is limited to the focused artist.
- Artist focus still walks A–Z, skipping anyone on cooldown.

### Upgrade

1. Stop the worker (Ctrl+C, or stop the scheduled task).
2. Copy `worker.py`, `state.py`, `config_loader.py`, `lidarr_client.py`, and `check.py` over your install. Keep `config.toml`.
3. Add under `[search]`:

```toml
artist_cooldown_days = 7
max_missing_pages = 20
```

4. Start one copy only. Ports 2234 and 2235 cannot be shared.

## [1.1.0] — 2026-09-24

First feature release after the initial Windows package.

### Added

- Local status page at `http://127.0.0.1:8787` with live transfers, cancel buttons, and the last 10 completed downloads.
- Track-level Soulseek search for albums that already have some files in Lidarr (`fill_missing_tracks`).
- Artist focus mode so the worker finishes one artist before hopping (`focus_one_artist`).
- Alphabetical artist order when picking work.
- Concurrent album searches (`concurrent_albums`).
- Incremental FLAC/WAV → MP3 conversion as each file finishes, not after the whole album.
- Incomplete-folder cleanup on failed or cancelled downloads (`clean_incomplete_on_fail`).
- Age sweep of leftover Incomplete files (`incomplete_max_age_hours`, default 12).
- After a successful import, ask Lidarr to **RenameFiles** for that artist so tracks move into the library naming scheme.
- Quiet aioslsk peer-connect warnings so the log is readable.

### Changed

- Full-album Soulseek grabs only run when Lidarr has **zero** files for that album. Partial albums use track fill instead of re-downloading the folder.
- Lidarr wanted/missing is paged until empty instead of stopping at the first 250 records.
- SQLite state is safe across the status-page thread and the worker thread.
- Preferred extension order and conversion settings are documented in `config.example.toml`.

### Fixed

- Duplicate TOML keys no longer sneak into the example config.
- Status page no longer crashes with `SQLite objects created in a thread can only be used in that same thread`.
- Failed downloads no longer pile up in Incomplete for days.

### Upgrade

1. Stop the scheduled task.
2. Copy the new `.py` files over your install. Keep your existing `config.toml`.
3. Add any new keys you want from `config.example.toml` (they have safe defaults if omitted).
4. Start the task again.

## [1.0.0] — 2026-08-29

Initial public package: Windows-native Lidarr + aioslsk worker, no Docker, no slskd.
