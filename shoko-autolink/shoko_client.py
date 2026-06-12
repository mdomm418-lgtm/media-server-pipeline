from __future__ import annotations

import time
from typing import Any

import requests

from models import EpisodeMap, VideoFile


class ShokoClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        device: str = "shoko-autolink",
        delay_ms: int = 200,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.device = device
        self.delay_ms = delay_ms
        self._apikey: str | None = None
        self._session = requests.Session()

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> ShokoClient:
        s = cfg["shoko"]
        b = cfg.get("behavior", {})
        return cls(
            base_url=s["base_url"],
            username=s["username"],
            password=s["password"],
            device=s.get("device", "shoko-autolink"),
            delay_ms=b.get("shoko_request_delay_ms", 200),
        )

    def _headers(self) -> dict[str, str]:
        if not self._apikey:
            raise RuntimeError("Not authenticated")
        return {"apikey": self._apikey}

    def _pause(self) -> None:
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)

    def authenticate(self) -> None:
        r = self._session.post(
            f"{self.base_url}/api/auth",
            json={"user": self.username, "pass": self.password, "device": self.device},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        self._apikey = data.get("apikey") or data.get("token")
        if not self._apikey:
            raise RuntimeError(f"Shoko auth failed: {data}")

    def list_unrecognized_files(self) -> list[VideoFile]:
        out: list[VideoFile] = []
        page = 1
        page_size = 200
        while True:
            self._pause()
            params: dict[str, Any] = {
                "include_only": "Unrecognized",
                "pageSize": page_size,
                "page": page,
            }
            r = self._session.get(
                f"{self.base_url}/api/v3/File",
                headers=self._headers(),
                params=params,
                timeout=120,
            )
            r.raise_for_status()
            data = r.json()
            rows = data.get("List", data if isinstance(data, list) else [])
            for row in rows:
                vf = VideoFile.from_shoko(row)
                if vf:
                    out.append(vf)
            total = int(data.get("Total", len(out)))
            if len(out) >= total or not rows:
                break
            page += 1
        return out

    def get_anidb_ban_status(self) -> bool:
        """Query Shoko API to check if an AniDB HTTP ban is active."""
        try:
            self._pause()
            r = self._session.get(
                f"{self.base_url}/api/v3/AniDB/BanStatus",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("HTTP", {}).get("IsBanned", False)
        except Exception as e:
            # If we fail to get the status, assume it's banned to be safe
            print(f"Warning: Failed to check AniDB ban status: {e}")
            return True

    def trigger_import(self) -> None:
        """Trigger Shoko to run an import scan (finds new files)."""
        try:
            self._pause()
            self._session.get(
                f"{self.base_url}/api/v3/Action/RunImport",
                headers=self._headers(),
                timeout=10,
            )
        except Exception as e:
            print(f"Warning: Failed to trigger Shoko import: {e}")

    def refresh_anidb_series(self, anidb_id: int, delay_sec: float = 8, ban_active: bool = False) -> None:
        if ban_active:
            return  # Skip refresh during AniDB ban
        self._pause()
        r = self._session.post(
            f"{self.base_url}/api/v3/Series/AniDB/{anidb_id}/Refresh",
            headers=self._headers(),
            params={"createSeriesEntry": "true", "immediate": "true"},
            timeout=120,
        )
        r.raise_for_status()
        if delay_sec > 0:
            time.sleep(delay_sec)

    def get_shoko_series_id(self, anidb_id: int) -> int | None:
        self._pause()
        r = self._session.get(
            f"{self.base_url}/api/v3/Series/AniDB/{anidb_id}",
            headers=self._headers(),
            timeout=60,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            sid = data.get("ShokoID") or data.get("ID")
            return int(sid) if sid is not None else None
        return None

    def get_episode_map(self, series_id: int) -> EpisodeMap:
        """Map Sonarr/Shoko episode keys to Shoko EpisodeID."""
        self._pause()
        r = self._session.get(
            f"{self.base_url}/api/v3/Series/{series_id}/Episode",
            headers=self._headers(),
            params={
                "pageSize": 0,
                "includeMissing": True,
                "includeDataFrom": "AniDB",
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        eps = data if isinstance(data, list) else data.get("List", [])
        by_key: dict[tuple[str, int], int] = {}
        by_tvdb: dict[int, int] = {}
        for ep in eps:
            eid = ep.get("IDs", {}).get("ID")
            if eid is None:
                continue
            eid = int(eid)
            anidb = ep.get("AniDB") or {}
            ep_type = str(anidb.get("Type") or "Episode")
            ep_num = anidb.get("EpisodeNumber")
            if ep_num is not None:
                by_key[(ep_type, int(ep_num))] = eid
                if ep_type == "Episode":
                    by_key[("Regular", int(ep_num))] = eid
            for tvdb_id in ep.get("IDs", {}).get("TvDB") or []:
                by_tvdb[int(tvdb_id)] = eid
        return EpisodeMap(by_key=by_key, by_tvdb=by_tvdb)

    def link_file_to_episode(self, file_id: int, episode_id: int, max_retries: int = 2) -> None:
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                self._pause()
                r = self._session.post(
                    f"{self.base_url}/api/v3/File/{file_id}/Link",
                    headers=self._headers(),
                    json={"EpisodeIDs": [episode_id]},
                    timeout=60,
                )
                r.raise_for_status()
                return
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
        raise last_error

    def get_file(self, file_id: int) -> dict[str, Any]:
        self._pause()
        r = self._session.get(
            f"{self.base_url}/api/v3/File/{file_id}",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def file_is_linked(self, file_id: int) -> bool:
        try:
            data = self.get_file(file_id)
            return bool(data.get("Episode") or data.get("Episodes"))
        except requests.HTTPError:
            return False
