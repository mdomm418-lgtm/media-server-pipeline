#!/usr/bin/env python3
"""Relocate anime series from Sonarr /data/tv to /data/anime (reactive safety net).

ListSync/Seerr requests anime into the TV root folder; Sonarr correctly classifies many
as seriesType=anime but nothing relocates them. This cron script detects miscategorized
anime under /data/tv and moves them to /data/anime via Sonarr's editor API (symlink-safe
rename on the same filesystem), then triggers Shoko import + a guarded Jellyfin refresh.

Usage:
  anime_auto_sorter.py                 # dry-run (default)
  anime_auto_sorter.py --execute       # move (capped) + refresh downstream
  anime_auto_sorter.py --execute --limit 5
  anime_auto_sorter.py --status
  anime_auto_sorter.py --list-ambiguous
  anime_auto_sorter.py --no-refresh    # move only
  anime_auto_sorter.py --check         # mount-health only (exit 0=OK, 1=bad)

Cron (offset from jellyfin_safe_refresh to avoid double-scan stalls)::

  7,22,37,52 * * * * flock -n /home/admin/anime-auto-sorter/.lock \\
    /usr/bin/python3 /home/admin/anime_auto_sorter.py --execute \\
    >> /home/admin/anime-auto-sorter/anime-auto-sorter.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_PREFIX = "[anime_auto_sorter]"

SONARR_URL = os.environ.get("SONARR_URL", "http://127.0.0.1:8989/api/v3")
SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/opt/sonarr/config/config.xml")
TV_ROOT = os.environ.get("TV_ROOT", "/data/tv")
ANIME_ROOT = os.environ.get("ANIME_ROOT", "/data/anime")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096")
JELLYFIN_DB = os.environ.get("JELLYFIN_DB", "/opt/jellyfin/config/data/data/jellyfin.db")
SHOKO_URL = os.environ.get("SHOKO_URL", "http://127.0.0.1:8111")
SHOKO_ENV = os.environ.get("SHOKO_ENV", "/home/admin/shoko-autolink/.env")
ANIME_LIST_XML = os.environ.get("ANIME_LIST_XML", "/opt/shoko-autolink/data/anime-list.xml")
MOVE_LIMIT = int(os.environ.get("MOVE_LIMIT", "10"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/home/admin/anime-auto-sorter"))
DENYLIST_FILE = STATE_DIR / "denylist.json"
STATE_FILE = STATE_DIR / "state.json"
REVIEW_FILE = STATE_DIR / "review.json"
LOG_FILE = STATE_DIR / "anime-auto-sorter.log"

HOST_NZBDAV = os.environ.get("NZBDAV_MOUNT", "/mnt/nzbdav")
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", "/mnt/media")
HOST_MEDIA_PREFIX = os.environ.get("HOST_MEDIA_PREFIX", "/mnt/media")
CONTAINER_MEDIA_PREFIX = os.environ.get("CONTAINER_MEDIA_PREFIX", "/data")
CANARY_HOST = os.environ.get(
    "MOUNT_CANARY",
    "/mnt/media/anime/My Teen Romantic Comedy SNAFU/Season 1/"
    "My Teen Romantic Comedy SNAFU - S01E02 - All People Surely Have Their Own "
    "Worries Bluray-1080p.mkv",
)
CANARY_SCAN_LIMIT = int(os.environ.get("CANARY_SCAN_LIMIT", "400"))
CONTAINERS = os.environ.get("MEDIA_CONTAINERS", "sonarr,jellyfin").split(",")
ANIME_LANGS = frozenset(
    x.strip()
    for x in os.environ.get("ANIME_LANGS", "Chinese,Japanese,Korean").split(",")
    if x.strip()
)
MOVE_SETTLE_TIMEOUT = int(os.environ.get("MOVE_SETTLE_TIMEOUT", "120"))
MOVE_POLL_INTERVAL = float(os.environ.get("MOVE_POLL_INTERVAL", "2"))

TIER_MOVE = "move"
TIER_REVIEW = "review"
TIER_SKIP = "skip"

_CANARY_CACHE: tuple[str | None, str | None] | None = None
_TVDB_ANIME_CACHE: set[int] | None = None


@dataclass(frozen=True)
class ClassifiedSeries:
    series: dict[str, Any]
    tier: str
    reason: str


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {LOG_PREFIX} {msg}"
    print(line, flush=True)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_sonarr_api_key() -> str:
    env_key = os.environ.get("SONARR_API_KEY")
    if env_key:
        return env_key
    root = ET.parse(SONARR_CONFIG).getroot()
    key = root.findtext("ApiKey")
    if not key:
        raise RuntimeError(f"No ApiKey in {SONARR_CONFIG} and SONARR_API_KEY unset")
    return key


def sonarr_headers(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key, "Content-Type": "application/json"}


def sonarr_get(api_key: str, path: str) -> Any:
    req = urllib.request.Request(
        SONARR_URL + path, headers=sonarr_headers(api_key), method="GET"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def sonarr_put(api_key: str, path: str, body: dict[str, Any]) -> Any:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        SONARR_URL + path,
        data=data,
        headers=sonarr_headers(api_key),
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_denylist() -> dict[str, list[Any]]:
    data = load_json(DENYLIST_FILE, {"tvdbIds": [], "titles": []})
    data.setdefault("tvdbIds", [])
    data.setdefault("titles", [])
    return data


def load_state() -> dict[str, Any]:
    data = load_json(
        STATE_FILE,
        {"moved": [], "last_run_at": None, "lifetime_moved": 0},
    )
    data.setdefault("moved", [])
    return data


def load_review() -> list[dict[str, Any]]:
    return load_json(REVIEW_FILE, [])


def is_denied(series: dict[str, Any], denylist: dict[str, list[Any]]) -> bool:
    tvdb_id = series.get("tvdbId")
    title = (series.get("title") or "").lower()
    denied_ids = {int(x) for x in denylist.get("tvdbIds", []) if x is not None}
    denied_titles = {(t or "").lower() for t in denylist.get("titles", [])}
    if tvdb_id is not None and int(tvdb_id) in denied_ids:
        return True
    return title in denied_titles


def has_anime_genre(series: dict[str, Any]) -> bool:
    genres = series.get("genres") or []
    return any((g or "").lower() == "anime" for g in genres)


def original_language_name(series: dict[str, Any]) -> str:
    lang = series.get("originalLanguage") or {}
    if isinstance(lang, dict):
        return str(lang.get("name") or "")
    return str(lang)


def load_tvdb_anime_ids(xml_path: str | Path = ANIME_LIST_XML) -> set[int]:
    global _TVDB_ANIME_CACHE
    if _TVDB_ANIME_CACHE is not None:
        return _TVDB_ANIME_CACHE
    path = Path(xml_path)
    ids: set[int] = set()
    if path.is_file() and path.stat().st_size > 0:
        try:
            for node in ET.parse(path).getroot().findall("anime"):
                tvdb_raw = node.get("tvdbid")
                if not tvdb_raw or tvdb_raw == "movie":
                    continue
                try:
                    ids.add(int(tvdb_raw))
                except ValueError:
                    continue
        except ET.ParseError as e:
            log(f"WARN could not parse anime-list.xml: {e}")
    _TVDB_ANIME_CACHE = ids
    return ids


def in_anime_list(tvdb_id: int | None, tvdb_anime_ids: set[int]) -> bool:
    return tvdb_id is not None and int(tvdb_id) in tvdb_anime_ids


def classify_series(
    series: dict[str, Any],
    *,
    tv_root: str = TV_ROOT,
    tvdb_anime_ids: set[int] | None = None,
) -> ClassifiedSeries | None:
    path = series.get("path") or ""
    if not path.startswith(tv_root.rstrip("/") + "/") and path != tv_root.rstrip("/"):
        return None

    tvdb_ids = tvdb_anime_ids if tvdb_anime_ids is not None else load_tvdb_anime_ids()
    tvdb_id = series.get("tvdbId")
    series_type = series.get("seriesType")
    lang = original_language_name(series)
    anime_genre = has_anime_genre(series)
    list_hit = in_anime_list(tvdb_id, tvdb_ids)

    if series_type == "anime":
        return ClassifiedSeries(series, TIER_MOVE, "tier1: seriesType=anime")

    if anime_genre and lang in ANIME_LANGS:
        return ClassifiedSeries(
            series,
            TIER_MOVE,
            f"tier2: Anime genre + language={lang}",
        )

    if anime_genre and list_hit:
        return ClassifiedSeries(
            series,
            TIER_MOVE,
            f"tier2: Anime genre + anime-list.xml tvdbId={tvdb_id}",
        )

    if anime_genre:
        return ClassifiedSeries(
            series,
            TIER_REVIEW,
            f"ambiguous: Anime genre, language={lang or 'unknown'}, no anime-list hit",
        )

    return None


def classify_all(
    all_series: list[dict[str, Any]],
    denylist: dict[str, list[Any]],
    *,
    tvdb_anime_ids: set[int] | None = None,
) -> tuple[list[ClassifiedSeries], list[ClassifiedSeries], list[ClassifiedSeries]]:
    to_move: list[ClassifiedSeries] = []
    to_review: list[ClassifiedSeries] = []
    denied: list[ClassifiedSeries] = []

    ids = tvdb_anime_ids if tvdb_anime_ids is not None else load_tvdb_anime_ids()

    for series in all_series:
        classified = classify_series(series, tvdb_anime_ids=ids)
        if classified is None:
            continue
        if is_denied(series, denylist):
            denied.append(
                ClassifiedSeries(series, TIER_SKIP, "denylist"),
            )
            continue
        if classified.tier == TIER_MOVE:
            to_move.append(classified)
        elif classified.tier == TIER_REVIEW:
            to_review.append(classified)
    return to_move, to_review, denied


def run_cmd(cmd: list[str] | str, timeout: int = 60) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        return subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def to_container_path(host_path: str) -> str:
    if host_path.startswith(HOST_MEDIA_PREFIX):
        return host_path.replace(HOST_MEDIA_PREFIX, CONTAINER_MEDIA_PREFIX, 1)
    return host_path


def resolve_canary() -> tuple[str | None, str | None]:
    global _CANARY_CACHE
    if _CANARY_CACHE is not None:
        return _CANARY_CACHE

    if os.path.lexists(CANARY_HOST) and os.path.exists(CANARY_HOST):
        _CANARY_CACHE = (CANARY_HOST, to_container_path(CANARY_HOST))
        return _CANARY_CACHE

    log(f"configured canary missing ({CANARY_HOST}); auto-discovering a replacement")
    probed = 0
    for root, _dirs, files in os.walk(LIBRARY_ROOT):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                if not os.path.islink(p):
                    continue
                probed += 1
                if os.path.exists(p):
                    log(f"auto canary: {p}")
                    _CANARY_CACHE = (p, to_container_path(p))
                    return _CANARY_CACHE
            except OSError:
                continue
            if probed >= CANARY_SCAN_LIMIT:
                break
        if probed >= CANARY_SCAN_LIMIT:
            break

    _CANARY_CACHE = (None, None)
    return _CANARY_CACHE


def host_mount_ok() -> bool:
    if not os.path.ismount(HOST_NZBDAV):
        log(f"FAIL host: {HOST_NZBDAV} is not a mountpoint")
        return False
    try:
        os.listdir(HOST_NZBDAV)
    except OSError as e:
        log(f"FAIL host: cannot list {HOST_NZBDAV}: {e}")
        return False
    host_canary, _ = resolve_canary()
    if not host_canary:
        log("FAIL host: no resolvable canary symlink found under library")
        return False
    if not os.path.lexists(host_canary) or not os.path.exists(host_canary):
        log(f"FAIL host: canary broken {host_canary}")
        return False
    return True


def container_mount_ok(name: str) -> bool:
    name = name.strip()
    if not name:
        return True
    _, container_canary = resolve_canary()
    if not container_canary:
        return False
    cmd = (
        f"test -d /mnt/nzbdav && ls /mnt/nzbdav >/dev/null 2>&1 "
        f"&& test -e '{container_canary}'"
    )
    for prefix in ([], ["sudo"]):
        r = run_cmd([*prefix, "docker", "exec", name, "sh", "-c", cmd], timeout=30)
        if r.returncode == 0:
            return True
    log(f"FAIL container {name}: nzbdav mount or canary")
    return False


def mounts_ok() -> bool:
    if not host_mount_ok():
        return False
    return all(container_mount_ok(c) for c in CONTAINERS)


def wait_for_move_commands(api_key: str, timeout: int = MOVE_SETTLE_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    active_names = {"MoveSeries", "RefreshSeries", "RenameSeries"}
    while time.time() < deadline:
        cmds = sonarr_get(api_key, "/command")
        active = [
            c
            for c in cmds
            if c.get("name") in active_names
            and c.get("status") in ("queued", "started")
        ]
        if not active:
            return True
        time.sleep(MOVE_POLL_INTERVAL)
    log(f"WARN move commands still active after {timeout}s")
    return False


def move_series(
    api_key: str, targets: list[ClassifiedSeries], limit: int
) -> tuple[list[ClassifiedSeries], list[ClassifiedSeries]]:
    batch = targets[:limit]
    remainder = targets[limit:]
    if not batch:
        return [], remainder

    ids = [c.series["id"] for c in batch]
    log(f"MOVE issuing editor for {len(ids)} series -> {ANIME_ROOT}")
    sonarr_put(
        api_key,
        "/series/editor",
        {
            "seriesIds": ids,
            "rootFolderPath": ANIME_ROOT,
            "moveFiles": True,
        },
    )
    wait_for_move_commands(api_key)
    for item in batch:
        log(
            f"MOVE {item.series.get('title')} (id={item.series.get('id')}, "
            f"tvdb={item.series.get('tvdbId')}) reason={item.reason}"
        )
    return batch, remainder


def update_review_file(to_review: list[ClassifiedSeries]) -> None:
    if not to_review:
        return
    existing = {entry.get("tvdbId"): entry for entry in load_review()}
    now = datetime.now(timezone.utc).isoformat()
    for item in to_review:
        s = item.series
        tvdb_id = s.get("tvdbId")
        existing[tvdb_id] = {
            "title": s.get("title"),
            "tvdbId": tvdb_id,
            "path": s.get("path"),
            "reason": item.reason,
            "updatedAt": now,
        }
    save_json(REVIEW_FILE, sorted(existing.values(), key=lambda x: (x.get("title") or "")))


def record_moves(state: dict[str, Any], moved: list[ClassifiedSeries]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for item in moved:
        s = item.series
        state["moved"].append(
            {
                "title": s.get("title"),
                "tvdbId": s.get("tvdbId"),
                "seriesId": s.get("id"),
                "fromPath": s.get("path"),
                "reason": item.reason,
                "movedAt": now,
            }
        )
    state["lifetime_moved"] = int(state.get("lifetime_moved", 0)) + len(moved)
    state["last_run_at"] = now
    save_json(STATE_FILE, state)


def read_env_file(path: str | Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def shoko_run_import() -> bool:
    env = read_env_file(SHOKO_ENV)
    user = env.get("SHOKO_USERNAME") or os.environ.get("SHOKO_USERNAME")
    password = env.get("SHOKO_PASSWORD") or os.environ.get("SHOKO_PASSWORD")
    if not user or not password:
        log("WARN Shoko creds missing; skipping RunImport")
        return False

    auth_body = json.dumps(
        {"user": user, "pass": password, "device": "anime-auto-sorter"}
    ).encode()
    auth_req = urllib.request.Request(
        SHOKO_URL + "/api/auth",
        data=auth_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(auth_req, timeout=30) as resp:
        apikey = json.loads(resp.read()).get("apikey")
    if not apikey:
        log("WARN Shoko auth returned no apikey")
        return False

    import_req = urllib.request.Request(
        SHOKO_URL + "/api/v3/Action/RunImport",
        headers={"apikey": apikey},
        method="GET",
    )
    with urllib.request.urlopen(import_req, timeout=60) as resp:
        log(f"Shoko RunImport HTTP {resp.status}")
    return True


def jellyfin_token() -> str:
    conn = sqlite3.connect(JELLYFIN_DB)
    row = conn.execute(
        "SELECT AccessToken FROM ApiKeys ORDER BY DateCreated DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        raise RuntimeError("No Jellyfin API key in database")
    return row[0]


def jellyfin_library_scan_running(token: str) -> bool:
    req = urllib.request.Request(
        JELLYFIN_URL + "/ScheduledTasks",
        headers={"X-Emby-Token": token},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tasks = json.loads(resp.read())
    for task in tasks:
        name = (task.get("Name") or "").lower()
        state = task.get("State") or ""
        if "scan media library" in name and state.lower() != "idle":
            log(f"Jellyfin scan already running (state={state}); skipping refresh")
            return True
    return False


def jellyfin_refresh(token: str) -> bool:
    if jellyfin_library_scan_running(token):
        return False
    body = urllib.parse.urlencode(
        {
            "Recursive": "true",
            "MetadataRefreshMode": "Default",
            "ImageRefreshMode": "Default",
            "ReplaceAllMetadata": "false",
        }
    ).encode()
    req = urllib.request.Request(
        JELLYFIN_URL + "/Library/Refresh",
        data=body,
        method="POST",
        headers={
            "X-Emby-Token": token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        log(f"Jellyfin library refresh HTTP {resp.status}")
    return True


def report_untracked_orphans(api_key: str) -> None:
    """Report-only: unmapped folders under /data/tv that look anime-ish."""
    roots = sonarr_get(api_key, "/rootfolder")
    tv_root = next((r for r in roots if r.get("path") == TV_ROOT), None)
    if not tv_root:
        return
    unmapped = tv_root.get("unmappedFolders") or []
    if unmapped:
        names = [u.get("name") for u in unmapped]
        log(f"ORPHAN unmapped folders under {TV_ROOT}: {names}")


def print_status(api_key: str) -> None:
    denylist = load_denylist()
    state = load_state()
    all_series = sonarr_get(api_key, "/series")
    to_move, to_review, denied = classify_all(all_series, denylist)
    mount_ok = mounts_ok()

    print(f"Mount health: {'OK' if mount_ok else 'UNHEALTHY'}")
    print(f"Classified to move: {len(to_move)}")
    print(f"Ambiguous (review): {len(to_review)}")
    print(f"Denied: {len(denied)}")
    print(f"Denylist entries: {len(denylist.get('tvdbIds', []))} ids, {len(denylist.get('titles', []))} titles")
    print(f"Lifetime moved: {state.get('lifetime_moved', 0)}")
    print(f"Last run: {state.get('last_run_at')}")
    if to_move:
        print("\nWould move:")
        for item in to_move[:20]:
            print(f"  - {item.series.get('title')} ({item.reason})")
        if len(to_move) > 20:
            print(f"  ... and {len(to_move) - 20} more")


def print_ambiguous() -> None:
    for entry in load_review():
        print(
            f"{entry.get('title')} tvdb={entry.get('tvdbId')} "
            f"path={entry.get('path')} reason={entry.get('reason')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move miscategorized anime from Sonarr /data/tv to /data/anime."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform moves (default is dry-run)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MOVE_LIMIT,
        help=f"Max series to move per run (default {MOVE_LIMIT})",
    )
    parser.add_argument("--status", action="store_true", help="Show status and exit")
    parser.add_argument(
        "--list-ambiguous", action="store_true", help="Print review list and exit"
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip Shoko import and Jellyfin refresh after moves",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Mount-health check only (exit 0=OK, 1=unhealthy)",
    )
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not DENYLIST_FILE.exists():
        save_json(DENYLIST_FILE, {"tvdbIds": [], "titles": []})

    if args.check:
        return 0 if mounts_ok() else 1

    if args.list_ambiguous:
        print_ambiguous()
        return 0

    api_key = read_sonarr_api_key()

    if args.status:
        print_status(api_key)
        return 0

    denylist = load_denylist()
    state = load_state()
    all_series = sonarr_get(api_key, "/series")
    to_move, to_review, denied = classify_all(all_series, denylist)

    for item in denied:
        log(f"SKIP(denylist) {item.series.get('title')} tvdb={item.series.get('tvdbId')}")

    update_review_file(to_review)
    for item in to_review:
        log(f"REVIEW {item.series.get('title')} tvdb={item.series.get('tvdbId')} {item.reason}")

    report_untracked_orphans(api_key)

    if not to_move:
        log("No anime to move under /data/tv")
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, state)
        return 0

    log(f"Detected {len(to_move)} anime under {TV_ROOT}")
    for item in to_move:
        log(f"PLAN MOVE {item.series.get('title')} ({item.reason})")

    if not args.execute:
        if len(to_move) > args.limit:
            log(f"CAP would defer {len(to_move) - args.limit} series to next run")
        log("DRY RUN — re-run with --execute to move")
        return 0

    if not mounts_ok():
        log("SKIP(mount) mounts unhealthy — doing nothing")
        return 0

    moved, remainder = move_series(api_key, to_move, args.limit)
    if remainder:
        log(f"CAP deferred {len(remainder)} series to next run")

    if moved:
        record_moves(state, moved)
        if not args.no_refresh:
            try:
                shoko_run_import()
            except Exception as e:
                log(f"WARN Shoko RunImport failed: {e}")
            try:
                jellyfin_refresh(jellyfin_token())
            except Exception as e:
                log(f"WARN Jellyfin refresh failed: {e}")

    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    save_json(STATE_FILE, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
