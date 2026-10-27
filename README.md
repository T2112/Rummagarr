# Rummagarr

Rummagarr is a Windows desktop app that fills Lidarr's missing albums and tracks from Soulseek.

[![Release](https://img.shields.io/github/v/release/T2112/lidarr-soulseek)](https://github.com/T2112/lidarr-soulseek/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/T2112/lidarr-soulseek/total)](https://github.com/T2112/lidarr-soulseek/releases)
[![License: personal use](https://img.shields.io/badge/license-personal%20use-blue)](LICENSE.txt)
[![CI](https://img.shields.io/github/actions/workflow/status/T2112/lidarr-soulseek/windows-installer.yml?branch=main&label=CI)](https://github.com/T2112/lidarr-soulseek/actions/workflows/windows-installer.yml)

**Version 1.19.1.** Rummagarr is published in the repository T2112/lidarr-soulseek (it will move to T2112/rummagarr).

[Sponsor Rummagarr](https://github.com/sponsors/T2112) · [Latest release](https://github.com/T2112/lidarr-soulseek/releases/latest) · [Issues](https://github.com/T2112/lidarr-soulseek/issues)

## What it is

Rummagarr runs on Windows next to a Lidarr you already have. It searches Soulseek for the albums and tracks Lidarr still wants, downloads the files, and asks Lidarr to import and rename them. It does not use Docker or slskd.

## Key features

- Soulseek search for Lidarr's missing albums and tracks, then the download.
- Optional FLAC/WAV to MP3 through ffmpeg before Lidarr imports the files.
- A Wanted list saved on this PC, with search order, Sync, and a per-row search.
- Search, Browse, and Messages on the same Soulseek login as the worker.
- Download all missing and long queues, with a progress count and Cancel.
- Lidarr import after each album, after each artist, on a timer, or left manual. A slow import shows Waiting on Lidarr import (3/10 min), then the next artist continues. That row has Retry import and Skip.
- Optional bridge: Lidarr searches a local Torznab indexer and grabs through a qBittorrent-compatible API. The standalone worker still runs with the bridge off.
- Help → Check for updates installs a newer release. Sharing limits, favorite-user suggestions, and an optional local crash bundle are included.

## Screenshots

![Status](docs/status.png)

![Wanted](docs/wanted.png)

![Settings](docs/settings.png)

A demo GIF of Status, then Wanted, then Search is not in this repository yet.

## Download and install

Download `Rummagarr-Setup-<version>.exe` from the [latest release](https://github.com/T2112/lidarr-soulseek/releases/latest). No GitHub account is required. The installer adds a Start Menu shortcut and a desktop shortcut, both named Rummagarr. An older install is upgraded in place.

### Windows SmartScreen

Windows may say “Windows protected your PC” because the installer is not signed with a paid certificate. Click **More info**, then **Run anyway**.

If the file is blocked, unblock it in PowerShell from the folder that contains the installer:

```powershell
Unblock-File .\Rummagarr-Setup-<version>.exe
```

## Tech stack

- Python and Tkinter on Windows
- Soulseek through [aioslsk](https://github.com/JurgenR/aioslsk)
- Lidarr's HTTP API
- SQLite on this PC
- PyInstaller and Inno Setup for the installer
- GitHub Actions (`.github/workflows/windows-installer.yml`) for tests and the Windows build

## Architecture

- `desktop_app.py` is the window. The worker searches, downloads, and talks to Lidarr.
- Config, the database, and `lidarr-soulseek.log` stay in `%LOCALAPPDATA%\Rummagarr`.
- Bridge mode (`lidarr_bridge.py`) serves Torznab and a qBittorrent 2.11.2-compatible API on this PC.
- The installed app updates itself from GitHub Releases. The check prefers the planned repository name and uses this repository while that name is unpublished. The client follows a redirect if the repository is renamed.
- Pull requests and pushes to `main` run the tests and upload `Rummagarr-Setup-<version>.exe` as a workflow artifact. A `v<version>` tag publishes that installer on the release.

## Changelog and issues

- [CHANGELOG.md](CHANGELOG.md)
- [GitHub Issues](https://github.com/T2112/lidarr-soulseek/issues)
- [Sponsor Rummagarr](https://github.com/sponsors/T2112)
- Portfolio, LinkedIn, and GitHub profile copy: [docs/showcase.md](docs/showcase.md)

## Set up Lidarr and Soulseek

1. Lidarr is already running. In Lidarr, open **Settings → General → Security** and copy the API key. On this PC the URL is usually `http://127.0.0.1:8686`. If Lidarr is on another computer, use the address this PC can reach, including a Tailscale address.
2. You need a Soulseek username and password. Close Nicotine+, the official Soulseek client, and slskd while Rummagarr is running. One login can only be online in one place.
3. Create an Incomplete folder and a Complete folder. The first window asks for the Lidarr URL, the API key, the Soulseek username and password, and those two folders. Click **Test Lidarr**, then **Save**. You do not edit `config.toml` by hand.
4. The sidebar opens on **Status**. **Start** begins searching. Open **Wanted** and click **Sync** to load missing music from Lidarr. Checked rows are the ones the worker searches.
5. Optional: `winget install Gyan.FFmpeg` if you want FLAC and WAV converted to MP3 before Lidarr imports them. Status says so when ffmpeg is missing.

## Using the app

**Start** begins searching. The line in the status card names the current search, wait, or download, and it says Idle only when nothing is selected. **Pause** holds the current work and resumes it later. **Stop** ends the run and keeps your place. **Skip** drops the album or song it is on and moves to the next one. **Auto-import to Lidarr after each artist** is off until you check it. When it is on, an empty download queue after an artist asks Lidarr to import and rename that artist's files. Settings → Behavior → Lidarr import can turn that off, import after each album, or import on a timer. A slow Lidarr import shows Waiting on Lidarr import (3/10 min) and then moves on. Retry import and Skip are on that artist's row.

**Wanted** opens the saved list immediately. **Sync** reads Lidarr and fills the list as each page arrives. **Artist** filters by name. Uncheck anything the worker should leave alone. The magnifying glass on a row opens **Search** for that artist, album, or song.

**Search** runs a query you type. Results are grouped by user. Download uses the same queue, conversion, and Lidarr import as an automatic grab, and it uses the login from **Start**.

**Settings → Behavior** edits formats, how long an artist is skipped, MP3 conversion, and the rest of the saved options. Dark is the default theme. Light applies immediately and is saved. The version is on the window title and under **Help → About**. **Help → About → Donate** opens the [GitHub Sponsors](https://github.com/sponsors/T2112) page.

The installed app checks GitHub Releases when it opens. **Help → Check for updates** runs that check immediately. Config, the database, and the log live in `%LOCALAPPDATA%\Rummagarr\`.

A custom Lidarr import list of MusicBrainz IDs is at  
https://raw.githubusercontent.com/T2112/lidarr-soulseek/main/lists/lidarr-custom-list.json

## Help

Questions, bugs, and ideas go to [GitHub Issues](https://github.com/T2112/lidarr-soulseek/issues). Include the app version from **Help → About**. For a crash, attach the zip from **Help → Open crash folder**.

## Sponsor

[Sponsor on GitHub](https://github.com/sponsors/T2112)

## Build the installer

On a Windows machine, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

That installs the Python dependencies and PyInstaller, freezes `desktop_app.py`, and compiles `packaging\LidarrSoulseek.iss` with Inno Setup 6. If Inno Setup is not already installed, the script downloads it. The setup program is written to:

```text
dist\Rummagarr-Setup-1.11.0.exe
```

The same steps run in GitHub Actions (`.github/workflows/windows-installer.yml`) on `windows-latest`. Pull requests and pushes to `main` upload the installer as a workflow artifact.

To publish a public download, push a tag `v<version>` that matches the `VERSION` file (for example `v1.19.0`). That run creates a GitHub Release and attaches `Rummagarr-Setup-<version>.exe`. The release notes are the matching section of `CHANGELOG.md`, followed by the install instructions. The release fails if that section is missing. You can also publish the version already in `VERSION` without making the tag yourself: **Actions → Windows installer → Run workflow**, and choose the `main` branch. If that release already exists, the workflow replaces the installer and the notes.

This cannot be cross-compiled from Linux. PyInstaller builds for the OS it runs on, and Inno Setup is a Windows program. Push the branch and let that workflow produce the `.exe`.

`python main.py` still runs the worker without the window, for the original script install below.

## Recent changes

Version 1.19.1 sets how often finished artists are imported: off (manual), after each album, after each artist, or on a timer. One slow Lidarr import no longer holds the worker. Status shows Waiting on Lidarr import (3/10 min), with Retry import and Skip.

Older notes are in [CHANGELOG.md](CHANGELOG.md).

## Requirements

- Windows 10 or 11
- Lidarr already running on this PC
- A Soulseek account
- Python 3.11+ from https://www.python.org/downloads/  
  Tick **Add python.exe to PATH**
- ffmpeg (`winget install Gyan.FFmpeg`) if you want FLAC converted to MP3

## Script install

The Windows installer above is the download most people want. These steps run the Python scripts from a checkout.

1. Unzip this folder somewhere permanent, for example `C:\Tools\rummagarr`.  
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

The script opens Soulseek TCP `2234` and `2235`, and the Lidarr bridge port (`8788` unless `[bridge]` in config says otherwise). It still runs when `config.toml` is missing. The installed app keeps that file in `%LOCALAPPDATA%\Rummagarr`, and the script reads it there. The installer can add the same rules at the end of setup. Settings → Bridge → Allow in Windows Firewall opens the bridge port from the app. Lidarr on another machine needs this PC's Tailscale address, not `127.0.0.1`.

Forward TCP `2234` and `2235` on the router to this PC. Tailscale does not need those router forwards for the bridge.

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

This worker is provided as-is for personal use.

Only download music you have the rights to. Soulseek is a peer-to-peer network, and you are responsible for what you download and what you share.

Rummagarr uses aioslsk (GPL-3.0-or-later). If you distribute this package together with aioslsk, the combined work is subject to the GPL-3.0. See https://github.com/JurgenR/aioslsk and [LICENSE.txt](LICENSE.txt).
