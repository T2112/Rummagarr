# Changelog

All notable changes to this project are documented here.

## [1.8.0] — 2026-10-11

### Added

- The Status tab names what the worker is doing. It stays on Idle only when nothing is selected. A search shows the artist and album, which query is running, how many results have arrived, and how long the step has taken. The same text is on the browser status page.
- Search is a new tab for typing a Soulseek query yourself. Results show up while they arrive, grouped by user, with speed, free slot or queue, size, bitrate, length, and format. Filter the list, then download a file or a folder into the same queue Lidarr already imports from. It uses the worker's Soulseek login.
- Each Wanted row has a magnifying glass. Click it to open Search with that artist, album, or song already filled in. A download from that search is tied to the Lidarr album or track.

## [1.7.1] — 2026-10-11

### Fixed

- Wanted checkboxes respond to a click. The handler asked which part of the row was clicked with the wrong arguments, so Tk discarded every click on a box. Each row now draws a checkbox image, which shows on Windows 10 and 11 without a special font. Click the box to toggle it. Click the rest of the row to select it. Space toggles the selected row. A click still saves while Sync is running, and the next page does not turn it back on. An artist box checks or unchecks that artist's albums and songs, and shows a mixed mark when only some of them are checked.

## [1.7.0] — 2026-10-11

### Changed

- The Wanted list is stored in `state.sqlite3` and opens from that file. Launch does not call Lidarr. Sync walks Lidarr page by page and writes each page as it arrives, so the list can be searched and checked while the rest is still loading. A later Sync reads the wanted/missing pages again, because that API has no changed-since filter, and requests tracks only for albums whose album-level fields changed. Those lookups are one request per artist (`GET /api/v1/track?artistId=`) and run several at a time. A cancelled or crashed Sync continues where it stopped. Behavior → Lidarr requests at once sets that concurrency. Unchanged rows are not rewritten, and albums Lidarr no longer returns are removed when the page walk finishes.
- The Wanted tab asks the database for the rows on screen. Filters, sorting, the artist search box, and the selected counts are SQL queries. Select all for the current view is one update, and scrolling asks for the next page by key instead of skipping millions of rows. The worker picks the next checked artist from an index instead of loading the list into memory.

## [1.6.0] — 2026-10-10

### Fixed

- The Wanted tab no longer locks up the window on a large Lidarr list. Sync runs off the UI thread, shows progress such as "Syncing 45,000 / 100,000", and can be cancelled.
- The list only draws the rows on screen. Checkboxes, Select all, the artist / album / song filters, and column sorting stay, and they no longer walk the whole list once per row.
- Selections are saved in one database transaction. The worker looks up the focused artist's albums and songs from an index instead of rescanning the full wanted list on every pass.

## [1.5.0] — 2026-10-10

### Added

- Soulseek searches fold accented and symbolic names to ASCII (`Mötley Crüe` is also searched as `Motley Crue`, and the same for Beyoncé, Sigur Rós, Queensrÿche, Blue Öyster Cult, Björk, P!nk, Ke$ha, and `&` / `and`). The original spelling is searched too when it differs, and up to three Lidarr artist aliases already on the album are extra search terms. Match scoring uses the same folding. A folder that is a different artist still fails.
- Settings has a Behavior tab for the options that used to be config-file only: concurrent albums, the artist retry period, quality and format preferences, ffmpeg conversion, polling, timeouts, the status page address, history size, and the update check. Each field has a range and a short explanation. Restore defaults puts them back without clearing the login. Search and download changes apply on the next pass. Connection, folders, and the status page ask you to stop and start the worker.

## [1.4.1] — 2026-10-10

### Fixed

- Wanted sync no longer fails when Lidarr sends a non-numeric track number such as `A1`, `B12`, or `1a`. Those numbers stay on the row and sort by side, then number. A row with no usable id is logged and skipped, and the rest of the sync still finishes.

## [1.4.0] — 2026-10-10

The installed app can update itself from GitHub Releases.

### Added

- On launch, the desktop app checks https://api.github.com/repos/T2112/lidarr-soulseek/releases/latest in the background. Help → Check for updates runs the same check and says when this copy is already current.
- A newer release opens a dialog with the version and release notes. Update now downloads `LidarrSoulseek-Setup-<version>.exe`, checks the file size and the published SHA-256, stops the worker, and runs a silent upgrade. The installer starts the app again. Later asks next time. Skip this version is remembered.
- Settings includes "Check for updates when the app opens", on by default. A copy started from source reports the new version and opens the release page instead of running the installer.
- The window title, the header, and Help → About show the current version.

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
