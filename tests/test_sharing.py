from __future__ import annotations

from pathlib import Path

from config_loader import default_config, save_config, try_load_config
from sharing import (
    LiveUpload,
    QueuedUpload,
    RemoteFile,
    SharePolicy,
    WantedTrack,
    admit,
    match_files_to_wanted,
    uploads_to_pause,
)
from tests.conftest import album, hole, make_config, make_worker
from wanted import build_catalog, items_to_rows
from worker import build_slsk_settings, policy_from_config


def _policy(**overrides) -> SharePolicy:
    policy = SharePolicy()
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def test_queue_limits_refuse_and_can_ban():
    policy = _policy(max_queued_files=2, max_queued_bytes=5_000, auto_ban=True)
    queued = [
        QueuedUpload("Leecher", "a.flac", 1000, "queued"),
        QueuedUpload("leecher", "b.flac", 1000, "uploading"),
    ]
    decision = admit(policy, "leecher", "c.flac", 1000, queued, peer_share_files=10)
    assert decision.allow is False
    assert decision.ban is True

    size_policy = _policy(max_queued_files=25, max_queued_bytes=1500, auto_ban=False)
    size_decision = admit(
        size_policy,
        "leecher",
        "c.flac",
        600,
        [QueuedUpload("leecher", "a.flac", 1000, "queued")],
        None,
    )
    assert size_decision.allow is False
    assert size_decision.ban is False

    allowed = admit(policy, "friend", "one.flac", 100, [], 10)
    assert allowed.allow is True
    assert allowed.ban is False


def test_peer_share_and_ignore_list():
    policy = _policy(require_peer_share=True, min_peer_share_files=5, banned={"leecher"})
    ignored = admit(policy, "Leecher", "a.flac", 10, [], 100)
    assert ignored.allow is False
    short = admit(policy, "quiet", "a.flac", 10, [], 4)
    assert short.allow is False
    assert "4" in short.reason
    enough = admit(policy, "quiet", "a.flac", 10, [], 5)
    assert enough.allow is True


def test_per_user_speed_pauses_the_fastest_upload_first():
    policy = _policy(per_user_speed_kbps=100, pause_all=False)
    rows = [
        LiveUpload("a\none", "leecher", speed=80 * 1024, status="uploading", priority=False),
        LiveUpload("a\ntwo", "leecher", speed=40 * 1024, status="uploading", priority=True),
    ]
    paused = uploads_to_pause(policy, rows)
    assert paused == {"a\none"}
    assert uploads_to_pause(_policy(pause_all=True), rows) == {"a\none", "a\ntwo"}
    assert uploads_to_pause(_policy(per_user_speed_kbps=0), rows) == set()


def test_wanted_matching_uses_folded_names_and_skips_other_artists():
    tracks = [
        WantedTrack("Ella Langley", "Hungover", "Hungover", "song:1", "song", album_id=1, track_id=1),
        WantedTrack("Mötley Crüe", "Dr. Feelgood", "Kickstart My Heart", "song:2", "song", album_id=2, track_id=2),
        WantedTrack("Someone Else", "Other", "Hungover", "song:3", "song", album_id=3, track_id=3),
    ]
    files = [
        RemoteFile("vinyl", r"Ella Langley - Hungover\01 Hungover.flac", size=1000, extension="flac"),
        RemoteFile("vinyl", r"Music\Motley Crue\Dr. Feelgood\02 Kickstart My Heart.flac", size=2000, extension="flac"),
        RemoteFile("vinyl", r"Noise\not a match.mp3", size=10, extension="mp3"),
    ]
    matches = match_files_to_wanted(
        files,
        tracks,
        min_filename_ratio=0.5,
        preferred_extensions=["flac", "mp3"],
        min_mp3_bitrate=320,
    )
    found = {match.song for match in matches}
    assert found == {"Hungover", "Kickstart My Heart"}
    by_song = {match.song: match for match in matches}
    assert by_song["Hungover"].album_id == 1
    assert by_song["Kickstart My Heart"].artist == "Mötley Crüe"
    assert by_song["Kickstart My Heart"].track_id == 2


def test_store_lookup_does_not_return_other_artists(tmp_path):
    worker = make_worker(tmp_path)
    try:
        items = build_catalog(
            [
                album(1, "Ella Langley", "Hungover", files=0, total=1),
                album(2, "Mötley Crüe", "Dr. Feelgood", files=0, total=1),
            ],
            [
                hole(11, 1, "Ella Langley", "Hungover", "Hungover"),
                hole(22, 2, "Mötley Crüe", "Dr. Feelgood", "Kickstart My Heart"),
            ],
        )
        worker.store.write_wanted_page(items_to_rows(items))
        rows = worker.store.wanted_tracks_for(["Motley Crue"], ["motley crue"])
        artists = {row["artist"] for row in rows}
        assert "Mötley Crüe" in artists
        assert "Ella Langley" not in artists
        files = [
            RemoteFile("vinyl", r"Music\Motley Crue\Dr. Feelgood\02 Kickstart My Heart.flac", extension="flac")
        ]
        matches = worker.match_user_files(
            [
                type(
                    "File",
                    (),
                    {
                        "username": item.username,
                        "filename": item.filename,
                        "size": item.size,
                        "bitrate": 0,
                        "extension": "flac",
                    },
                )()
                for item in files
            ]
        )
        assert [match.song for match in matches] == ["Kickstart My Heart"]
    finally:
        worker.close()


def test_worker_rejects_an_oversized_queue(tmp_path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg.max_queued_files_per_user = 1
        worker.cfg.auto_ban_over_queue = True

        class Transfer:
            username = "leecher"
            remote_path = "a.flac"
            filesize = 10
            state = type("State", (), {"__str__": lambda self: "QueuedState"})()

        class Manager:
            def get_uploads(self):
                return [Transfer()]

        worker.client = type("Client", (), {"transfers": Manager(), "users": None})()
        decision = worker.decide_upload("leecher", "b.flac", 10)
        assert decision.allow is False
        assert decision.ban is True
        worker.ban_user("leecher")
        assert "leecher" in worker.cfg.ignored_users
        assert worker.take_config_dirty() is True
    finally:
        worker.close()


def test_share_settings_include_every_folder_and_wait_to_scan(tmp_path: Path):
    cfg = make_config(tmp_path)
    cfg.share_dir = str(tmp_path / "library")
    cfg.share_dirs = [str(tmp_path / "more")]
    cfg.upload_slots = 4
    cfg.upload_speed_kbps = 128
    settings = build_slsk_settings(cfg)
    assert settings.shares.scan_on_start is False
    assert [entry.path for entry in settings.shares.directories] == [
        str(tmp_path / "library"),
        str(tmp_path / "more"),
    ]
    assert settings.transfers.limits.upload_slots == 4
    assert settings.network.limits.upload_speed_kbps == 128
    policy = policy_from_config(cfg)
    assert policy.upload_slots == 4
    assert policy.upload_speed_kbps == 128


def test_sharing_config_round_trip(tmp_path: Path):
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        share_dir=str(tmp_path / "library"),
        share_dirs=[str(tmp_path / "extra")],
        upload_slots=3,
        max_queued_mb_per_user=1024,
        pause_uploads=True,
        priority_users={"friend"},
        require_peer_share=True,
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    loaded = try_load_config(path)
    assert loaded.upload_slots == 3
    assert loaded.max_queued_mb_per_user == 1024
    assert loaded.pause_uploads is True
    assert loaded.priority_users == {"friend"}
    assert loaded.share_dirs == [str(tmp_path / "extra")]
    assert loaded.require_peer_share is True
