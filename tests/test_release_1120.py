"""Rummagarr 1.12.0: matching, blocklist, cap, changelog, and Lidarr command waits."""

from __future__ import annotations

import math

from changelog_notes import ChangelogError, changelog_section
from config_loader import default_config, save_config, try_load_config
from lidarr_client import LidarrClient
from match import (
    FolderCandidate,
    build_queries,
    fill_uncovered_titles,
    group_results,
    release_skip_reason,
    score_folder,
)
from tests.conftest import make_worker
from tests.test_match import _Rated


def test_changelog_section_is_the_version_and_nothing_after_it():
    text = """# Changelog

## [1.12.0] — 2026-10-14

- First

## [1.11.0] — 2026-10-13

- Older
"""
    section = changelog_section(text, "1.12.0")
    assert section.startswith("## [1.12.0]")
    assert "First" in section
    assert "1.11.0" not in section
    try:
        changelog_section(text, "9.9.9")
    except ChangelogError as exc:
        assert "9.9.9" in str(exc)
    else:
        raise AssertionError("missing section should fail")


def test_release_body_is_the_changelog_then_the_installer_notes():
    from changelog_notes import release_body

    text = "## [1.12.0] — 2026-10-14\n\n- Tries the next user\n\n## [1.11.0] — 2026-10-13\n\n- Older\n"
    body = release_body(text, "1.12.0")
    assert body.startswith("## [1.12.0]")
    assert "Tries the next user" in body
    assert "1.11.0" not in body.split("Windows installer for Rummagarr", 1)[0]
    assert body.index("Tries the next user") < body.index("Windows installer for Rummagarr 1.12.0.")
    assert "Rummagarr-Setup-1.12.0.exe" in body


def test_shipped_changelog_has_the_published_versions():
    from pathlib import Path

    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    for version, needle in (
        ("1.18.0", "Messages"),
        ("1.17.0", "favorite"),
        ("1.16.0", "Download all missing"),
        ("1.15.0", "Browse"),
        ("1.14.0", "qBittorrent"),
        ("1.13.0", "Search order"),
        ("1.12.0", "blocklist"),
        ("1.11.0", "Don't ask again"),
        ("1.10.0", "Rummagarr"),
    ):
        section = changelog_section(text, version)
        assert needle in section
    eleven = changelog_section(text, "1.11.0")
    for phrase in (
        "quality profile",
        "free-slot",
        "never reached Soulseek",
        "Albums per run",
        "Review",
        "Details",
        "Lidarr unreachable",
        "stretch to the bottom",
        "read back from Lidarr",
        "console window",
        "Don't ask again",
    ):
        assert phrase in eleven


def test_update_prompt_shows_the_changelog_section():
    from desktop_app import _update_notes
    from updater import UpdateStatus

    status = UpdateStatus(
        current="1.11.0",
        latest="1.12.0",
        notes="## [1.12.0] — 2026-10-14\n\n- Tries the next user\n\nWindows installer for Rummagarr 1.12.0.\n",
        newer=True,
    )
    shown = _update_notes(status)
    assert "Tries the next user" in shown
    assert "Windows installer" not in shown


def test_this_year_is_searched_and_a_future_year_is_not():
    assert "2026" in build_queries("Adele", "30", "2026")[0]
    assert "2099" not in build_queries("Adele", "30", "2099")[0]


def test_locked_files_are_not_scored():
    class Result:
        username = "ada"
        shared_items = [
            _Rated(r"Adele\30\01.flac"),
            _Rated(r"Adele\30\02.flac"),
        ]
        locked_results = [_Rated(r"Adele\30\02.flac")]

    folders = group_results([Result()], set())
    names = {item.filename for folder in folders for item in folder.files}
    assert r"Adele\30\02.flac" not in names
    assert r"Adele\30\01.flac" in names


def test_a_short_format_is_filled_from_the_other_files():
    primary = [_Rated(r"Adele\30\01 Hello.mp3", bitrate=320)]
    allowed = primary + [_Rated(r"Adele\30\02 Easy.flac")]
    filled = fill_uncovered_titles(primary, allowed, ["Hello", "Easy"], 0.5)
    assert len(filled) == 2


def test_deluxe_loses_to_the_standard_release():
    standard = FolderCandidate(
        username="studio",
        remote_dir=r"Adele\30",
        files=[_Rated(r"Adele\30\01 Hello.mp3", bitrate=320, duration=180)],
    )
    deluxe = FolderCandidate(
        username="extra",
        remote_dir=r"Adele\30 Deluxe",
        files=[_Rated(r"Adele\30 Deluxe\01 Hello.mp3", bitrate=320, duration=180)],
    )
    plain = score_folder(standard, "Adele", "30", ["Hello"], ["mp3"], 0.5, 1, 320)
    extra = score_folder(deluxe, "Adele", "30", ["Hello"], ["mp3"], 0.5, 1, 320)
    assert plain.score > extra.score


def test_live_and_compilation_are_skipped_unless_allowed():
    live = {"title": "Live", "albumType": "Live"}
    single = {"title": "One", "albumType": "Single"}
    from dataclasses import replace

    cfg = default_config()
    assert release_skip_reason(live, cfg) == "skipped live release"
    assert release_skip_reason(single, cfg) == "skipped single release"
    assert release_skip_reason(live, replace(cfg, allow_live=True)) == ""
    profile = {
        "id": 2,
        "secondaryAlbumTypes": [{"albumType": {"name": "Live"}, "allowed": True}],
    }
    album = {"title": "Night", "albumType": "Live", "metadataProfileId": 2}
    assert release_skip_reason(album, cfg, {2: profile}) == ""


def test_fake_flac_bitrate_and_spectrum():
    from audio_check import fake_flac_reason, spectrum_is_lossy

    assert "bitrate" in fake_flac_reason(extension="flac", bitrate_kbps=320)
    assert fake_flac_reason(extension="flac", bitrate_kbps=900) == ""
    assert fake_flac_reason(extension="mp3", bitrate_kbps=128) == ""
    rate = 44100
    samples = [math.sin(2 * math.pi * 1000 * i / rate) for i in range(2048)]
    assert spectrum_is_lossy(samples, rate)


def test_release_ids_prefer_the_monitored_release():
    from audio_tags import release_ids

    release, group = release_ids(
        {
            "foreignAlbumId": "group-1",
            "releases": [
                {"foreignReleaseId": "old", "monitored": False},
                {"foreignReleaseId": "kept", "monitored": True},
            ],
        }
    )
    assert release == "kept"
    assert group == "group-1"


def test_blocklist_skips_a_user_and_can_be_cleared(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.store.block_source("vinylqueen", r"Adele\30", "timeout")
        assert worker.store.source_blocked("vinylqueen", r"Adele\30")
        assert not worker.store.source_blocked("other", r"Adele\30")
        rows = worker.store.blocklist_rows()
        assert rows and rows[0]["username"] == "vinylqueen"
        worker.store.clear_block(rows[0]["id"])
        assert worker.store.blocklist_rows() == []
    finally:
        worker.close()


def test_partial_and_staged_albums_are_not_searched_again(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.store.upsert(4, "Adele", "21", "partial", "use track fill")
        worker.store.upsert(5, "Adele", "30", "staged", "on disk")
        assert worker.store.should_skip(4, 0) is True
        assert worker.store.should_skip(5, 0) is True
    finally:
        worker.close()


def test_saved_tracks_are_used_without_another_lidarr_call(tmp_path):
    from wanted import build_catalog, items_to_rows

    from tests.conftest import album, track

    worker = make_worker(tmp_path)
    item = album(21, "Adele", "21")
    catalog = build_catalog(
        [item],
        [{"track": track(211, "Hello"), "album": item, "artist_name": "Adele", "album_title": "21"}],
    )
    worker.store.write_wanted_page(items_to_rows(catalog))
    try:
        saved = worker.store.saved_tracks(21)
        assert saved and saved[0]["title"] == "Hello"
        called = []
        worker.lidarr.tracks_for_album = lambda album_id: called.append(album_id) or []
        assert worker.store.saved_tracks(21)
        assert called == []
    finally:
        worker.close()


def test_album_cap_pauses_until_tomorrow(tmp_path):
    from dataclasses import replace

    worker = make_worker(tmp_path)
    worker.cfg = replace(worker.cfg, max_albums_per_run=1)
    worker._albums_this_run = 1
    worker.control.start()
    try:
        assert worker._album_cap_reached() is True
        seconds, kind = worker.between_pass_wait()
        worker._apply_wait(seconds, kind)
        assert kind == "day"
        assert seconds > 60
        assert "Album cap" in worker.activity_view()[0]
    finally:
        worker.close()


def test_finish_command_waits_until_completed():
    client = LidarrClient("http://127.0.0.1:9", "key")
    seen = {"n": 0}

    def get(path, timeout=20.0):
        seen["n"] += 1
        status = "started" if seen["n"] == 1 else "completed"
        return {"id": 7, "status": status}

    client._get_once = get
    try:
        result = client.finish_command({"id": 7, "status": "queued"}, timeout=5)
    finally:
        client.close()
    assert result["status"] == "completed"
    assert seen["n"] >= 2
    assert client.finish_command({"name": "DownloadedAlbumsScan"})["name"] == "DownloadedAlbumsScan"


def test_import_failures_read_past_the_first_page():
    client = LidarrClient("http://127.0.0.1:9", "key")
    pages = []

    def get(path, params=None, timeout=20.0):
        pages.append((path, params["page"]))

        class Response:
            status_code = 200

            def json(self):
                size = params["pageSize"]
                if params["page"] >= 2:
                    return {"records": []}
                return {
                    "records": [
                        {
                            "artist": {"artistName": "Adele"},
                            "status": "warning",
                            "errorMessage": "track count does not match",
                        }
                        for _ in range(size)
                    ]
                }

        return Response()

    client._client.get = get
    try:
        found = client.import_failures("Adele")
    finally:
        client.close()
    assert any("track count" in line for line in found)
    assert any(page == 2 for _path, page in pages)


def test_config_defaults_match_the_example(tmp_path):
    cfg = default_config()
    assert cfg.concurrent_albums == 3
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[lidarr]",
                'url = "http://127.0.0.1:8686"',
                'api_key = "k"',
                "[soulseek]",
                'username = "u"',
                'password = "p"',
                "[paths]",
                'download_dir = "/tmp/in"',
                'complete_dir = "/tmp/out"',
                "[search]",
                "[runtime]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    loaded = try_load_config(path)
    assert loaded.max_albums_per_cycle == 1
    assert loaded.concurrent_albums == 3
    assert loaded.follow_metadata_profile is True
    save_config(path, loaded)
    assert try_load_config(path).concurrent_albums == 3


def test_run_summary_is_written(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker._summary_grabbed("Adele – 21")
        worker._summary_failed("Adele – 30", "no folder passed filters")
        text = (worker.cfg.log_file.parent / "run-summary.txt").read_text(encoding="utf-8")
        assert "1 grabbed" in text
        assert "Adele – 30: no folder passed filters" in text
        assert "This run" in worker.ui_snapshot()["run_summary"]
    finally:
        worker.close()
