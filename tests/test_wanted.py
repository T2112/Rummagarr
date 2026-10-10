from __future__ import annotations

from tests.conftest import album, hole, track
from wanted import (
    VIEW_ALBUMS,
    VIEW_ARTISTS,
    VIEW_EVERYTHING,
    VIEW_SONGS,
    album_key,
    albums_for_search,
    build_catalog,
    catalog_from_lidarr,
    choice_updates,
    filter_view,
    holes_for_search,
    is_enabled,
    item_checked,
    select_all_state,
    select_all_updates,
    song_key,
)


def catalog():
    albums = [
        album(1, "Ella Langley", "Hungover", files=0, total=12),
        album(2, "Ella Langley", "Still", files=2, total=10),
        album(3, "Chris Stapleton", "Traveller", files=0, total=14, artist_id=2),
    ]
    holes = [
        hole(21, 2, "Ella Langley", "Still", "Cowboy", number=3),
        hole(31, 3, "Chris Stapleton", "Traveller", "Tennessee Whiskey", number=1),
    ]
    return build_catalog(albums, holes), albums, holes


def test_views_split_artists_albums_and_songs():
    items, _albums, _holes = catalog()
    assert [item.kind for item in filter_view(items, VIEW_EVERYTHING)].count("artist") == 2
    assert [item.artist for item in filter_view(items, VIEW_ARTISTS)] == ["Chris Stapleton", "Ella Langley"]
    assert [item.album for item in filter_view(items, VIEW_ALBUMS)] == ["Traveller", "Hungover", "Still"]
    assert [item.song for item in filter_view(items, VIEW_SONGS)] == ["Tennessee Whiskey", "Cowboy"]


def test_new_keys_default_to_checked_and_artist_toggle_cascades():
    items, _albums, _holes = catalog()
    choices: dict[str, bool] = {}
    ella = next(item for item in items if item.kind == "artist" and item.artist == "Ella Langley")
    assert item_checked(ella, items, choices) is True
    choices.update(choice_updates(ella, False, items))
    assert choices[album_key(1)] is False
    assert choices[album_key(2)] is False
    assert choices[song_key(21)] is False
    assert is_enabled(album_key(3), choices) is True
    assert item_checked(ella, items, choices) is False
    choices.update(choice_updates(ella, True, items))
    assert item_checked(ella, items, choices) is True


def test_select_all_is_scoped_to_the_current_view():
    items, _albums, _holes = catalog()
    songs = filter_view(items, VIEW_SONGS)
    choices = select_all_updates(songs, items, False)
    assert choices == {song_key(21): False, song_key(31): False}
    assert select_all_state(songs, items, choices) == "none"
    assert select_all_state(filter_view(items, VIEW_ALBUMS), items, choices) == "all"
    mixed = dict(choices)
    mixed[song_key(21)] = True
    assert select_all_state(songs, items, mixed) == "some"


def test_worker_filters_use_saved_choices_and_default_on():
    albums = [album(1, "Ada", "One"), album(2, "Ada", "Two")]
    holes = [hole(9, 2, "Ada", "Two", "Missing")]
    assert [row["id"] for row in albums_for_search(albums, {})] == [1, 2]
    assert [row["id"] for row in albums_for_search(albums, {album_key(2): False})] == [1]
    assert [row["track"]["id"] for row in holes_for_search(holes, {song_key(9): False})] == []
    assert [row["track"]["id"] for row in holes_for_search(holes, {})] == [9]


class FakeLidarr:
    def __init__(self) -> None:
        self.albums = [album(4, "Tyler Childers", "Purgatory", files=1, total=3, artist_id=8)]
        self.tracks = [
            track(40, "Kept", has_file=True, number=1),
            track(41, "Feathered Indians", number=2),
            track(42, "Unmonitored", number=3),
        ]
        self.tracks[2]["monitored"] = False

    def wanted_missing(self, **_kwargs):
        return self.albums, 1, True

    def tracks_for_album(self, album_id: int):
        assert album_id == 4
        return self.tracks


def test_vinyl_track_numbers_stay_visible_and_sort():
    albums = [album(7, "Miles Davis", "Kind of Blue", files=1, total=8)]
    holes = [
        hole(1, 7, "Miles Davis", "Kind of Blue", "No number", number=""),
        hole(2, 7, "Miles Davis", "Kind of Blue", "Side B late", number="B12"),
        hole(3, 7, "Miles Davis", "Kind of Blue", "Side A", number="A1"),
        hole(4, 7, "Miles Davis", "Kind of Blue", "Suffix", number="1a"),
        hole(5, 7, "Miles Davis", "Kind of Blue", "Ten", number="10"),
        hole(6, 7, "Miles Davis", "Kind of Blue", "Two", number="2"),
        hole(8, 7, "Miles Davis", "Kind of Blue", "Side A two", number="A2"),
        hole(9, 7, "Miles Davis", "Kind of Blue", "Disc two", number="A1"),
    ]
    holes[-1]["track"]["mediumNumber"] = 2
    items = build_catalog(albums, holes)
    songs = [item for item in items if item.kind == "song"]
    assert [item.song for item in songs] == [
        "Suffix",
        "Two",
        "Ten",
        "Side A",
        "Side A two",
        "Side B late",
        "No number",
        "Disc two",
    ]
    details = {item.song: item.detail for item in songs}
    assert details["Side A"] == "Track A1"
    assert details["Side A two"] == "Track A2"
    assert details["Side B late"] == "Track B12"
    assert details["Suffix"] == "Track 1a"
    assert details["Ten"] == "Track 10"
    assert details["No number"] == "Missing song"
    assert details["Disc two"] == "Disc 2 · Track A1"


def test_one_bad_lidarr_row_does_not_abort_sync():
    class Client:
        def wanted_missing(self, **_kwargs):
            return [
                album(7, "Miles Davis", "Kind of Blue", files=1, total=4),
                {"title": "Broken record", "artist": {"artistName": "Nobody"}},
            ], 1, True

        def tracks_for_album(self, album_id: int):
            assert album_id == 7
            return [
                {
                    "id": 70,
                    "title": "So What",
                    "trackNumber": "A1",
                    "mediumNumber": 1,
                    "monitored": True,
                    "hasFile": False,
                },
                {"id": "A1", "title": "Bad id", "trackNumber": "1", "monitored": True, "hasFile": False},
                {"title": "No id", "trackNumber": "B2", "monitored": True, "hasFile": False},
                "not-a-track",
                {
                    "id": 71,
                    "title": "Freddie Freeloader",
                    "trackNumber": "B12",
                    "mediumNumber": "B",
                    "monitored": True,
                    "hasFile": False,
                },
            ]

    items, exhausted = catalog_from_lidarr(Client())
    assert exhausted is True
    songs = [item for item in items if item.kind == "song"]
    assert [item.song for item in songs] == ["So What", "Freddie Freeloader"]
    assert songs[0].detail == "Track A1"
    assert songs[1].detail == "Disc B · Track B12"
    assert [item.album for item in items if item.kind == "album"] == ["Kind of Blue"]


def test_catalog_from_lidarr_keeps_missing_monitored_songs_only():
    items, exhausted = catalog_from_lidarr(FakeLidarr(), max_pages=3)
    assert exhausted is True
    songs = filter_view(items, VIEW_SONGS)
    assert [item.song for item in songs] == ["Feathered Indians"]
    assert songs[0].track_id == 41
    assert songs[0].payload["id"] == 4
