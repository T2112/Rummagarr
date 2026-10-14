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


class _Rated(_File):
    def __init__(self, filename: str, bitrate: int = 0, duration: float = 0) -> None:
        super().__init__(filename)
        self._bitrate = bitrate
        self._duration = duration

    def get_attribute_map(self):
        from aioslsk.protocol.primitives import AttributeKey

        attrs = {}
        if self._bitrate:
            attrs[AttributeKey.BITRATE] = self._bitrate
        if self._duration:
            attrs[AttributeKey.DURATION] = self._duration
        return attrs


def _hungover(files):
    return FolderCandidate(
        username="one-user",
        remote_dir=r"Ella Langley\Hungover",
        files=files,
    )


def test_one_folder_keeps_a_single_format():
    mixed = _hungover(
        [
            _Rated(r"Ella Langley\Hungover\01 Hungover.mp3", bitrate=320, duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.mp3", bitrate=320, duration=200),
            _Rated(r"Ella Langley\Hungover\01 Hungover.flac", duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.flac", duration=200),
        ]
    )
    scored = score_folder(
        mixed,
        "Ella Langley",
        "Hungover",
        ["Hungover", "Blue"],
        ["mp3", "flac"],
        0.5,
        2,
        320,
    )
    assert scored.reason == "ok"
    assert {scored.files[0].filename.rsplit(".", 1)[-1]} == {"mp3"}
    assert all(path.endswith(".mp3") for path in (item.filename for item in scored.files))


def test_lossless_profile_skips_mp3_and_standard_skips_below_cutoff():
    from match import gate_from_profile

    lossless = gate_from_profile({"name": "Lossless", "cutoff": 1, "items": [{"quality": {"id": 1, "name": "FLAC"}, "allowed": True}]}, ["mp3", "flac"], 320)
    flac = _hungover(
        [
            _Rated(r"Ella Langley\Hungover\01 Hungover.flac", duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.flac", duration=200),
        ]
    )
    mp3 = _hungover(
        [
            _Rated(r"Ella Langley\Hungover\01 Hungover.mp3", bitrate=320, duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.mp3", bitrate=320, duration=200),
        ]
    )
    assert score_folder(flac, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320, gate=lossless).reason == "ok"
    assert score_folder(mp3, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320, gate=lossless).reason == "below quality cutoff"

    standard = gate_from_profile(
        {"name": "Standard", "cutoff": 9, "items": [{"quality": {"id": 9, "name": "MP3-320"}, "allowed": True}]},
        ["mp3", "flac"],
        320,
    )
    low = _hungover(
        [
            _Rated(r"Ella Langley\Hungover\01 Hungover.mp3", bitrate=128, duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.mp3", bitrate=128, duration=200),
        ]
    )
    assert score_folder(low, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320, gate=standard).reason != "ok"
    assert score_folder(mp3, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320, gate=standard).reason == "ok"


def test_duration_and_studio_outrank_a_live_copy():
    studio = _hungover(
        [
            _Rated(r"Ella Langley\Hungover\01 Hungover.mp3", bitrate=320, duration=180),
            _Rated(r"Ella Langley\Hungover\02 Blue.mp3", bitrate=320, duration=200),
        ]
    )
    live = FolderCandidate(
        username="other",
        remote_dir=r"Ella Langley\Hungover Live",
        files=[
            _Rated(r"Ella Langley\Hungover Live\01 Hungover Live.mp3", bitrate=320, duration=240),
            _Rated(r"Ella Langley\Hungover Live\02 Blue Live.mp3", bitrate=320, duration=260),
        ],
    )
    studio_score = score_folder(
        studio, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320,
        durations=[180, 200],
    )
    live_score = score_folder(
        live, "Ella Langley", "Hungover", ["Hungover", "Blue"], ["mp3", "flac"], 0.5, 2, 320,
        durations=[180, 200],
    )
    assert studio_score.reason == "ok"
    assert live_score.reason == "ok"
    assert studio_score.score > live_score.score
    live_points = live_score.score

    titled = score_folder(
        FolderCandidate(
            username="other",
            remote_dir=r"Ella Langley\Hungover Live",
            files=[
                _Rated(r"Ella Langley\Hungover Live\01 Hungover Live.mp3", bitrate=320, duration=240),
                _Rated(r"Ella Langley\Hungover Live\02 Blue Live.mp3", bitrate=320, duration=260),
            ],
        ),
        "Ella Langley",
        "Hungover Live",
        ["Hungover Live", "Blue Live"],
        ["mp3", "flac"],
        0.5,
        2,
        320,
        durations=[240, 260],
    )
    assert titled.score > live_points
