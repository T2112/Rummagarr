# Seekarr

**Version 1.10.0** — Windows desktop app. No Docker. No slskd. The GitHub repository is still [T2112/lidarr-soulseek](https://github.com/T2112/lidarr-soulseek).

The worker reads Lidarr **Wanted → Missing**, searches Soulseek, downloads matching albums or individual missing tracks, converts FLAC/WAV to MP3 when needed, asks Lidarr to import and rename, then cleans leftover files.

Custom Lidarr import list (MusicBrainz IDs):  
https://raw.githubusercontent.com/T2112/lidarr-soulseek/main/lists/lidarr-custom-list.json

## Download / Install

The Windows installer is on the [latest release](https://github.com/T2112/lidarr-soulseek/releases/latest). No GitHub account is required.

Run `Seekarr-Setup-<version>.exe`. It installs the app for your user and adds a Start Menu shortcut and a desktop shortcut, both named Seekarr. The first window asks for the Lidarr URL, API key, Soulseek username and password, and the folders. An older Lidarr Soulseek install is upgraded in place.

ffmpeg is separate if you want FLAC converted to MP3:

```powershell
winget install Gyan.FFmpeg
```

## Desktop app

Most people should install the desktop app instead of setting up Python.

1. Download `Seekarr-Setup-<version>.exe` from the [latest release](https://github.com/T2112/lidarr-soulseek/releases/latest).
2. Run the installer. It copies the app into your user programs folder and adds a **Start Menu** shortcut and a **desktop** shortcut, both named Seekarr. Add/Remove Programs lists it as Seekarr. An optional task can also start the app when you sign in. If Lidarr Soulseek is already installed, this installer replaces it.
3. The first window asks for the Lidarr URL, the Lidarr API key, the Soulseek username and password, and the Incomplete / Complete folders. Use **Test Lidarr**, then **Save**. You do not edit `config.toml` by hand.
4. Open **Status**. **Start** begins searching. The line under the buttons names the current search, wait, or download, and it says Idle only when nothing is selected. **Pause** holds the current work and resumes it later. **Stop** ends the run and keeps your place. **Skip** drops the album or song it is on and moves to the next one. **Auto-import to Lidarr after each artist** is off until you check it. When it is on, an empty download queue after an artist asks Lidarr to import and rename that artist's files.
5. Open **Wanted**. The saved list opens immediately. **Sync** reads Lidarr and fills the list as each page arrives; you can search and check rows while that is still running. **Artist** filters by name. Uncheck anything the worker should leave alone. **Select all** applies to the current view: Everything, Incomplete artists, Incomplete albums, or Incomplete songs. The magnifying glass on a row opens **Search** for that artist, album, or song. The list and the checkboxes are kept in the local database.
6. Open **Search** to type a Soulseek query yourself. Results are grouped by user. Download sends the file through the same queue, conversion, and Lidarr import as an automatic grab. Search uses the login from **Start** and does not sign in a second time.

The browser page at `http://127.0.0.1:8787` still works while the worker is running. The desktop window does not need a browser.

The installed app checks GitHub Releases when it opens. **Help → Check for updates** runs that check immediately. A newer version can be installed from the dialog. **Skip this version** remembers that release, and Settings → Behavior can turn the automatic check off. A copy started from source only shows the new version and opens the release page.

**Settings → Behavior** edits the rest of `config.toml`: how many albums run at once, how long an artist is skipped, formats, MP3 conversion, timeouts, and the status page. Restore defaults puts those options back. Search and download changes apply on the next pass. Login, folders, and the status page address apply after you stop and start the worker.

The version is on the window title and under **Help → About**.

ffmpeg is still separate if you want FLAC converted to MP3: `winget install Gyan.FFmpeg`. The Status tab says so when it is missing.

Config, the database, and the log for an installed copy live in `%LOCALAPPDATA%\Seekarr\`. The first launch after an upgrade copies them from `%LOCALAPPDATA%\Lidarr Soulseek\` when the new folder has no `config.toml` yet, and leaves the old folder in place.

### Build the installer

On a Windows machine, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

That installs the Python dependencies and PyInstaller, freezes `desktop_app.py`, and compiles `packaging\LidarrSoulseek.iss` with Inno Setup 6. If Inno Setup is not already installed, the script downloads it. The setup program is written to:

```text
dist\Seekarr-Setup-1.10.0.exe
```

The same steps run in GitHub Actions (`.github/workflows/windows-installer.yml`) on `windows-latest`. Pull requests and pushes to `main` upload the installer as a workflow artifact.

To publish a public download, push a tag `v<version>` that matches the `VERSION` file (for example `v1.10.0`). That run creates a GitHub Release and attaches `Seekarr-Setup-<version>.exe` and the same file as `LidarrSoulseek-Setup-<version>.exe`, so an installed 1.9.4 can still update. You can also publish the version already in `VERSION` without making the tag yourself: **Actions → Windows installer → Run workflow**, and choose the `main` branch. If that release already exists, the workflow replaces the installer files.

This cannot be cross-compiled from Linux. PyInstaller builds for the OS it runs on, and Inno Setup is a Windows program. Push the branch and let that workflow produce the `.exe`.

`python main.py` still runs the worker without the window, for the original script install below.

## What's new in 1.10.0

- The app is named Seekarr: the window title, About box, Start menu shortcut, desktop shortcut, and Add/Remove Programs entry. The installer is `Seekarr-Setup-<version>.exe`. An existing Lidarr Soulseek install upgrades in place, and its settings are copied into the Seekarr data folder.
- Status has **Auto-import to Lidarr after each artist**. It is off until checked. When it is on, and the download queue empties after an artist finishes, Lidarr imports that download folder and renames the artist's files. The status line says when that import is running, done, or failed.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.9.4

- Search → Download all wanted no longer needs a selected user. It matches that artist's wanted albums and tracks across every result, keeps one complete folder when a user has it, and fills any missing tracks from other users. The status line reports how many matched, from how many users, and how many were not found. A selected user still limits the download to that user.
- Automatic search no longer sits on "Waiting for retry period (next artist in 5s)" when the next artist has nothing to send. The countdown reaches a real search. If every artist is waiting, the status names the earliest one and the real time left.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.9.3

- Search is sent again when Wait before next artist is 0. A pass that has nobody ready waits and retries instead of sitting on "Starting the next artist".
- Status says Soulseek is connected only when the server connection is up and the worker loop is responding. The line under it shows login, loop lag, and the share scan.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.9.2

- Soulseek search works again after the sharing scan. Answering other people's searches no longer blocks the loop that sends our search and reads the replies.
- A search with nothing to show leaves "Searching…" and says why, in the Search tab and in the log. That includes not logged in, a closed listen port, sharing 0 files, and no peer answering.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.9.1

- Behavior → Wait before next artist is at the top of that tab, under Artist retry period. 0 still starts the next ready artist immediately. The mouse wheel scrolls the tab.
- Search keeps peer replies that arrive while a share scan is indexing. The log counts replies received against files shown.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.9.0

- Shared folders are scanned after login, so other Soulseek users can browse and download them while the worker is running. Pause keeps that up. Stop closes it. `open-firewall.ps1` opens the Windows firewall; the router still has to forward both listen ports if UPnP does not.
- Behavior → Wait before next artist is the "Waiting for retry period" notice. Set it to 0 to start the next ready artist immediately. Artist retry period is still the multi-day skip after a visit.
- The active tab is larger. Inactive tabs are smaller. That applies to the main tabs, Search tabs, and Settings tabs.
- Shared lists uploads, history, and totals, with pause, cancel, and ban. Settings → Sharing enforces queue, speed, and peer-share limits.
- Search can download every wanted file a user has, after a checklist grouped by album.
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.8.0

- Status names the current search, wait, download, or retry instead of sitting on Idle
- A Search tab runs a Soulseek query on the same login and can queue the files it finds
- Wanted rows have a magnifying glass that starts that search for the artist, album, or song
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.7.1

- Wanted checkboxes are drawn boxes and toggle when you click them, including while Sync is still running
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.7.0

- The Wanted list is saved in the local database and opens without another Sync
- Sync writes each Lidarr page as it arrives, and the list stays searchable while that runs
- A later Sync asks Lidarr for track titles only when that album's wanted/missing fields changed
- An artist search box filters the saved list
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.6.0

- Wanted sync stays responsive on a list of about 100,000 songs, with progress and Cancel
- The Wanted tab only draws the visible rows, so Select all and the filters stay quick
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.5.0

- Soulseek search folds accented names (`Mötley Crüe`, Beyoncé, Björk) and symbols (`P!nk`, `Ke$ha`, `&`) so the ASCII filename is still found
- Settings → Behavior exposes the config-file options, with Restore defaults
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.4.1

- Wanted sync keeps vinyl-style track numbers (`A1`, `B12`) instead of failing the whole list
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.4.0

- The installed app updates itself from GitHub Releases
- Help → Check for updates, with Update now, Later, and Skip this version
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.2.0

- One artist per visit, then that artist is skipped for 7 days (`artist_cooldown_days`)
- Wanted/missing is read 20 pages at a time and resumes next cycle (`max_missing_pages`)
- Partial-track fill is limited to the focused artist instead of scanning the whole library
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## What's new in 1.1.0

- Status page with live transfers, cancel, and recent completions
- Track fill for partial albums (does not re-download albums Lidarr already has)
- Convert each lossless file as soon as it finishes
- Concurrent album searches
- A–Z artist order and optional one-artist focus
- Incomplete folder cleanup on failure plus a 12-hour sweep
- Lidarr `RenameFiles` after a successful import
- Full changelog: [CHANGELOG.md](CHANGELOG.md)

## Requirements

- Windows 10 or 11
- Lidarr already running on this PC
- A Soulseek account
- Python 3.11+ from https://www.python.org/downloads/  
  Tick **Add python.exe to PATH**
- ffmpeg (`winget install Gyan.FFmpeg`) if you want FLAC converted to MP3

## Install

1. Unzip this folder somewhere permanent, for example `C:\Tools\lidarr-soulseek`.  
   A network drive like `Z:` works but PowerShell may block scripts there.
2. Open PowerShell **in this folder**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install.ps1
```

3. Edit `config.toml`. Every text value stays in double quotes.

```toml
[lidarr]
url = "http://127.0.0.1:8686"
api_key = "your_lidarr_api_key"
scan_path = "D:\\Media\\Incoming\\Soulseek"

[soulseek]
username = "your_soulseek_user"
password = "your_soulseek_password"
```

Lidarr API key: **Settings → General → Security**.

Windows paths use doubled backslashes: `"D:\\Music\\Complete"`.

Create the Incomplete and Complete folders before the first run.

4. Confirm Lidarr:

```powershell
.\.venv\Scripts\python.exe check.py
```

You want `Lidarr OK` and a list of missing albums.

5. Open listen ports. PowerShell **as Administrator**:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\open-firewall.ps1
```

Forward TCP `2234` and `2235` on the router to this PC.

6. Test in the foreground:

```powershell
.\.venv\Scripts\python.exe main.py
```

While it runs, open **http://127.0.0.1:8787** for a live download list and Cancel buttons.

Stop with Ctrl+C.

7. Run in the background at logon:

```powershell
.\register-task.ps1
```

If PowerShell says the script is not digitally signed (common on `Z:`):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\register-task.ps1
```

## Lidarr so files land in the library

The worker drops finished albums in Complete and calls `DownloadedAlbumsScan` with **Move**, then `RenameFiles`.

In Lidarr:

- **Settings → Media Management → Rename Tracks** = On
- Track naming should include an album folder, for example  
  `{Album Title} ({Release Year})/{track:00} {Track Title}`
- Artist **Root Folder** must be the real library, not Complete
- **Settings → Download Clients → Completed Download Handling** = Enable

## What working looks like

Log file: `lidarr-soulseek.log`

```
Lidarr OK
Logged in to Soulseek as YourUser
Searching: artist album
Picked someuser :: folder
Download progress 4/12 complete
Converting track.flac -> MP3 320k
Asked Lidarr to import ...
Asked Lidarr to rename files for artist id ...
Deleted source file ...
```

Peer `ConnectToPeer` / `failed connect on user request` lines are other clients failing to reach you. They are not job failures. Recent builds hide most of that noise.

`connection is closing / closed` on FileSearch means the Soulseek session dropped. Close Nicotine+ / official Soulseek / slskd if they use the same username. Wait 10–15 minutes after a lot of reconnects, then start the task again.

## Config notes

| Setting | Meaning |
|---|---|
| `preferred_extensions = ["flac", "wav", "mp3"]` | Search order. Put `flac` first to prefer lossless, then convert |
| `convert_to_mp3 = true` | Encode lossless to 320 kbps MP3 before Lidarr import |
| `fill_missing_tracks = true` | After albums, search individual missing tracks |
| `focus_one_artist = true` | One artist per visit, A–Z |
| `artist_cooldown_days = 7` | After a visit, skip that artist for this many days. Separate from the short wait between artists |
| `cycle_seconds = 300` | Wait before the next ready artist. `0` starts them immediately |
| `max_missing_pages = 20` | Lidarr wanted/missing pages per cycle (250 albums each) |
| `concurrent_albums = 3` | How many full albums to work at once |
| `delete_sources_after_import = true` | Delete original FLAC/WAV after Lidarr is told to import |
| `clean_incomplete_on_fail = true` | Delete leftovers when a download fails |
| `incomplete_max_age_hours = 12` | Sweep old Incomplete files each cycle (`0` disables) |
| `share_dir` | Optional folder to share. Extra folders are `share_dirs` under `[sharing]`. Leave empty to share nothing. The worker scans after login |

Lidarr does not need a download client for this. The album quality profile must allow MP3.

Use only one Soulseek client on this account while the worker runs.

## Daily commands

```powershell
Get-Content .\lidarr-soulseek.log -Wait -Tail 30
Get-ScheduledTask -TaskName LidarrSoulseekWorker
Stop-ScheduledTask -TaskName LidarrSoulseekWorker
Start-ScheduledTask -TaskName LidarrSoulseekWorker
```

Uninstall the logon task:

```powershell
.\uninstall.ps1
```

## Legal

Soulseek is a peer-to-peer network. Only download and share files you have the right to download and share.
