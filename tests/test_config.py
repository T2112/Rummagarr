from __future__ import annotations

from pathlib import Path

from config_loader import (
    ConfigError,
    _retarget_paths,
    default_config,
    migrate_app_data,
    normalize_theme,
    save_config,
    try_load_config,
)


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
    assert loaded.auto_import_after_artist is False


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


def test_auto_import_defaults_off_and_round_trips(tmp_path: Path):
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
    )
    assert cfg.auto_import_after_artist is False
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    text = path.read_text(encoding="utf-8")
    assert "auto_import_after_artist = false" in text
    assert try_load_config(path).auto_import_after_artist is False
    path.write_text(text.replace("auto_import_after_artist = false\n", ""), encoding="utf-8")
    assert "auto_import_after_artist =" not in path.read_text(encoding="utf-8")
    assert try_load_config(path).auto_import_after_artist is False
    save_config(path, default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        auto_import_after_artist=True,
    ))
    assert try_load_config(path).auto_import_after_artist is True


def test_theme_defaults_to_dark_and_round_trips(tmp_path: Path):
    assert normalize_theme("nope") == "dark"
    assert default_config().theme == "dark"
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        theme="light",
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    text = path.read_text(encoding="utf-8")
    assert 'theme = "light"' in text
    assert try_load_config(path).theme == "light"
    path.write_text(text.replace("[ui]\ntheme = \"light\"\n", ""), encoding="utf-8")
    assert try_load_config(path).theme == "dark"


def test_migrate_app_data_copies_the_old_folder_once(tmp_path: Path):
    source = tmp_path / "Lidarr Soulseek"
    dest = tmp_path / "Rummagarr"
    source.mkdir()
    (source / "state.sqlite3").write_bytes(b"db-bytes")
    (source / "lidarr-soulseek.log").write_text("old log", encoding="utf-8")
    db = str(source / "state.sqlite3")
    (source / "config.toml").write_text(
        "\n".join(
            [
                f'state_db = "{db}"',
                f'log_file = "{source / "lidarr-soulseek.log"}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    migrate_app_data(dest, source)
    assert (dest / "state.sqlite3").read_bytes() == b"db-bytes"
    assert (dest / "lidarr-soulseek.log").read_text(encoding="utf-8") == "old log"
    copied = (dest / "config.toml").read_text(encoding="utf-8")
    assert str(dest) in copied
    assert str(source) not in copied
    assert (source / "config.toml").exists()
    (dest / "config.toml").write_text("kept", encoding="utf-8")
    (source / "extra.txt").write_text("later", encoding="utf-8")
    migrate_app_data(dest, source)
    assert (dest / "config.toml").read_text(encoding="utf-8") == "kept"
    assert not (dest / "extra.txt").exists()
    windows_old = Path(r"C:\Users\a\AppData\Local\Lidarr Soulseek")
    windows_new = Path(r"C:\Users\a\AppData\Local\Rummagarr")
    raw = 'state_db = "C:\\\\Users\\\\a\\\\AppData\\\\Local\\\\Lidarr Soulseek\\\\state.sqlite3"\n'
    if "\\" in str(windows_old):
        rewritten = _retarget_paths(raw, windows_old, windows_new)
        assert "Lidarr Soulseek" not in rewritten
        assert "Rummagarr" in rewritten


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
