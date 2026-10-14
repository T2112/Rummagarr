"""Open the desktop window with sample library data and save screenshots.

Run under a virtual display large enough for device scale 2 (2560x1600):

    xvfb-run -a -s "-screen 0 2560x1600x24" python scripts/screenshot_gui.py /opt/cursor/artifacts
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop_app import DesktopApp  # noqa: E402
from tests.conftest import album, hole, make_worker  # noqa: E402
from wanted import build_catalog, items_to_rows  # noqa: E402


class Transfer:
    def __init__(self, filename: str, username: str, done: int, total: int, state: str) -> None:
        self.filename = filename
        self.username = username
        self.filesize = total
        self.bytes_transfered = done
        self.state = type("State", (), {"name": state})()


def grab(widget, path: Path) -> None:
    """Lossless PNG of the window. No resize."""
    from process_launch import run_hidden

    path.parent.mkdir(parents=True, exist_ok=True)
    widget.update_idletasks()
    widget.update()
    width = int(widget.winfo_width())
    height = int(widget.winfo_height())
    origin_x = int(widget.winfo_rootx())
    origin_y = int(widget.winfo_rooty())
    display = os.environ.get("DISPLAY", ":0")
    run_hidden(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            f"{width}x{height}",
            "-i",
            f"{display}+{origin_x},{origin_y}",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )


def _shoot(app, path: Path) -> None:
    app.update_idletasks()
    app.update()
    app.lift()
    time.sleep(0.3)
    grab(app, path)
    print(path, app.winfo_width(), app.winfo_height(), flush=True)


def main() -> None:
    stay = "--stay" in sys.argv
    os.environ.setdefault("RUMMAGARR_UI_SCALE", "2")
    out = Path(next((arg for arg in sys.argv[1:] if not arg.startswith("-")), "/tmp/lidarr-soulseek-shots"))
    data = Path("/tmp/lidarr-soulseek-shot-data")
    data.mkdir(parents=True, exist_ok=True)
    worker = make_worker(data)
    items = build_catalog(
        [
            album(1, "Ella Langley", "Hungover", files=0, total=12),
            album(2, "Ella Langley", "Still", files=3, total=10),
            album(3, "Chris Stapleton", "Traveller", files=0, total=14, artist_id=2),
            album(4, "Tyler Childers", "Purgatory", files=1, total=10, artist_id=3),
        ],
        [
            hole(21, 2, "Ella Langley", "Still", "Cowboy", number=4),
            hole(22, 2, "Ella Langley", "Still", "Out of Love", number=6),
            hole(41, 4, "Tyler Childers", "Purgatory", "Feathered Indians", number=2),
        ],
    )
    worker.store.begin_wanted_sync()
    worker.store.write_wanted_page(items_to_rows(items))
    worker.store.finish_wanted_sync(complete=True)
    worker.store.set_wanted_choices({"album:3": False})
    for index, name in enumerate(
        ["01 Blue.mp3", "02 Hungover.mp3", "03 Out of Love.flac", "04 Cowboy.mp3", "05 Still.mp3"],
        start=1,
    ):
        worker.store.add_completed_download("Ella Langley", "Hungover", name, "vinylqueen", "album")
    worker.active_transfers = [
        Transfer(r"Hungover\01 Blue.flac", "vinylqueen", 450, 1000, "DOWNLOADING"),
        Transfer(r"Hungover\02 Country Song.mp3", "archives_user", 1000, 1000, "COMPLETE"),
    ]
    worker.set_job("downloading album", "Ella Langley", "Hungover")
    worker.control.start()
    worker._activate("album:1")

    app = DesktopApp(
        worker,
        data / "config.toml",
        start_status_server=False,
        honor_start_on_launch=False,
        poll_ms=60_000,
    )
    app.geometry(f"{app.px(1280)}x{app.px(800)}+0+0")
    app.limit_var.set("25")
    app.on_limit()
    app.notebook.select(app.status_tab)
    app.update_idletasks()
    app.update()
    app.lift()
    if stay:
        print("Rummagarr demo window is open", flush=True)
        app.mainloop()
        worker.close()
        return

    _shoot(app, out / "app-dark-status.png")
    app.notebook.select(app.wanted_tab)
    _shoot(app, out / "app-dark-wanted.png")
    app.notebook.select(app.settings_tab)
    _shoot(app, out / "app-dark-settings.png")

    form = app._settings_form
    form.theme_var.set("light")
    form._on_theme_choice()
    app.update()
    _shoot(app, out / "app-light-settings.png")
    app.notebook.select(app.status_tab)
    _shoot(app, out / "app-light-status.png")
    app.notebook.select(app.wanted_tab)
    _shoot(app, out / "app-light-wanted.png")

    app.destroy()
    worker.close()
    print(out)


if __name__ == "__main__":
    main()
