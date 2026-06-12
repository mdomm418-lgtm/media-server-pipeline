#!/usr/bin/env python3
"""
Slowly search Sonarr/Radarr for missing episodes and movies without overloading
indexers, download clients, or NzbDAV.

Uses SeriesSearch (per show) instead of global MissingEpisodeSearch, and small
MoviesSearch batches for Radarr. Intended for cron (e.g. every 30 minutes).

Only targets standard Sonarr (8989) and Radarr (7878). sonarr-anime / radarr-anime
are deprecated and intentionally excluded.

Usage:
  backlog_fetcher.py --dry-run
  backlog_fetcher.py --execute
  backlog_fetcher.py --status
  backlog_fetcher.py --fix-listsync --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/opt/sonarr/config/config.xml")
RADARR_CONFIG = os.environ.get("RADARR_CONFIG", "/opt/radarr/config/config.xml")
SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989/api/v3")
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878/api/v3")
LISTSYNC_DB = os.environ.get("LISTSYNC_DB", "/opt/listsync/data/list_sync.db")
STATE_FILE = os.environ.get(
    "BACKLOG_STATE_FILE", "/home/admin/backlog-fetcher/state.json"
)
TIMEOUT = 60
CACHE_TTL_SECONDS = int(os.environ.get("BACKLOG_CACHE_TTL", 6 * 3600))


@dataclass
class GuardConfig:
    max_queue_items: int = 5
    max_active_commands: int = 2
    min_seconds_since_last_search: int = 120


@dataclass
class BatchConfig:
    series_batch: int = 2
    movie_batch: int = 3
    delay_seconds: int = 30


@dataclass
class RunStats:
    movies_searched: int = 0
    series_searched: int = 0
    skipped_reason: str | None = None


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def read_api_key(config_path: str, env_var: str) -> str:
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    root = ET.parse(config_path).getroot()
    key = root.findtext("ApiKey")
    if not key:
        raise SystemExit(f"No ApiKey in {config_path} and {env_var} unset")
    return key


def sonarr_headers() -> dict[str, str]:
    return {"X-Api-Key": read_api_key(SONARR_CONFIG, "SONARR_API_KEY")}


def radarr_headers() -> dict[str, str]:
    return {"X-Api-Key": read_api_key(RADARR_CONFIG, "RADARR_API_KEY")}


def default_state() -> dict[str, Any]:
    return {
        "series_cursor": 0,
        "movie_cursor": 0,
        "last_search_at": None,
        "last_run_at": None,
        "totals": {"movies_searched": 0, "series_searched": 0, "runs": 0},
    }


def load_state() -> dict[str, Any]:
    path = Path(STATE_FILE)
    if not path.exists():
        return default_state()
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"Warning: could not read state ({e}); starting fresh")
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def arr_online(url: str, headers: dict[str, str]) -> bool:
    try:
        r = requests.get(f"{url}/system/status", headers=headers, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False


def queue_count(url: str, headers: dict[str, str]) -> int:
    """Count active downloads only — not importBlocked completed items."""
    r = requests.get(
        f"{url}/queue", headers=headers, params={"pageSize": 200}, timeout=TIMEOUT
    )
    r.raise_for_status()
    records = r.json().get("records") or []
    inactive = frozenset({"importBlocked", "importFailed"})
    return sum(
        1
        for rec in records
        if rec.get("trackedDownloadState") not in inactive
    )


def active_search_commands(url: str, headers: dict[str, str]) -> int:
    r = requests.get(f"{url}/command", headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    search_names = {
        "SeriesSearch",
        "EpisodeSearch",
        "MissingEpisodeSearch",
        "MoviesSearch",
        "MissingMoviesSearch",
        "RssSync",
    }
    return sum(
        1
        for c in r.json()
        if c.get("name") in search_names and c.get("status") in ("queued", "started")
    )


def check_guards(
    guards: GuardConfig, state: dict[str, Any]
) -> str | None:
    if not arr_online(SONARR_URL, sonarr_headers()):
        return "Sonarr offline"
    if not arr_online(RADARR_URL, radarr_headers()):
        return "Radarr offline"

    total_queue = queue_count(SONARR_URL, sonarr_headers()) + queue_count(
        RADARR_URL, radarr_headers()
    )
    if total_queue >= guards.max_queue_items:
        return f"queue depth {total_queue} >= {guards.max_queue_items}"

    active = active_search_commands(SONARR_URL, sonarr_headers()) + active_search_commands(
        RADARR_URL, radarr_headers()
    )
    if active >= guards.max_active_commands:
        return f"active search/rss commands {active} >= {guards.max_active_commands}"

    last = state.get("last_search_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if elapsed < guards.min_seconds_since_last_search:
                return (
                    f"cooldown {int(elapsed)}s < {guards.min_seconds_since_last_search}s"
                )
        except ValueError:
            pass
    return None


def paginate_wanted_missing(
    url: str, headers: dict[str, str], *, max_pages: int | None = None
) -> list[dict[str, Any]]:
    page = 1
    records: list[dict[str, Any]] = []
    total = None
    while True:
        r = requests.get(
            f"{url}/wanted/missing",
            headers=headers,
            params={
                "page": page,
                "pageSize": 100,
                "sortKey": "date",
                "sortDirection": "ascending",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if total is None:
            total = int(data.get("totalRecords") or 0)
        batch = data.get("records") or []
        records.extend(batch)
        if page * 100 >= total:
            break
        if max_pages is not None and page >= max_pages:
            break
        page += 1
    return records


def wanted_missing_total(url: str, headers: dict[str, str]) -> int:
    r = requests.get(
        f"{url}/wanted/missing",
        headers=headers,
        params={"pageSize": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return int(r.json().get("totalRecords") or 0)


def cache_is_fresh(state: dict[str, Any]) -> bool:
    updated = state.get("cache_updated_at")
    if not updated:
        return False
    try:
        dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() < CACHE_TTL_SECONDS
    except ValueError:
        return False


def build_movie_cache() -> list[list[Any]]:
    records = paginate_wanted_missing(RADARR_URL, radarr_headers())
    out: list[list[Any]] = []
    seen: set[int] = set()
    for rec in records:
        mid = rec.get("id")
        if mid in seen:
            continue
        seen.add(mid)
        title = rec.get("title") or "?"
        year = rec.get("year") or "?"
        out.append([mid, f"{title} ({year})"])
    return out


def build_series_cache() -> list[list[Any]]:
    records = paginate_wanted_missing(SONARR_URL, sonarr_headers())
    counts: dict[int, int] = {}
    for rec in records:
        sid = rec.get("seriesId")
        if sid is None:
            continue
        counts[sid] = counts.get(sid, 0) + 1

    titles: dict[int, str] = {}
    if counts:
        r = requests.get(f"{SONARR_URL}/series", headers=sonarr_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        for s in r.json():
            if s["id"] in counts:
                titles[s["id"]] = s.get("title") or f"series-{s['id']}"

    ranked = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    return [
        [sid, titles.get(sid, f"series-{sid}"), counts[sid]] for sid, _ in ranked
    ]


def ensure_cache(state: dict[str, Any], force: bool = False) -> dict[str, Any]:
    if not force and cache_is_fresh(state):
        return state
    log("Refreshing backlog cache (this may take a minute)...")
    state["cached_movies"] = build_movie_cache()
    state["cached_series"] = build_series_cache()
    state["cache_updated_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log(
        f"Cache updated: {len(state['cached_movies'])} movies, "
        f"{len(state['cached_series'])} series"
    )
    return state


def cached_movies(state: dict[str, Any]) -> list[tuple[int, str]]:
    return [(m[0], m[1]) for m in state.get("cached_movies") or []]


def cached_series(state: dict[str, Any]) -> list[tuple[int, str, int]]:
    return [(s[0], s[1], s[2]) for s in state.get("cached_series") or []]


def rotate_batch[T](items: list[T], cursor: int, batch_size: int) -> tuple[list[T], int]:
    if not items:
        return [], 0
    n = len(items)
    cursor = cursor % n
    if cursor + batch_size <= n:
        batch = items[cursor : cursor + batch_size]
        next_cursor = (cursor + batch_size) % n
    else:
        batch = items[cursor:] + items[: batch_size - (n - cursor)]
        next_cursor = (cursor + batch_size) % n
    return batch, next_cursor


def queue_movies_search(movie_ids: list[int], execute: bool) -> bool:
    if not movie_ids:
        return False
    payload = {"name": "MoviesSearch", "movieIds": movie_ids}
    if not execute:
        return True
    r = requests.post(
        f"{RADARR_URL}/command", headers=radarr_headers(), json=payload, timeout=TIMEOUT
    )
    r.raise_for_status()
    return True


def queue_series_search(series_id: int, execute: bool) -> bool:
    payload = {"name": "SeriesSearch", "seriesId": series_id}
    if not execute:
        return True
    r = requests.post(
        f"{SONARR_URL}/command", headers=sonarr_headers(), json=payload, timeout=TIMEOUT
    )
    r.raise_for_status()
    return True


def fix_listsync_stuck(execute: bool) -> None:
    db_path = Path(LISTSYNC_DB)
    if not db_path.exists():
        log(f"ListSync DB not found: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, session_id, status, in_progress, start_time FROM sync_history "
        "WHERE in_progress=1 ORDER BY id DESC LIMIT 5"
    )
    stuck = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM synced_items")
    synced_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM item_lists")
    item_lists_count = cur.fetchone()[0]

    log(f"ListSync synced_items={synced_count} item_lists={item_lists_count}")
    if not stuck:
        log("No stuck ListSync sync rows found")
        conn.close()
        return

    for row in stuck:
        log(f"Stuck sync: id={row[0]} session={row[1]} since={row[4]}")
        if execute:
            cur.execute(
                "UPDATE sync_history SET in_progress=0, status='failed', "
                "end_time=CURRENT_TIMESTAMP, error_message='cleared by backlog_fetcher' "
                "WHERE id=?",
                (row[0],),
            )
            log(f"  cleared sync_history id={row[0]}")
    if execute:
        conn.commit()
        log("ListSync unstuck — trigger a manual sync from the ListSync dashboard")
    else:
        log("DRY RUN — would clear stuck sync_history rows")
    conn.close()


def print_status(state: dict[str, Any]) -> None:
    log("=== Backlog status ===")
    if arr_online(SONARR_URL, sonarr_headers()):
        ep_total = wanted_missing_total(SONARR_URL, sonarr_headers())
        series_n = len(state.get("cached_series") or [])
        if not cache_is_fresh(state):
            series_n_str = f"{series_n} (cache stale — run with --refresh-cache)"
        else:
            series_n_str = str(series_n)
        log(f"Sonarr: {ep_total} missing episodes across {series_n_str} series")
        log(f"  queue={queue_count(SONARR_URL, sonarr_headers())}")
    else:
        log("Sonarr: offline")

    if arr_online(RADARR_URL, radarr_headers()):
        movie_n = wanted_missing_total(RADARR_URL, radarr_headers())
        log(f"Radarr: {movie_n} missing movies")
        log(f"  queue={queue_count(RADARR_URL, radarr_headers())}")
    else:
        log("Radarr: offline")

    log(f"State file: {STATE_FILE}")
    log(f"  cache_updated={state.get('cache_updated_at')}")
    log(f"  series_cursor={state.get('series_cursor')} movie_cursor={state.get('movie_cursor')}")
    log(f"  last_run={state.get('last_run_at')} last_search={state.get('last_search_at')}")
    totals = state.get("totals") or {}
    log(
        f"  lifetime: runs={totals.get('runs', 0)} "
        f"series_searched={totals.get('series_searched', 0)} "
        f"movies_searched={totals.get('movies_searched', 0)}"
    )


def run_backlog(
    execute: bool, guards: GuardConfig, batch: BatchConfig, refresh_cache: bool
) -> RunStats:
    stats = RunStats()
    state = load_state()
    state = ensure_cache(state, force=refresh_cache)

    skip = check_guards(guards, state)
    if skip:
        stats.skipped_reason = skip
        log(f"Skipping run: {skip}")
        return stats

    movies = cached_movies(state)
    series = cached_series(state)
    movie_batch, next_movie_cursor = rotate_batch(
        movies, state.get("movie_cursor", 0), batch.movie_batch
    )
    series_batch, next_series_cursor = rotate_batch(
        series, state.get("series_cursor", 0), batch.series_batch
    )

    log(
        f"Backlog: {len(movies)} movies, {len(series)} series with gaps "
        f"(batch movies={len(movie_batch)} series={len(series_batch)})"
    )

    if movie_batch:
        ids = [mid for mid, _ in movie_batch]
        titles = ", ".join(t for _, t in movie_batch)
        log(f"Radarr MoviesSearch ids={ids}: {titles}")
        if queue_movies_search(ids, execute):
            stats.movies_searched = len(ids)

    for sid, title, missing_count in series_batch:
        log(f"Sonarr SeriesSearch id={sid} ({title}, {missing_count} missing eps)")
        if queue_series_search(sid, execute):
            stats.series_searched += 1
        if execute and batch.delay_seconds > 0:
            time.sleep(batch.delay_seconds)

    if not execute:
        log("DRY RUN — no searches queued")
        return stats

    now = datetime.now(timezone.utc).isoformat()
    state["movie_cursor"] = next_movie_cursor
    state["series_cursor"] = next_series_cursor
    state["last_run_at"] = now
    if stats.movies_searched or stats.series_searched:
        state["last_search_at"] = now
    totals = state.setdefault("totals", {"movies_searched": 0, "series_searched": 0, "runs": 0})
    totals["runs"] = totals.get("runs", 0) + 1
    totals["movies_searched"] = totals.get("movies_searched", 0) + stats.movies_searched
    totals["series_searched"] = totals.get("series_searched", 0) + stats.series_searched
    save_state(state)
    log(
        f"Done: searched {stats.movies_searched} movies, "
        f"{stats.series_searched} series"
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Throttled Sonarr/Radarr backlog search (standard instances only)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Queue searches (default is dry-run preview only)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print backlog counts and state, then exit",
    )
    parser.add_argument(
        "--fix-listsync",
        action="store_true",
        help="Clear stuck ListSync sync_history row",
    )
    parser.add_argument("--series-batch", type=int, default=2)
    parser.add_argument("--movie-batch", type=int, default=3)
    parser.add_argument("--delay-seconds", type=int, default=30)
    parser.add_argument("--max-queue", type=int, default=5)
    parser.add_argument("--max-active-commands", type=int, default=2)
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Rebuild missing-item cache even if still fresh",
    )
    parser.add_argument("--cooldown-seconds", type=int, default=120)
    args = parser.parse_args()

    execute = args.execute
    guards = GuardConfig(
        max_queue_items=args.max_queue,
        max_active_commands=args.max_active_commands,
        min_seconds_since_last_search=args.cooldown_seconds,
    )
    batch = BatchConfig(
        series_batch=max(0, args.series_batch),
        movie_batch=max(0, args.movie_batch),
        delay_seconds=max(0, args.delay_seconds),
    )

    if args.status:
        print_status(load_state())
        return 0

    if args.fix_listsync:
        fix_listsync_stuck(execute)
        if not execute:
            log("DRY RUN — pass --execute to apply ListSync fix")

    run_backlog(execute, guards, batch, refresh_cache=args.refresh_cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
