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
    assert loaded.check_updates is True
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


def test_check_updates_defaults_on_when_the_key_is_missing(tmp_path: Path):
    path = tmp_path / "config.toml"
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        check_updates=False,
    )
    save_config(path, cfg)
    assert try_load_config(path).check_updates is False
    text = path.read_text(encoding="utf-8").replace("check_updates = false\n", "")
    path.write_text(text, encoding="utf-8")
    assert "check_updates =" not in text
    assert try_load_config(path).check_updates is True


def test_lidarr_track_concurrency_defaults_and_clamps(tmp_path: Path):
    path = tmp_path / "config.toml"
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
    )
    assert cfg.lidarr_track_concurrency == 6
    save_config(path, cfg)
    text = path.read_text(encoding="utf-8")
    assert "lidarr_track_concurrency = 6" in text
    assert try_load_config(path).lidarr_track_concurrency == 6
    path.write_text(text.replace("lidarr_track_concurrency = 6\n", ""), encoding="utf-8")
    assert "lidarr_track_concurrency =" not in path.read_text(encoding="utf-8")
    assert try_load_config(path).lidarr_track_concurrency == 6
    path.write_text(text.replace("lidarr_track_concurrency = 6", "lidarr_track_concurrency = 100"), encoding="utf-8")
    assert try_load_config(path).lidarr_track_concurrency == 8
    path.write_text(text.replace("lidarr_track_concurrency = 6", "lidarr_track_concurrency = 0"), encoding="utf-8")
    assert try_load_config(path).lidarr_track_concurrency == 1
