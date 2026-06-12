#!/usr/bin/env python3
"""Rescue stuck Sonarr imports via the Manual Import API.

Handles:
  * obfuscated usenet filenames (Manual Import + queue episodeId fallback)
  * ``Destination already exists`` — file is already in the library under another
    episode number (common with anime absolute numbering / split-cour). Triggers
    RescanSeries, removes the redundant queue item, and cleans the download folder.

Replaces the old root-run ``/opt/sonarr_recovery.py`` which created root-owned
library paths and made native imports fail with EACCES.

Usage:
  sonarr_import_rescue.py             # dry-run
  sonarr_import_rescue.py --execute   # apply fixes
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

SONARR_CONFIG = os.environ.get("SONARR_CONFIG", "/opt/sonarr/config/config.xml")
BASE = os.environ.get("SONARR_URL", "http://127.0.0.1:8989/api/v3")
HOST_PATH_PREFIX = os.environ.get("HOST_PATH_PREFIX", "/mnt/media")
SONARR_PATH_PREFIX = os.environ.get("SONARR_PATH_PREFIX", "/data")

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".ts")
MIN_BYTES = int(os.environ.get("RESCUE_MIN_BYTES", "50000000"))
IMPORT_MODE = os.environ.get("RESCUE_IMPORT_MODE", "move")
POLL_SECONDS = int(os.environ.get("RESCUE_POLL_SECONDS", "60"))
DEST_EXISTS_RE = re.compile(r"Destination (.+?) already exists", re.IGNORECASE)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [import-rescue] {msg}", flush=True)


def api_key() -> str:
    env = os.environ.get("SONARR_API_KEY")
    if env:
        return env
    return ET.parse(SONARR_CONFIG).getroot().findtext("ApiKey") or ""


def headers() -> dict[str, str]:
    return {"X-Api-Key": api_key(), "Content-Type": "application/json"}


def api(method: str, path: str, body=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers(), method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def host_path(sonarr_path: str) -> str:
    if sonarr_path.startswith(SONARR_PATH_PREFIX + "/"):
        return HOST_PATH_PREFIX + sonarr_path[len(SONARR_PATH_PREFIX) :]
    return sonarr_path


def stuck_items() -> list[dict]:
    recs = api("GET", "/queue", params={"pageSize": "200", "includeEpisode": "true"}).get(
        "records", []
    )
    out = []
    for r in recs:
        st = r.get("trackedDownloadState")
        status = r.get("trackedDownloadStatus")
        if st in ("importPending", "importBlocked", "importFailed") or status in (
            "warning",
            "error",
        ):
            out.append(r)
    return out


def queue_item_exists(queue_id: int) -> bool:
    try:
        recs = api("GET", "/queue", params={"pageSize": "200"}).get("records", [])
    except urllib.error.URLError:
        return True
    return any(r.get("id") == queue_id for r in recs)


def error_messages(rec: dict) -> list[str]:
    msgs: list[str] = []
    for block in rec.get("statusMessages") or []:
        for m in block.get("messages") or []:
            msgs.append(m)
    if rec.get("errorMessage"):
        msgs.append(rec["errorMessage"])
    return msgs


def destination_exists_errors(rec: dict) -> list[str]:
    return [m for m in error_messages(rec) if "destination already exists" in m.lower()]


def parse_destinations(rec: dict) -> list[str]:
    paths: list[str] = []
    for msg in destination_exists_errors(rec):
        m = DEST_EXISTS_RE.search(msg)
        if m:
            paths.append(m.group(1).strip())
    return paths


def build_files(rec: dict) -> list[dict]:
    folder = rec.get("outputPath")
    sid = rec.get("seriesId")
    ep_fallback = rec.get("episodeId")
    if not folder:
        return []
    cands = api(
        "GET", "/manualimport", params={"folder": folder, "filterExistingFiles": "false"}
    )
    if not isinstance(cands, list):
        return []
    files = []
    for c in cands:
        p = c.get("path", "")
        if not p.lower().endswith(VIDEO_EXT):
            continue
        size = c.get("size") or 0
        if size and size < MIN_BYTES:
            continue
        eps = [e["id"] for e in (c.get("episodes") or [])]
        if not eps and ep_fallback:
            eps = [ep_fallback]
        if not eps:
            log(f"    no episode mapping for {os.path.basename(p)}")
            continue
        series_id = (c.get("series") or {}).get("id") or sid
        files.append(
            {
                "path": p,
                "folderName": c.get("folderName", ""),
                "seriesId": series_id,
                "episodeIds": eps,
                "quality": rec.get("quality") or c.get("quality"),
                "languages": rec.get("languages") or c.get("languages") or [],
                "releaseGroup": c.get("releaseGroup", ""),
                "downloadId": c.get("downloadId") or rec.get("downloadId"),
            }
        )
    return files


def queue_rescan(series_id: int, execute: bool, rescans: set[int]) -> None:
    if series_id in rescans:
        return
    rescans.add(series_id)
    log(f"    queue RescanSeries seriesId={series_id}")
    if execute:
        api("POST", "/command", body={"name": "RescanSeries", "seriesId": series_id})


def remove_queue_item(queue_id: int, execute: bool) -> bool:
    log(f"    remove queue item id={queue_id}")
    if not execute:
        return True
    params = {
        "remove": "true",
        "blocklist": "false",
        "skipRedownload": "true",
        "changeCategory": "false",
    }
    try:
        api("DELETE", f"/queue/{queue_id}", params=params)
        return not queue_item_exists(queue_id)
    except urllib.error.HTTPError as e:
        log(f"    queue remove failed: {e}")
        return False


def cleanup_download_folder(folder: str, execute: bool) -> None:
    if not folder or not folder.startswith("/mnt/nzbdav/completed-symlinks/"):
        return
    if not os.path.isdir(folder):
        return
    log(f"    remove completed folder {folder}")
    if execute:
        shutil.rmtree(folder, ignore_errors=True)


def handle_destination_exists(
    rec: dict, execute: bool, rescans: set[int]
) -> bool:
    """True when the queue item was handled (or would be in dry-run)."""
    if not destination_exists_errors(rec):
        return False

    title = (rec.get("title") or "")[:60]
    dests = parse_destinations(rec)
    existing = [p for p in dests if os.path.lexists(host_path(p))]

    if dests and not existing:
        log(f"  destination paths from error not on disk — skip: {title}")
        return False

    if existing:
        log(f"  destination exists ({len(existing)} file(s)) — already in library: {title}")
        for p in existing:
            log(f"    on disk: {host_path(p)}")
    else:
        # Sonarr's queue API often omits the path; the import still failed because
        # the target filename is already present (common with anime absolute numbering).
        log(f"  destination already exists — duplicate grab, clearing queue: {title}")

    sid = rec.get("seriesId")
    if sid:
        queue_rescan(sid, execute, rescans)

    qid = rec.get("id")
    removed = remove_queue_item(qid, execute) if qid else False
    cleanup_download_folder(rec.get("outputPath") or "", execute)
    return removed or not execute


def run_manual_import(rec: dict, files: list[dict], execute: bool) -> str:
    if not execute:
        return "dry-run"
    cmd = api(
        "POST",
        "/command",
        body={"name": "ManualImport", "importMode": IMPORT_MODE, "files": files},
    )
    cid = cmd["id"]
    deadline = time.time() + POLL_SECONDS
    status = cmd.get("status")
    while time.time() < deadline:
        time.sleep(3)
        st = api("GET", f"/command/{cid}")
        status = st.get("status")
        if status in ("completed", "failed", "aborted"):
            break
    return status or "unknown"


def run(execute: bool) -> int:
    if not api_key():
        log(f"No Sonarr API key in {SONARR_CONFIG}")
        return 1

    items = stuck_items()
    log(f"{len(items)} stuck queue item(s)")
    if not items:
        return 0

    rescans: set[int] = set()
    handled = 0

    for rec in items:
        title = (rec.get("title") or "")[:60]
        qid = rec.get("id")

        if destination_exists_errors(rec):
            if handle_destination_exists(rec, execute, rescans):
                handled += 1
            continue

        folder = rec.get("outputPath")
        if not folder or not rec.get("seriesId"):
            log(f"  SKIP (no folder/series): {title}")
            continue

        files = build_files(rec)
        if not files:
            log(f"  nothing importable: {title}")
            continue

        log(f"  manual import: {title} -> eps={[f['episodeIds'] for f in files]}")
        if not execute:
            continue

        status = run_manual_import(rec, files, execute=True)
        if qid and not queue_item_exists(qid):
            log(f"    -> {status}; queue cleared")
            handled += 1
            cleanup_download_folder(folder, execute=True)
            continue

        log(f"    -> {status}; still stuck")
        # Re-fetch — manual import may have populated destination-exists errors.
        try:
            refreshed = next(
                (r for r in stuck_items() if r.get("id") == qid),
                rec,
            )
        except urllib.error.URLError:
            refreshed = rec
        if destination_exists_errors(refreshed) and handle_destination_exists(
            refreshed, execute, rescans
        ):
            handled += 1

    log(f"handled {handled}/{len(items)} stuck item(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rescue stuck Sonarr imports (manual import + destination-exists cleanup)."
    )
    ap.add_argument("--execute", action="store_true", help="apply fixes")
    args = ap.parse_args()
    return run(args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
