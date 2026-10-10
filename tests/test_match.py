from __future__ import annotations

from match import (
    FolderCandidate,
    aliases_from_album,
    build_queries,
    build_track_queries,
    normalize,
    score_folder,
    similarity,
)


class _File:
    def __init__(self, filename: str) -> None:
        self.filename = filename

    def get_attribute_map(self):
        return {}


FOLDED_PAIRS = (
    ("Mötley Crüe", "Motley Crue"),
    ("Beyoncé", "Beyonce"),
    ("Sigur Rós", "Sigur Ros"),
    ("Queensrÿche", "Queensryche"),
    ("Blue Öyster Cult", "Blue Oyster Cult"),
    ("Björk", "Bjork"),
    ("AC/DC", "AC DC"),
    ("Guns N' Roses", "Guns N\u2019 Roses"),
    ("P!nk", "Pink"),
    ("Ke$ha", "Kesha"),
    ("Simon & Garfunkel", "Simon and Garfunkel"),
)


def test_special_names_fold_to_the_ascii_spelling():
    for left, right in FOLDED_PAIRS:
        assert normalize(left) == normalize(right), (left, normalize(left), right, normalize(right))
        assert similarity(left, right) == 1.0


def test_folded_names_do_not_become_a_different_artist():
    assert normalize("Björk") != normalize("Bjorn")
    assert normalize("Mötley Crüe") != normalize("Metallica")
    assert normalize("P!nk") != normalize("Pink Floyd")
    assert similarity("Mötley Crüe", "Metallica") < 0.5
    assert similarity("Björk", "Bjorn") < 1.0


def test_search_uses_the_folded_query_then_the_original():
    queries = build_queries("Mötley Crüe", "Dr. Feelgood", "1989")
    assert queries[0] == "motley crue dr feelgood 1989"
    assert "motley crue dr feelgood" in queries
    assert any("Mötley Crüe" in query and "Dr. Feelgood" in query for query in queries)
    assert all("m tley" not in query for query in queries)

    pink = build_queries("P!nk", "Funhouse", "2008")
    assert pink[0] == "pink funhouse 2008"
    assert any(query.startswith("P!nk Funhouse") for query in pink)

    kesha = build_track_queries("Ke$ha", "Animal", "TiK ToK")
    assert kesha[0] == "kesha animal tik tok"
    assert any(query.startswith("Ke$ha") for query in kesha)


def test_apostrophes_and_slashes_already_share_one_folded_query():
    straight = build_queries("Guns N' Roses", "Appetite for Destruction", "1987")
    curly = build_queries("Guns N\u2019 Roses", "Appetite for Destruction", "1987")
    assert straight[0] == curly[0] == "guns n roses appetite for destruction 1987"
    acdc = build_queries("AC/DC", "Back in Black", "1980")
    assert acdc[0] == "ac dc back in black 1980"
    assert any(query.startswith("AC/DC ") for query in acdc)


def test_a_placeholder_year_is_left_off_the_query():
    queries = build_queries("Björk", "Fossora", "2026")
    assert queries[0] == "bjork fossora"
    assert all("2026" not in query for query in queries)
    assert any(query.startswith("Björk") for query in queries)


def test_aliases_are_extra_terms_and_duplicates_are_skipped():
    album = {
        "artist": {
            "artistName": "Mötley Crüe",
            "aliases": [
                "Motley Crue",
                {"name": "Mötley Crüe"},
                {"aliasName": "The Crue"},
                "Mötley",
                "MC",
            ],
        }
    }
    aliases = aliases_from_album(album)
    assert aliases == ["The Crue", "Mötley", "MC"]
    queries = build_queries("Mötley Crüe", "Dr. Feelgood", None, aliases=aliases)
    assert "the crue dr feelgood" in queries
    assert "motley dr feelgood" in queries
    assert "mc dr feelgood" in queries
    assert queries.count("motley crue dr feelgood") == 1


def test_only_three_aliases_are_searched():
    album = {
        "artist": {
            "artistName": "Prince",
            "aliases": ["The Artist", "Jamie Starr", "Camille", "Joey Coco"],
        }
    }
    assert aliases_from_album(album) == ["The Artist", "Jamie Starr", "Camille"]
    queries = build_queries("Prince", "Purple Rain", "1984", aliases=aliases_from_album(album))
    assert "the artist purple rain 1984" in queries
    assert "camille purple rain 1984" in queries
    assert not any(query.startswith("joey coco") for query in queries)


def test_ascii_folder_matches_and_the_wrong_artist_does_not():
    motley = FolderCandidate(
        username="sharer",
        remote_dir=r"Motley Crue\Dr Feelgood",
        files=[
            _File(r"Motley Crue\Dr Feelgood\01 Kickstart My Heart.mp3"),
            _File(r"Motley Crue\Dr Feelgood\02 Dr Feelgood.mp3"),
        ],
    )
    scored = score_folder(
        motley,
        "Mötley Crüe",
        "Dr. Feelgood",
        ["Kickstart My Heart", "Dr. Feelgood"],
        ["mp3", "flac", "wav"],
        0.5,
        2,
        320,
    )
    assert scored.reason == "ok"
    assert scored.score > 0

    metallica = FolderCandidate(
        username="sharer",
        remote_dir=r"Metallica\Master of Puppets",
        files=[
            _File(r"Metallica\Master of Puppets\01 Battery.mp3"),
            _File(r"Metallica\Master of Puppets\02 Master of Puppets.mp3"),
        ],
    )
    rejected = score_folder(
        metallica,
        "Mötley Crüe",
        "Dr. Feelgood",
        ["Kickstart My Heart", "Dr. Feelgood"],
        ["mp3", "flac", "wav"],
        0.5,
        2,
        320,
    )
    assert rejected.reason != "ok"
    assert rejected.score == 0
