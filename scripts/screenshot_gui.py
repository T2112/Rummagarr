"""Open the desktop window with sample library data and save screenshots.

Run under a virtual display:

    xvfb-run -a -s "-screen 0 1280x800x24" python scripts/screenshot_gui.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop_app import DesktopApp, SettingsForm, apply_theme  # noqa: E402
from tests.conftest import album, hole, make_worker  # noqa: E402
from wanted import build_catalog, items_to_rows  # noqa: E402


class Transfer:
    def __init__(self, filename: str, username: str, done: int, total: int, state: str) -> None:
        self.filename = filename
        self.username = username
        self.filesize = total
        self.bytes_transfered = done
        self.state = type("State", (), {"name": state})()


def grab(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    display = os.environ.get("DISPLAY", "")
    import subprocess

    subprocess.check_call(
        ["import", "-window", "root", "-display", display, str(path)],
    )


def main() -> None:
    stay = "--stay" in sys.argv
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
    app.limit_var.set("25")
    app.on_limit()
    app.notebook.select(app.status_tab)
    app.update_idletasks()
    app.update()
    app.lift()
    app.attributes("-topmost", True)
    app.after(800, lambda: app.attributes("-topmost", False))
    if stay:
        print("Seekarr demo window is open", flush=True)
        app.mainloop()
        worker.close()
        return
    time.sleep(0.4)
    grab(out / "status_tab.png")

    app.notebook.select(app.wanted_tab)
    app.update_idletasks()
    app.update()
    time.sleep(0.4)
    grab(out / "wanted_tab.png")

    app.destroy()

    import tkinter as tk

    setup = tk.Tk()
    setup.title("Seekarr setup")
    setup.geometry("740x700")
    apply_theme(setup)
    form = SettingsForm(setup, Path("/tmp/unused-config.toml"), None, lambda _cfg: None, setup.destroy)
    form.url_var.set("http://127.0.0.1:8686")
    form.user_var.set("your-soulseek-user")
    form.pack(fill="both", expand=True)
    setup.update_idletasks()
    setup.update()
    time.sleep(0.3)
    grab(out / "setup.png")
    setup.destroy()
    worker.close()
    print(out)


if __name__ == "__main__":
    main()
