# Changelog

All notable changes to this project are documented here.

## [1.11.0] — 2026-10-13

### Added

- Download all wanted asks with a checklist. Don't ask again saves that choice, and Settings → Behavior → Ask before download all wanted turns the popup back on.
- Each album follows its Lidarr quality profile (Any, Lossless, or Standard). One format is downloaded for that album, from one user, and anything below the profile cutoff is skipped.
- Matches use the file duration as well as the filename, and a studio recording is preferred to a live or remix cut unless the Lidarr title asks for one.
- A free-slot match can be scored and downloaded before the full search wait ends.
- An album whose search never reached Soulseek is tried again after reconnect, and that artist is not put on the multi-day cooldown.
- Behavior shows formats, the artist retry period, the wait before the next artist, and conversion first. The rest is under Advanced.
- Albums per run caps how many album searches one Start will begin. Monitored and recently added artists go first. Only this Lidarr tag limits the list when it is filled in.
- After an automatic import, a Lidarr queue or history failure (a track-count mismatch or an unmatched release) is kept on the Status card, the files are moved to a Review folder, and the next search skips that Soulseek user.
- The Status card has a Details toggle for the login and server line. Downloads that are already running are not labeled Soulseek offline.
- Lidarr requests use a longer timeout, retry with a pause between tries, and lookup or paged routes instead of the full artist list. When Lidarr does not answer, the status line says Lidarr unreachable.

### Fixed

- Wanted, Search, Shared, and the Status lists stretch to the bottom of the window in Dark and Light, including after the window is resized.
- After an automatic Lidarr import, that artist's wanted albums and track files are read back from Lidarr before the next artist is searched. Albums and tracks Lidarr now has are not downloaded again. An album whose files are already queued or already in the download folder is not queued again.
- Converting a download with ffmpeg, and launching the installer, no longer opens a blank console window. On Windows 11 that window was Windows Terminal, titled with a path under AppData\Local, and it took the keyboard. Child processes are started with CREATE_NO_WINDOW and a hidden window.

### Changed

- The last automatic import result, including the Lidarr error, stays on the Status card until the next artist visit finishes.

## [1.10.0] — 2026-10-13

### Added

- The desktop app, installer, Start menu shortcut, desktop shortcut, and Add/Remove Programs entry are named Rummagarr. The setup file is `Rummagarr-Setup-<version>.exe`. The GitHub repository name is unchanged.
- Status has a checkbox, **Auto-import to Lidarr after each artist**. It is saved in `config.toml` and starts off. When it is on, and an artist queued downloads, Rummagarr asks Lidarr once — when that download queue is empty — to run DownloadedAlbumsScan on the download folder with import mode Move, then RenameFiles for that artist when Lidarr did not already rename on import. Leaving the queue empty does not scan again. The status line says `Importing <artist> into Lidarr...`, then that the import finished or failed. The scan runs beside the worker, so the next artist is not held up. Errors are written to the log.
- The window uses a dark sidebar (Status, Wanted, Search, Shared, Settings) with flat cards and dense tables. Settings has a Dark / Light theme. Dark is the default. The choice applies immediately and is saved in `config.toml`. The app and installer use the teal R icon.

### Changed

- An upgrade from Lidarr Soulseek uses the same installer id, so the new setup replaces the old one. The program mutex is unchanged, so a running 1.9.4 closes for the upgrade. The first launch copies `config.toml`, the database, and the log from `%LOCALAPPDATA%\Lidarr Soulseek` (or `~/.lidarr-soulseek`) into the Rummagarr folder when that folder has no config yet.
- Help → Check for updates accepts `Rummagarr-Setup-<version>.exe` and, if that file is not on the release, `LidarrSoulseek-Setup-<version>.exe`. A published release includes both names, so 1.9.4 can download `LidarrSoulseek-Setup-1.10.0.exe` and install this version.

## [1.9.4] — 2026-10-12

### Added

- Search → Download all wanted works with no user selected. After an artist search it reads every result, finds that artist's wanted albums and tracks, and queues the best source for each. A folder that already has every wanted song anyone is sharing for an album is kept together from that one user. Otherwise each missing song comes from another user. Free slot, then speed, then the format and bitrate from Behavior decide which source wins. A file already downloaded or queued, or the same path twice, is not queued again. The status line and the log say how many wanted items matched, from how many users, and how many were not found. Select a user, or one of their files, and the button still downloads only that user's matches. The button's hover note says which of the two it will do.

### Fixed

- The check that parallel Lidarr track lookups overlap could take about 89 seconds and fail, so the 1.9.4 release was not published. The lookups did overlap. Closing the window left its fields and checkbox images for the garbage collector. A lookup thread then deleted them, and Tcl waits one second per object for a main loop that is not running. Closing the window now drops those objects on the main thread.
- Automatic search could stay on "Waiting for retry period (next artist in 5s)" and never send another FileSearch. Manual search and downloads already queued still worked. An artist with nothing to send — a wanted row with no albums or songs, a partial album with no missing tracks, or an album already imported — still looked ready. The short wait before the next artist started over, so the countdown never reached a search. That wait now runs only after a search was sent. When it reaches 0, the next artist who has something to search is searched. If every artist is actually waiting, the status names the earliest one and counts down the real time left. A cooldown saved by an older version under an "artist:" id is included in that time. A pass that sends no search does not mark the artist as visited.

## [1.9.3] — 2026-10-12

### Fixed

- A search was never sent, and the Status tab stayed on "Starting the next artist" while still saying Soulseek was connected. Wait before next artist set to 0 returns to the worker immediately. When every artist was already on the retry cooldown, the saved list still looked ready, so that return never slept and never gave the Soulseek connection a turn. The cooldown check now uses the same artist id as the saved list. A pass that does not send a search waits 30 seconds, writes the reason, and tries again. A FileSearch that the server does not accept within 20 seconds is logged and skipped.
- The Status line says Soulseek is connected only while the server connection is up and the worker loop has had a turn in the last few seconds. Otherwise it says the session is down or the loop is behind. The line under it, and the log at each search, report login, server state, listen port, loop lag, share scan, share file count, distributed parent, and whether a search is already being sent.

## [1.9.2] — 2026-10-12

### Fixed

- A Search for a common artist stayed on "Searching…" and listed nothing. The same happened to automatic searches for wanted albums. 1.9.0 indexes the shared library. Until a distributed parent is chosen, the Soulseek server sends this client every search on the network, and answering one walks that index. Those answers ran on the same loop that sends our search and accepts the peers who reply. A large index, or a stream of those searches, never gave the loop a turn, so our search was not sent and the replies were not read. 1.9.1 paused during the share scan, which does not move those answers off the loop. Incoming share queries now run on a side thread, two at a time. Further ones are dropped. The loop keeps sending our search and reading replies. The scan still pauses between files.
- A search that finishes with nothing leaves the Search tab, instead of staying on "Searching…". The status and the log say why. Examples: not logged in, the server connection is down, the listen port is closed, the client is sharing 0 files, the share scan has not reported files yet, peers answered but every file was locked, or the search was sent and no peer answered. If Soulseek does not accept the search within 30 seconds, the status says it was not sent.
- A share report that has files but 0 folders is sent as 1 folder. Other clients ignore searches from a client that reports files in zero folders. A report of 0 files is left as 0 and written to the log. Inventing a file count is the same kind of misreport.

## [1.9.1] — 2026-10-12

### Fixed

- Behavior → Wait before next artist was already in the installed app, directly under a long list of other options. On a normal Windows window it sat below the visible area. The mouse wheel also stopped working as soon as the pointer moved onto the fields, so the page could not be scrolled to it. The control is now at the top of Behavior, directly under Artist retry period, and it still accepts 0. The mouse wheel scrolls that tab when the pointer is over the fields.
- A Search could finish with nothing to show even though peers had answered. The share scan added in 1.9.0 built its file index on the same loop that reads incoming search replies, and it did not pause while doing that. Replies sat unread until the Search tab gave up and dropped the request. The scan now lets that loop read replies while it indexes. The log line `Search 'query': N peer replies, M files shown, K locked files not shown` separates "nobody answered" from "answered, but nothing was listed". Upload slots, speed limits, "share only with users who share", and the minimum peer file count still apply only when someone downloads from you.

## [1.9.0] — 2026-10-11

### Added

- Settings → Sharing sets upload slots, how many files and how many megabytes one user may queue, a global upload speed and an optional per-user speed, a priority list, and whether to share only with users who already share files. Soulseek reports that as a file count. The same limits are enforced when a peer asks for a file, not only in the window. A user who goes past the queue can be ignored. Shared folders can be added and removed, then rescanned, and the scan reports the file and folder counts.
- The Shared tab lists uploads: user, file, size, progress, speed, status, and time. Finished uploads stay in the history. Totals count files and bytes for today and for all time. A row can be paused, resumed, or cancelled, and every upload from that user can be cancelled. Ban ignores the user. Pause all uploads holds every upload.
- Search can download every current file from a user, or only the ones that match the Wanted list. Download all wanted opens a checklist grouped by album, checked by default. Queued files are linked to the Lidarr album or track. Matching uses the same accent folding and filename threshold as automatic search, and it only reads wanted rows for artists named in that user's paths.

### Changed

- Wait before next artist on the Behavior tab is the wait in "Waiting for retry period (next artist in 5m)". It can be 0, which starts the next ready artist immediately and does not show that notice. Artist retry period is unchanged: it is the number of days to skip an artist after a visit. Both apply on the next pass. A zero wait does not spin the loop while every artist is still on that longer skip.
- The selected tab is larger, with more padding and a bold label. Inactive tabs are smaller. The selected-state expand no longer eats that padding. Main tabs, Search tabs, and Settings tabs use the same style.

### Fixed

- A shared folder was never indexed. The client listened on the configured ports, but it did not scan `share_dir`, so searches, browse requests, and uploads saw no files and the server was told the share was empty. The scan now runs after login, then reports the real counts. Listening still needs the firewall script and, when UPnP does not open the ports, a forward of both TCP ports. Pause leaves the session up. Stop closes it.
- Individual track scoring treated `.flac` and `flac` as different, so a matching audio file could be rejected as not audio. The comparison now ignores the dot.

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
