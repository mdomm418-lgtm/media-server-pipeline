#!/usr/bin/env python3
"""
Detect broken NzbDAV library symlinks and queue Sonarr/Radarr re-grabs.

Walks /mnt/media/{anime,tv,movies}, tests each symlink into /mnt/nzbdav/.ids/...,
matches failures to Sonarr episodefiles / Radarr moviefiles, and optionally:
  - removes the dead symlink
  - drops the stale *arr file record (deleteFiles=false)
  - queues EpisodeSearch / MoviesSearch

Usage:
  library_stream_health.py --dry-run
  library_stream_health.py --execute
  library_stream_health.py --execute --root movies --movie "The Running Man"
  library_stream_health.py --dry-run --report /home/admin/dedup-reports/stream-health.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import requests

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989/api/v3")
SONARR_KEY = os.environ.get("SONARR_API_KEY")
RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878/api/v3")
RADARR_KEY = os.environ.get("RADARR_API_KEY")
NZBDAV_DB = os.environ.get("NZBDAV_DB", "/opt/nzbdav/config/db.sqlite")
JELLYFIN_DB = os.environ.get("JELLYFIN_DB", "/opt/jellyfin/config/data/data/jellyfin.db")

SONARR_HEADERS = {"X-Api-Key": SONARR_KEY}
RADARR_HEADERS = {"X-Api-Key": RADARR_KEY}
TIMEOUT = 45

HOST_PREFIX = "/mnt/media"
CONTAINER_PREFIX = "/data"
NZBDAV_IDS_PREFIX = "/mnt/nzbdav/.ids/"
NZBDAV_MOUNT = "/mnt/nzbdav"

LIBRARY_ROOTS = [
    {"path": f"{HOST_PREFIX}/anime", "label": "anime", "arr": "sonarr"},
    {"path": f"{HOST_PREFIX}/tv", "label": "tv", "arr": "sonarr"},
    {"path": f"{HOST_PREFIX}/movies", "label": "movies", "arr": "radarr"},
]

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v"}
EXCLUDE_DIR_NAMES = {"downloads", "decypharr", "incomplete", "lost+found"}
EXTRA_RE = re.compile(
    r"NCED|NCOP|NC\.?ED|NC\.?OP|Creditless|Textless|"
    r"Clean\s*(Opening|Ending)|PV\d|Trailer|CM\d|Menu|Bonus|sample",
    re.IGNORECASE,
)
DAV_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)
EPISODE_RE = re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE)
REPORT_DIR = "/home/admin/dedup-reports"
READ_TIMEOUT_SEC = 4


@dataclass
class ArrFileRef:
    arr: Literal["sonarr", "radarr"]
    file_id: int
    media_id: int  # seriesId or movieId
    title: str
    relative_path: str
    season: Optional[int] = None
    episode: Optional[int] = None


@dataclass
class BrokenStream:
    path: str
    root: str
    symlink_target: Optional[str]
    dav_id: Optional[str]
    reason: str  # missing_target | unreadable | nzbdav_unhealthy
    arr: Optional[ArrFileRef] = None
    nzbdav_message: Optional[str] = None
    actions: list[str] = field(default_factory=list)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [stream-health] {msg}", flush=True)


def container_to_host(path: str) -> str:
    if path.startswith(CONTAINER_PREFIX + "/"):
        return HOST_PREFIX + path[len(CONTAINER_PREFIX) :]
    return path


def host_to_container(path: str) -> str:
    if path.startswith(HOST_PREFIX + "/"):
        return CONTAINER_PREFIX + path[len(HOST_PREFIX) :]
    return path


def mount_ok() -> bool:
    if not os.path.ismount(NZBDAV_MOUNT):
        log(f"SKIP: {NZBDAV_MOUNT} is not mounted")
        return False
    try:
        os.listdir(NZBDAV_MOUNT)
    except OSError as e:
        log(f"SKIP: cannot list {NZBDAV_MOUNT}: {e}")
        return False
    return True


def extract_dav_id(target: str) -> Optional[str]:
    m = DAV_ID_RE.search(target)
    return m.group(1).lower() if m else None


def test_readable(path: str) -> tuple[bool, str]:
    """Return (ok, reason). Uses a short read; does not wait on D-state FUSE."""
    if not os.path.lexists(path):
        return False, "missing_target"
    if not os.path.exists(path):
        return False, "missing_target"
    try:
        proc = subprocess.Popen(
            ["head", "-c", "4096", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        start = time.time()
        while time.time() - start < READ_TIMEOUT_SEC:
            if proc.poll() is not None:
                out, _ = proc.communicate()
                if proc.returncode == 0 and len(out) > 0:
                    return True, "ok"
                return False, "unreadable"
            time.sleep(0.1)
        try:
            proc.kill()
        except OSError:
            pass
        return False, "read_timeout"
    except OSError as e:
        return False, f"os_error:{e}"


def load_nzbdav_unhealthy() -> dict[str, str]:
    """Map lowercase dav UUID -> health message (if DB reachable)."""
    out: dict[str, str] = {}
    if not os.path.isfile(NZBDAV_DB):
        return out
    try:
        conn = sqlite3.connect(NZBDAV_DB)
        rows = conn.execute(
            """
            SELECT DavItemId, Message FROM HealthCheckResults
            WHERE Result != 0 AND DavItemId IS NOT NULL
            """
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        log(f"NzbDAV DB read failed: {e}")
        return out
    for dav_item_id, message in rows:
        if dav_item_id:
            out[str(dav_item_id).lower()] = (message or "")[:200]
    return out


def check_docker_logs() -> set[str]:
    """Parse docker logs to find files that threw errors during actual playback."""
    broken_paths = set()
    try:
        proc = subprocess.run(["docker", "logs", "--since", "24h", "nzbdav"], capture_output=True, text=True)
        for line in proc.stderr.splitlines() + proc.stdout.splitlines():
            if "has missing articles" in line or "Timeout reading from NNTP stream" in line or "Response Content-Length mismatch" in line:
                m = re.search(r"File `([^`]+)`", line)
                if m:
                    broken_paths.add(container_to_host(m.group(1)))
    except Exception as e:
        log(f"Failed to check docker logs: {e}")
    return broken_paths


def build_sonarr_index() -> dict[str, ArrFileRef]:
    index: dict[str, ArrFileRef] = {}
    try:
        series_list = requests.get(
            f"{SONARR_URL}/series", headers=SONARR_HEADERS, timeout=TIMEOUT
        ).json()
    except Exception as e:
        log(f"Sonarr series list failed: {e}")
        return index

    for series in series_list:
        sid = series["id"]
        title = series.get("title") or ""
        try:
            efiles = requests.get(
                f"{SONARR_URL}/episodefile",
                params={"seriesId": sid},
                headers=SONARR_HEADERS,
                timeout=TIMEOUT,
            ).json()
        except Exception as e:
            log(f"episodefile list failed for {title}: {e}")
            continue
        for ef in efiles:
            host_path = container_to_host(ef.get("path") or "")
            if not host_path:
                continue
            season = ef.get("seasonNumber")
            ep_nums = ef.get("episodeNumbers") or []
            episode = ep_nums[0] if ep_nums else None
            ref = ArrFileRef(
                arr="sonarr",
                file_id=ef["id"],
                media_id=sid,
                title=title,
                relative_path=ef.get("relativePath") or os.path.basename(host_path),
                season=season,
                episode=episode,
            )
            index[host_path] = ref
            index[os.path.normpath(host_path)] = ref
    return index


def build_radarr_index() -> dict[str, ArrFileRef]:
    index: dict[str, ArrFileRef] = {}
    try:
        movies = requests.get(
            f"{RADARR_URL}/movie", headers=RADARR_HEADERS, timeout=TIMEOUT
        ).json()
    except Exception as e:
        log(f"Radarr movie list failed: {e}")
        return index

    for movie in movies:
        if not movie.get("hasFile"):
            continue
        mf = movie.get("movieFile")
        if not mf:
            continue
        host_path = container_to_host(mf.get("path") or "")
        if not host_path:
            continue
        ref = ArrFileRef(
            arr="radarr",
            file_id=mf["id"],
            media_id=movie["id"],
            title=movie.get("title") or "",
            relative_path=mf.get("relativePath") or os.path.basename(host_path),
        )
        index[host_path] = ref
        index[os.path.normpath(host_path)] = ref
    return index


def should_skip_dir(dirname: str) -> bool:
    return dirname.lower() in EXCLUDE_DIR_NAMES or dirname.startswith(".")


def scan_root(
    root_cfg: dict,
    sonarr_index: dict[str, ArrFileRef],
    radarr_index: dict[str, ArrFileRef],
    nzbdav_bad: dict[str, str],
    min_age_sec: float,
    title_filters: Optional[list[str]],
) -> list[BrokenStream]:
    root_path = root_cfg["path"]
    arr_type = root_cfg["arr"]
    docker_playback_errors = check_docker_logs()
    issues: list[BrokenStream] = []
    if not os.path.isdir(root_path):
        return issues

    now = time.time()
    arr_index = sonarr_index if arr_type == "sonarr" else radarr_index

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            if EXTRA_RE.search(fname):
                continue
            full_path = os.path.normpath(os.path.join(dirpath, fname))
            if not os.path.islink(full_path):
                continue
            try:
                target = os.readlink(full_path)
            except OSError:
                continue
            if NZBDAV_IDS_PREFIX not in target and "/.ids/" not in target:
                continue

            if min_age_sec > 0:
                try:
                    if now - os.lstat(full_path).st_mtime < min_age_sec:
                        continue
                except OSError:
                    pass

            dav_id = extract_dav_id(target)
            
            # 1) FUSE test
            ok, reason = test_readable(full_path)

            # 2) DB health test
            nzbdav_msg = None
            if ok and dav_id and dav_id in nzbdav_bad:
                ok = False
                reason = "nzbdav_unhealthy"
                nzbdav_msg = nzbdav_bad[dav_id]

            # 3) Docker playback error test
            if ok and full_path in docker_playback_errors:
                ok = False
                reason = "playback_error_logged"
                nzbdav_msg = "Missing articles or timeouts detected during playback"

            if ok:
                continue

            ref = arr_index.get(full_path) or arr_index.get(os.path.normpath(full_path))
            if title_filters and ref:
                if not any(f.lower() in ref.title.lower() for f in title_filters):
                    continue
            if title_filters and not ref:
                # Match folder name for untracked broken symlinks
                if not any(f.lower() in full.lower() for f in title_filters):
                    continue

            issues.append(
                BrokenStream(
                    path=full,
                    root=root_cfg["label"],
                    symlink_target=target,
                    dav_id=dav_id,
                    reason=fail_reason,
                    arr=ref,
                    nzbdav_message=nzbdav_msg or None,
                )
            )
    return issues


def remove_symlink(path: str, execute: bool) -> None:
    if not os.path.lexists(path):
        log(f"  symlink already absent: {path}")
        return
    if execute:
        try:
            os.unlink(path)
        except OSError as e:
            log(f"  unlink failed: {e}")
            return
    log(f"  {'removed' if execute else 'would remove'} symlink {path}")


def drop_sonarr_episodefile(file_id: int, execute: bool) -> bool:
    if not execute:
        return True
    try:
        r = requests.delete(
            f"{SONARR_URL}/episodefile/{file_id}",
            headers=SONARR_HEADERS,
            params={"deleteFiles": "false"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"  Sonarr episodefile delete failed id={file_id}: {e}")
        return False


def drop_radarr_moviefile(file_id: int, execute: bool) -> bool:
    if not execute:
        return True
    try:
        r = requests.delete(
            f"{RADARR_URL}/moviefile/{file_id}",
            headers=RADARR_HEADERS,
            params={"deleteFiles": "false"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"  Radarr moviefile delete failed id={file_id}: {e}")
        return False


def queue_episode_search(series_id: int, season: int, episode: int, execute: bool) -> bool:
    if not execute:
        return True
    try:
        eps = requests.get(
            f"{SONARR_URL}/episode",
            params={"seriesId": series_id},
            headers=SONARR_HEADERS,
            timeout=TIMEOUT,
        ).json()
        ep = next(
            (
                e
                for e in eps
                if e.get("seasonNumber") == season and e.get("episodeNumber") == episode
            ),
            None,
        )
        if not ep:
            log(f"  episode not found for SeriesSearch fallback series={series_id}")
            requests.post(
                f"{SONARR_URL}/command",
                headers=SONARR_HEADERS,
                json={"name": "SeriesSearch", "seriesId": series_id},
                timeout=TIMEOUT,
            ).raise_for_status()
            return True
        requests.post(
            f"{SONARR_URL}/command",
            headers=SONARR_HEADERS,
            json={"name": "EpisodeSearch", "episodeIds": [ep["id"]]},
            timeout=TIMEOUT,
        ).raise_for_status()
        return True
    except Exception as e:
        log(f"  EpisodeSearch failed: {e}")
        return False


def queue_movies_search(movie_id: int, execute: bool) -> bool:
    if not execute:
        return True
    try:
        requests.post(
            f"{RADARR_URL}/command",
            headers=RADARR_HEADERS,
            json={"name": "MoviesSearch", "movieIds": [movie_id]},
            timeout=TIMEOUT,
        ).raise_for_status()
        return True
    except Exception as e:
        log(f"  MoviesSearch failed: {e}")
        return False


def apply_fixes(issues: list[BrokenStream], execute: bool, search: bool) -> None:
    sonarr_rescan: set[int] = set()
    for issue in issues:
        log(f"FIX {issue.root}: {issue.path}")
        log(f"  reason={issue.reason} dav_id={issue.dav_id or '?'}")
        if issue.nzbdav_message:
            log(f"  nzbdav: {issue.nzbdav_message[:100]}")
        if issue.arr:
            log(f"  arr: {issue.arr.arr} {issue.arr.title} file_id={issue.arr.file_id}")
        else:
            log("  arr: no matching episodefile/moviefile")

        if issue.arr:
            if issue.arr.arr == "sonarr":
                if drop_sonarr_episodefile(issue.arr.file_id, execute):
                    issue.actions.append("drop_episodefile")
            else:
                if drop_radarr_moviefile(issue.arr.file_id, execute):
                    issue.actions.append("drop_moviefile")

        remove_symlink(issue.path, execute)
        if execute:
            issue.actions.append("unlink_symlink")

        if search and issue.arr:
            if issue.arr.arr == "sonarr":
                if issue.arr.season is not None and issue.arr.episode is not None:
                    if queue_episode_search(
                        issue.arr.media_id,
                        issue.arr.season,
                        issue.arr.episode,
                        execute,
                    ):
                        issue.actions.append("EpisodeSearch")
                        sonarr_rescan.add(issue.arr.media_id)
                else:
                    if execute:
                        try:
                            requests.post(
                                f"{SONARR_URL}/command",
                                headers=SONARR_HEADERS,
                                json={
                                    "name": "SeriesSearch",
                                    "seriesId": issue.arr.media_id,
                                },
                                timeout=TIMEOUT,
                            ).raise_for_status()
                            issue.actions.append("SeriesSearch")
                            sonarr_rescan.add(issue.arr.media_id)
                        except Exception as e:
                            log(f"  SeriesSearch failed: {e}")
            else:
                if queue_movies_search(issue.arr.media_id, execute):
                    issue.actions.append("MoviesSearch")

    if execute and sonarr_rescan:
        for sid in sorted(sonarr_rescan):
            try:
                requests.post(
                    f"{SONARR_URL}/command",
                    headers=SONARR_HEADERS,
                    json={"name": "RescanSeries", "seriesId": sid},
                    timeout=TIMEOUT,
                ).raise_for_status()
                log(f"Queued RescanSeries seriesId={sid}")
            except Exception as e:
                log(f"RescanSeries failed {sid}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find broken NzbDAV symlinks and queue *arr re-grabs"
    )
    parser.add_argument("--execute", action="store_true", help="Apply fixes")
    parser.add_argument("--dry-run", action="store_true", help="Report only (default)")
    parser.add_argument(
        "--root",
        choices=["anime", "tv", "movies", "all"],
        default="all",
    )
    parser.add_argument("--series", action="append", help="Limit to series title (Sonarr)")
    parser.add_argument("--movie", action="append", help="Limit to movie title (Radarr)")
    parser.add_argument(
        "--no-search",
        action="store_true",
        help="Drop file records and symlinks only; do not queue searches",
    )
    parser.add_argument(
        "--min-age-hours",
        type=float,
        default=1.0,
        help="Skip symlinks newer than N hours",
    )
    parser.add_argument("--report", help="JSON report path")
    args = parser.parse_args()

    execute = args.execute and not args.dry_run
    if not execute:
        log("DRY RUN — pass --execute to apply fixes")

    if not mount_ok():
        return 2

    title_filters: list[str] = []
    if args.series:
        title_filters.extend(args.series)
    if args.movie:
        title_filters.extend(args.movie)

    roots = LIBRARY_ROOTS
    if args.root != "all":
        roots = [r for r in LIBRARY_ROOTS if r["label"] == args.root]

    log("Loading NzbDAV unhealthy index...")
    nzbdav_bad = load_nzbdav_unhealthy()
    log(f"  {len(nzbdav_bad)} unhealthy stream IDs in NzbDAV DB")

    log("Building Sonarr/Radarr path index...")
    sonarr_index = build_sonarr_index()
    radarr_index = build_radarr_index()
    log(f"  Sonarr files: {len(sonarr_index)}  Radarr files: {len(radarr_index)}")

    min_age = args.min_age_hours * 3600
    all_issues: list[BrokenStream] = []
    for root_cfg in roots:
        log(f"Scanning {root_cfg['path']}...")
        found = scan_root(
            root_cfg,
            sonarr_index,
            radarr_index,
            nzbdav_bad,
            min_age,
            title_filters or None,
        )
        log(f"  {len(found)} broken")
        all_issues.extend(found)

    log("─── Summary ───")
    by_reason: dict[str, int] = {}
    unmapped = 0
    for issue in all_issues:
        by_reason[issue.reason] = by_reason.get(issue.reason, 0) + 1
        if not issue.arr:
            unmapped += 1
    log(f"  total broken: {len(all_issues)}")
    for reason, n in sorted(by_reason.items()):
        log(f"    {reason}: {n}")
    log(f"  without *arr match: {unmapped}")

    for issue in all_issues[:40]:
        title = issue.arr.title if issue.arr else os.path.basename(os.path.dirname(issue.path))
        log(f"  [{issue.root}] {title}: {issue.reason} — {os.path.basename(issue.path)}")
    if len(all_issues) > 40:
        log(f"  ... and {len(all_issues) - 40} more")

    if all_issues:
        apply_fixes(all_issues, execute, search=not args.no_search)

    report_path = args.report or os.path.join(
        REPORT_DIR,
        f"stream-health-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json",
    )
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "execute": execute,
        "counts": {"total": len(all_issues), "unmapped": unmapped, **by_reason},
        "issues": [asdict(i) for i in all_issues],
    }
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    log(f"Report: {report_path}")

    return 1 if all_issues else 0


if __name__ == "__main__":
    sys.exit(main())
