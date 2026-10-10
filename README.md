# Lidarr Soulseek Worker

**Version 1.3.0** — Windows desktop app. No Docker. No slskd.

The worker reads Lidarr **Wanted → Missing**, searches Soulseek, downloads matching albums or individual missing tracks, converts FLAC/WAV to MP3 when needed, asks Lidarr to import and rename, then cleans leftover files.

Custom Lidarr import list (MusicBrainz IDs):  
https://raw.githubusercontent.com/T2112/lidarr-soulseek/main/lists/lidarr-custom-list.json

## Desktop app

Most people should install the desktop app instead of setting up Python.

1. Download `LidarrSoulseek-Setup-1.3.0.exe` from the **Windows installer** workflow artifacts on GitHub (Actions → the latest successful run → Artifacts).
2. Run the installer. It copies the app into your user programs folder and adds a **Start Menu** shortcut named Lidarr Soulseek. An optional task can also start the app when you sign in.
3. The first window asks for the Lidarr URL, the Lidarr API key, the Soulseek username and password, and the Incomplete / Complete folders. Use **Test Lidarr**, then **Save**. You do not edit `config.toml` by hand.
4. Open **Status**. **Start** begins searching. **Pause** holds the current work and resumes it later. **Stop** ends the run and keeps your place. **Skip** drops the album or song it is on and moves to the next one.
5. Open **Wanted**. **Sync** loads the missing list from Lidarr. Uncheck anything the worker should leave alone. **Select all** applies to the current view: Everything, Incomplete artists, Incomplete albums, or Incomplete songs. Those checkboxes are remembered after a restart.

The browser page at `http://127.0.0.1:8787` still works while the worker is running. The desktop window does not need a browser.

ffmpeg is still separate if you want FLAC converted to MP3: `winget install Gyan.FFmpeg`. The Status tab says so when it is missing.

Config, the database, and the log for an installed copy live in `%LOCALAPPDATA%\Lidarr Soulseek\`.

### Build the installer

On a Windows machine, from this folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

That installs the Python dependencies and PyInstaller, freezes `desktop_app.py`, and compiles `packaging\LidarrSoulseek.iss` with Inno Setup 6. If Inno Setup is not already installed, the script downloads it. The setup program is written to:

```text
dist\LidarrSoulseek-Setup-1.3.0.exe
```

The same steps run in GitHub Actions (`.github/workflows/windows-installer.yml`) on `windows-latest`, and the installer is uploaded as an artifact.

This cannot be cross-compiled from Linux. PyInstaller builds for the OS it runs on, and Inno Setup is a Windows program. Push the branch and let that workflow produce the `.exe`.

`python main.py` still runs the worker without the window, for the original script install below.

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
| `artist_cooldown_days = 7` | Do not revisit an artist for this many days |
| `max_missing_pages = 20` | Lidarr wanted/missing pages per cycle (250 albums each) |
| `concurrent_albums = 3` | How many full albums to work at once |
| `delete_sources_after_import = true` | Delete original FLAC/WAV after Lidarr is told to import |
| `clean_incomplete_on_fail = true` | Delete leftovers when a download fails |
| `incomplete_max_age_hours = 12` | Sweep old Incomplete files each cycle (`0` disables) |
| `share_dir` | Optional folder to share back to Soulseek. Leave empty at first |

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
