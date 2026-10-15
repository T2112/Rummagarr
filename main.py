from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from config_loader import app_version, load_config
from crash_bundle import crash_directory, install_crash_hook
from status_ui import start_status_ui
from worker import Worker


def setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    # Soulseek peer chatter. These are other clients failing to reach you, not job failures.
    for noisy in (
        "aioslsk.network",
        "aioslsk.network.network",
        "aioslsk.network.connection",
        "aioslsk.distributed",
        "aioslsk.client",
        "aioslsk.search.manager",
    ):
        logging.getLogger(noisy).setLevel(logging.ERROR)


async def amain(config_path: Path) -> None:
    cfg = load_config(config_path)
    setup_logging(cfg.log_file)
    worker = Worker(cfg)
    install_crash_hook(
        lambda: bool(getattr(worker.cfg, "crash_bundle", False)),
        lambda: crash_directory(worker.cfg.log_file),
        lambda: worker.cfg.log_file,
        lambda: config_path,
        app_version(),
    )
    server = start_status_ui(worker, cfg.status_bind, cfg.status_port)
    try:
        await worker.run_forever()
    finally:
        server.shutdown()
        server.server_close()
        worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Lidarr missing albums from Soulseek")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.toml")),
        help="Path to config.toml",
    )
    args = parser.parse_args()
    asyncio.run(amain(Path(args.config)))


if __name__ == "__main__":
    from process_launch import configure_child_processes

    configure_child_processes()
    main()
