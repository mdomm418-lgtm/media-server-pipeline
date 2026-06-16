"""Tests for anime_auto_sorter classification logic."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "anime_auto_sorter.py"
spec = importlib.util.spec_from_file_location("anime_auto_sorter", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules["anime_auto_sorter"] = mod
assert spec.loader is not None
spec.loader.exec_module(mod)


def _series(
    title: str,
    *,
    path: str = "/data/tv/Example",
    series_type: str = "standard",
    genres: list[str] | None = None,
    lang: str = "English",
    tvdb_id: int = 1,
) -> dict:
    return {
        "id": tvdb_id,
        "title": title,
        "path": path,
        "seriesType": series_type,
        "genres": genres or [],
        "originalLanguage": {"name": lang},
        "tvdbId": tvdb_id,
    }


def test_tier1_anime_series_type():
    s = _series("Noragami", series_type="anime", genres=["Anime"], lang="Japanese", tvdb_id=275610)
    c = mod.classify_series(s, tvdb_anime_ids=set())
    assert c is not None
    assert c.tier == mod.TIER_MOVE
    assert "seriesType=anime" in c.reason


def test_tier2_donghua_genre_and_language():
    s = _series(
        "Link Click",
        genres=["Action", "Anime", "Fantasy"],
        lang="Chinese",
        tvdb_id=402033,
    )
    c = mod.classify_series(s, tvdb_anime_ids=set())
    assert c is not None
    assert c.tier == mod.TIER_MOVE


def test_tier2_anime_list_hit():
    s = _series(
        "Some Anime",
        genres=["Anime"],
        lang="English",
        tvdb_id=999,
    )
    c = mod.classify_series(s, tvdb_anime_ids={999})
    assert c is not None
    assert c.tier == mod.TIER_MOVE


def test_ambiguous_english_anime_genre():
    s = _series(
        "Ambiguous Cartoon",
        genres=["Animation", "Anime"],
        lang="English",
        tvdb_id=12345,
    )
    c = mod.classify_series(s, tvdb_anime_ids=set())
    assert c is not None
    assert c.tier == mod.TIER_REVIEW


def test_western_not_classified():
    s = _series("The Bear", genres=["Comedy", "Drama"], lang="English", tvdb_id=367147)
    assert mod.classify_series(s, tvdb_anime_ids=set()) is None


def test_not_under_tv_root():
    s = _series("Noragami", path="/data/anime/Noragami", series_type="anime", genres=["Anime"], lang="Japanese")
    assert mod.classify_series(s, tvdb_anime_ids=set()) is None


def test_denylist_blocks_move():
    denylist = {"tvdbIds": [275610], "titles": []}
    all_series = [
        _series("Noragami", series_type="anime", genres=["Anime"], lang="Japanese", tvdb_id=275610),
        _series("Link Click", genres=["Anime"], lang="Chinese", tvdb_id=402033),
    ]
    to_move, to_review, denied = mod.classify_all(all_series, denylist, tvdb_anime_ids=set())
    assert len(denied) == 1
    assert denied[0].series["title"] == "Noragami"
    assert len(to_move) == 1
    assert to_move[0].series["title"] == "Link Click"
    assert to_review == []
