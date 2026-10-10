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


def test_catalog_from_lidarr_keeps_missing_monitored_songs_only():
    items, exhausted = catalog_from_lidarr(FakeLidarr(), max_pages=3)
    assert exhausted is True
    songs = filter_view(items, VIEW_SONGS)
    assert [item.song for item in songs] == ["Feathered Indians"]
    assert songs[0].track_id == 41
    assert songs[0].payload["id"] == 4
