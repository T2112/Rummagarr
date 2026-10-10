from __future__ import annotations

import argparse
from pathlib import Path

from config_loader import load_config
from lidarr_client import LidarrClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Lidarr API and config paths")
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.toml")))
    args = parser.parse_args()
    cfg = load_config(Path(args.config))
    print(f"Config OK")
    print(f"  download_dir = {cfg.download_dir}")
    print(f"  complete_dir = {cfg.complete_dir}")
    print(f"  lidarr       = {cfg.lidarr_url}")
    client = LidarrClient(cfg.lidarr_url, cfg.lidarr_api_key)
    try:
        status = client.ping()
        print(f"Lidarr OK: version={status.get('version')} app={status.get('appName')}")
        missing, next_page, exhausted = client.wanted_missing(max_pages=1)
        print(f"Missing monitored albums on page 1: {len(missing)}")
        for album in missing[:8]:
            artist = (album.get("artist") or {}).get("artistName") or "?"
            print(f"  - {artist} / {album.get('title')}")
        if len(missing) > 8:
            print(f"  ... {len(missing) - 8} more")
    finally:
        client.close()


if __name__ == "__main__":
    main()
