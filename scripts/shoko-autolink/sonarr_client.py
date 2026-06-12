from __future__ import annotations

import os
import re as _re
import unicodedata
from typing import Any

import requests


def _normalize_folder(name: str) -> str:
    """Normalize folder name for fuzzy matching."""
    # Normalize unicode
    name = unicodedata.normalize('NFKD', name)
    # Remove non-alphanumeric (keep spaces)
    name = _re.sub(r'[^\w\s]', '', name)
    # Collapse whitespace and lowercase
    return ' '.join(name.lower().split())


class SonarrClient:
    def __init__(self, base_url: str, api_key: str, anime_root_prefix: str = "/data/anime"):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.anime_root_prefix = anime_root_prefix.rstrip("/")
        self._series_by_folder: dict[str, dict[str, Any]] | None = None
        self._series_by_normalized: dict[str, dict[str, Any]] = {}
        self._episodes_cache: dict[int, list[dict[str, Any]]] = {}
        self._episode_files_cache: dict[int, list[dict[str, Any]]] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> SonarrClient:
        s = cfg["sonarr"]
        return cls(s["base_url"], s["api_key"], s.get("anime_root_prefix", "/data/anime"))

    def _get(self, path: str, **params: Any) -> Any:
        r = requests.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            params=params or None,
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def load_anime_series_index(self) -> dict[str, dict[str, Any]]:
        if self._series_by_folder is not None:
            return self._series_by_folder
        series = self._get("/api/v3/series")
        index: dict[str, dict[str, Any]] = {}
        for s in series:
            path = s.get("path", "")
            if not path.startswith(self.anime_root_prefix + "/") and path != self.anime_root_prefix:
                continue
            folder = os.path.basename(path.rstrip("/"))
            index[folder] = s
        self._series_by_folder = index
        self._series_by_normalized = {}
        for folder, s in index.items():
            norm = _normalize_folder(folder)
            self._series_by_normalized[norm] = s
        return index

    def get_series_by_folder(self, folder_name: str) -> dict[str, Any] | None:
        index = self.load_anime_series_index()
        exact = index.get(folder_name)
        if exact:
            return exact
        norm = _normalize_folder(folder_name)
        return self._series_by_normalized.get(norm)

    def get_episodes(self, series_id: int) -> list[dict[str, Any]]:
        if series_id not in self._episodes_cache:
            self._episodes_cache[series_id] = self._get("/api/v3/episode", seriesId=series_id)
        return self._episodes_cache[series_id]

    def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        if series_id not in self._episode_files_cache:
            self._episode_files_cache[series_id] = self._get(
                "/api/v3/episodefile", seriesId=series_id
            )
        return self._episode_files_cache[series_id]

    def find_episode_by_relative_path(
        self, series_id: int, relative_path: str
    ) -> dict[str, Any] | None:
        """Match Shoko relative path to Sonarr episodefile (hash/opaque filenames)."""
        norm = relative_path.replace("\\", "/").strip("/")
        base = os.path.basename(norm)
        for ef in self.get_episode_files(series_id):
            rp = (ef.get("relativePath") or "").replace("\\", "/").strip("/")
            if (
                rp == norm
                or norm.endswith(rp)
                or rp.endswith(norm)
                or os.path.basename(rp) == base
            ):
                ep_ids = ef.get("episodeIds") or []
                if not ep_ids:
                    return None
                ep_id = ep_ids[0]
                for ep in self.get_episodes(series_id):
                    if ep.get("id") == ep_id:
                        return ep
        return None

    def find_episode(
        self, series_id: int, season: int, episode: int, series_type: str = "anime"
    ) -> dict[str, Any] | None:
        season_eps = [ep for ep in self.get_episodes(series_id) if ep.get("seasonNumber") == season]
        if series_type == "anime":
            for ep in season_eps:
                if ep.get("episodeNumber") == episode:
                    return ep
            for ep in season_eps:
                if ep.get("absoluteEpisodeNumber") == episode:
                    return ep
        else:
            for ep in season_eps:
                if ep.get("episodeNumber") == episode:
                    return ep
        return None

    def uses_absolute_numbering(self, tvdb_id: int | None) -> bool:
        if tvdb_id is None:
            return False
        from anime_lists import AnimeListsDB  # noqa: circular - avoid

        return False  # linker passes flag from resolve result instead
