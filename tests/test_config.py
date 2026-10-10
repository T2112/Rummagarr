from __future__ import annotations

from pathlib import Path

from config_loader import ConfigError, default_config, save_config, try_load_config


def test_round_trip_keeps_secrets_and_windows_paths(tmp_path: Path):
    cfg = default_config(
        lidarr_url="http://127.0.0.1:8686",
        lidarr_api_key="abc123",
        lidarr_scan_path=r"D:\Media\Incoming",
        slsk_username="souluser",
        slsk_password='p@ss"word',
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        state_db=tmp_path / "state.sqlite3",
        log_file=tmp_path / "lidarr-soulseek.log",
        start_on_launch=True,
        ignored_users={"badpeer"},
        preferred_extensions=["flac", "mp3"],
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    text = path.read_text(encoding="utf-8")
    assert r"D:\\Media\\Incoming" in text
    assert r"p@ss\"word" in text
    loaded = try_load_config(path)
    assert loaded.lidarr_api_key == "abc123"
    assert loaded.slsk_password == 'p@ss"word'
    assert loaded.lidarr_scan_path == r"D:\Media\Incoming"
    assert loaded.start_on_launch is True
    assert loaded.ignored_users == {"badpeer"}
    assert loaded.preferred_extensions == ["flac", "mp3"]
    assert loaded.download_dir == tmp_path / "in"
    assert (tmp_path / "in").is_dir()


def test_changeme_and_bad_toml_are_errors(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('api_key = "oops"\n', encoding="utf-8")
    try:
        try_load_config(path)
    except ConfigError as exc:
        assert "missing" in str(exc).lower() or "Invalid TOML" in str(exc)
    else:
        raise AssertionError("bad toml should fail")

    save_config(
        path,
        default_config(
            lidarr_api_key="CHANGEME",
            slsk_username="user",
            slsk_password="pass",
            download_dir=tmp_path / "in",
            complete_dir=tmp_path / "out",
        ),
    )
    try:
        try_load_config(path)
    except ConfigError as exc:
        assert "CHANGEME" in str(exc)
    else:
        raise AssertionError("placeholder secrets should fail")
