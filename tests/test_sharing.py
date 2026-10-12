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
    choose_wanted_downloads,
    match_files_to_wanted,
    narrow_wanted_tracks,
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


def _pick_options() -> dict:
    return {
        "min_filename_ratio": 0.5,
        "preferred_extensions": ["flac", "mp3"],
        "min_mp3_bitrate": 320,
    }


def _hungover_songs() -> list[WantedTrack]:
    return [
        WantedTrack("Ella Langley", "Hungover", "Hungover", "song:1", "song", album_id=1, track_id=1),
        WantedTrack("Ella Langley", "Hungover", "Cowboy", "song:2", "song", album_id=1, track_id=2),
        WantedTrack("Ella Langley", "Still", "Nicotine", "song:3", "song", album_id=2, track_id=3),
    ]


def test_complete_folder_beats_a_faster_free_slot():
    tracks = _hungover_songs()[:2]
    files = [
        RemoteFile("slow", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", speed=100),
        RemoteFile("slow", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", speed=100),
        RemoteFile(
            "fast",
            r"Ella Langley\Hungover\01 Hungover.flac",
            extension="flac",
            free_slot=True,
            speed=9_000,
        ),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    assert {(match.username, match.song) for match in pick.matches} == {
        ("slow", "Hungover"),
        ("slow", "Cowboy"),
    }
    assert pick.line() == "2 wanted items matched from 1 user, and 0 wanted items not found."


def test_complete_folders_prefer_free_slot_then_speed_then_format():
    tracks = _hungover_songs()[:2]
    busy = [
        RemoteFile("busy", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", speed=9_000),
        RemoteFile("busy", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", speed=9_000),
    ]
    free = [
        RemoteFile("free", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", free_slot=True, speed=100),
        RemoteFile("free", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=100),
    ]
    pick = choose_wanted_downloads(busy + free, tracks, **_pick_options())
    assert {match.username for match in pick.matches} == {"free"}

    slow = [
        RemoteFile("slow", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", free_slot=True, speed=100),
        RemoteFile("slow", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=100),
    ]
    fast = [
        RemoteFile("fast", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", free_slot=True, speed=5_000),
        RemoteFile("fast", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=5_000),
    ]
    faster = choose_wanted_downloads(slow + fast, tracks, **_pick_options())
    assert {match.username for match in faster.matches} == {"fast"}

    mp3 = [
        RemoteFile("mp3user", r"Ella Langley\Hungover\01 Hungover.mp3", extension="mp3", bitrate=320, free_slot=True, speed=1_000),
        RemoteFile("mp3user", r"Ella Langley\Hungover\02 Cowboy.mp3", extension="mp3", bitrate=320, free_slot=True, speed=1_000),
    ]
    flac = [
        RemoteFile("flacuser", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", free_slot=True, speed=1_000),
        RemoteFile("flacuser", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=1_000),
    ]
    formatted = choose_wanted_downloads(mp3 + flac, tracks, **_pick_options())
    assert {match.username for match in formatted.matches} == {"flacuser"}


def test_missing_tracks_fall_back_to_other_users_and_report_gaps():
    tracks = _hungover_songs()
    files = [
        RemoteFile("busy", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", speed=100),
        RemoteFile(
            "free",
            r"Ella Langley\Hungover\01 Hungover.mp3",
            extension="mp3",
            bitrate=320,
            free_slot=True,
            speed=5_000,
        ),
        RemoteFile("other", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=100),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    by_song = {match.song: match.username for match in pick.matches}
    assert by_song == {"Hungover": "free", "Cowboy": "other"}
    assert pick.matched == 2
    assert pick.users == 2
    assert pick.missing == 1
    assert pick.line() == "2 wanted items matched from 2 users, and 1 wanted item not found."


def test_a_gap_in_the_catalog_does_not_split_a_folder_that_has_the_rest():
    tracks = _hungover_songs()
    files = [
        RemoteFile("album", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", speed=100),
        RemoteFile("album", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", speed=100),
        RemoteFile(
            "single",
            r"Ella Langley\Hungover\01 Hungover.flac",
            extension="flac",
            free_slot=True,
            speed=9_000,
        ),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    assert {match.username for match in pick.matches} == {"album"}
    assert pick.missing == 1
    assert pick.users == 1


def test_downloaded_and_queued_files_are_not_queued_again():
    tracks = _hungover_songs()[:2]
    files = [
        RemoteFile("old", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", state="downloaded"),
        RemoteFile(
            "new",
            r"Ella Langley\Hungover\01 Hungover.flac",
            extension="flac",
            free_slot=True,
            speed=9_000,
        ),
        RemoteFile("new", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", free_slot=True, speed=9_000),
        RemoteFile("new", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", state="queued"),
        RemoteFile("new", r"Ella Langley\Hungover\02 Cowboy.flac", extension="flac", state=""),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    assert pick.matches == []
    assert pick.skipped == 2
    assert pick.missing == 0
    assert pick.line() == "0 wanted items matched from 0 users, and 0 wanted items not found."


def test_duplicate_paths_are_queued_once_and_low_bitrate_mp3_is_rejected():
    tracks = _hungover_songs()[:1]
    files = [
        RemoteFile("peer", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", size=10),
        RemoteFile("peer", r"Ella Langley\Hungover\01 Hungover.flac", extension="flac", size=10),
        RemoteFile("peer", r"Ella Langley\Hungover\01 Hungover.mp3", extension="mp3", bitrate=128),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    assert len(pick.matches) == 1
    assert pick.matches[0].filename.endswith(".flac")
    assert pick.matched == 1
    assert pick.missing == 0


def test_album_without_songs_keeps_one_users_folder():
    tracks = [WantedTrack("Ella Langley", "Hungover", "", "album:1", "album", album_id=1)]
    files = [
        RemoteFile("slow", r"Ella Langley\Hungover\01 Blue.flac", extension="flac", speed=100),
        RemoteFile("slow", r"Ella Langley\Hungover\02 Red.flac", extension="flac", speed=100),
        RemoteFile("slow", r"Ella Langley\Hungover\01 Blue.mp3", extension="mp3", bitrate=320, speed=100),
        RemoteFile(
            "fast",
            r"Ella Langley\Hungover\01 Blue.flac",
            extension="flac",
            free_slot=True,
            speed=9_000,
        ),
    ]
    pick = choose_wanted_downloads(files, tracks, **_pick_options())
    assert pick.matched == 1
    assert pick.users == 1
    assert pick.missing == 0
    assert {match.username for match in pick.matches} == {"slow"}
    assert len(pick.matches) == 2
    assert all(match.filename.endswith(".flac") for match in pick.matches)

    tied = [
        RemoteFile("busy", r"Ella Langley\Hungover\01 Blue.flac", extension="flac", speed=9_000),
        RemoteFile("busy", r"Ella Langley\Hungover\02 Red.flac", extension="flac", speed=9_000),
        RemoteFile("free", r"Ella Langley\Hungover\01 Blue.mp3", extension="mp3", bitrate=320, free_slot=True, speed=100),
        RemoteFile("free", r"Ella Langley\Hungover\02 Red.mp3", extension="mp3", bitrate=320, free_slot=True, speed=100),
    ]
    free_pick = choose_wanted_downloads(tied, tracks, **_pick_options())
    assert {match.username for match in free_pick.matches} == {"free"}


def test_narrow_wanted_tracks_keeps_the_search_target():
    tracks = _hungover_songs()
    album = narrow_wanted_tracks(tracks, kind="album", album="Hungover", album_id=1)
    assert {track.key for track in album} == {"song:1", "song:2"}
    song = narrow_wanted_tracks(tracks, kind="song", album="Still", title="Nicotine", track_id=3)
    assert [track.key for track in song] == ["song:3"]
    artist = narrow_wanted_tracks(tracks, kind="artist")
    assert len(artist) == 3


def test_plan_wanted_downloads_uses_the_artist_and_skips_queued_files(tmp_path):
    worker = make_worker(tmp_path)
    try:
        items = build_catalog(
            [
                album(1, "Ella Langley", "Hungover", files=0, total=2),
                album(2, "Someone Else", "Other", files=0, total=1),
            ],
            [
                hole(11, 1, "Ella Langley", "Hungover", "Hungover"),
                hole(12, 1, "Ella Langley", "Hungover", "Cowboy", number=2),
                hole(21, 2, "Someone Else", "Other", "Hungover"),
            ],
        )
        worker.store.write_wanted_page(items_to_rows(items))

        class File:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        files = [
            File(
                username="slow",
                filename=r"Ella Langley\Hungover\01 Hungover.flac",
                extension="flac",
                size=10,
                bitrate=0,
                speed=100,
                free_slot=False,
                queue=2,
                state="",
                folder="",
            ),
            File(
                username="slow",
                filename=r"Ella Langley\Hungover\02 Cowboy.flac",
                extension="flac",
                size=10,
                bitrate=0,
                speed=100,
                free_slot=False,
                queue=2,
                state="queued",
                folder="",
            ),
            File(
                username="fast",
                filename=r"Ella Langley\Hungover\02 Cowboy.flac",
                extension="flac",
                size=10,
                bitrate=0,
                speed=8_000,
                free_slot=True,
                queue=0,
                state="",
                folder="",
            ),
            File(
                username="other",
                filename=r"Someone Else\Other\01 Hungover.flac",
                extension="flac",
                size=10,
                bitrate=0,
                speed=9_000,
                free_slot=True,
                queue=0,
                state="",
                folder="",
            ),
        ]
        plan = worker.plan_wanted_downloads(files, artist="Ella Langley")
        songs = {match.song: match.username for match in plan.matches}
        assert songs.get("Hungover") == "slow"
        assert "Cowboy" not in songs
        assert all(match.artist == "Ella Langley" for match in plan.matches)
        assert plan.skipped == 1
        assert plan.missing == 0
        assert plan.users == 1
    finally:
        worker.close()


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
