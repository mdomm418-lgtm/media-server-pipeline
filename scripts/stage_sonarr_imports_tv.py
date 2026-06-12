#!/usr/bin/env python3
"""Stage NzbDAV completed TV downloads to local disk and import via Sonarr.

TV equivalent of stage_sonarr_imports.py (which only handles anime).

Why this exists
---------------
NzbDAV delivers usenet downloads with obfuscated, "un-renamed" filenames -- random
combinations of letters/numbers (e.g. ``1qlEGyruwOJu3n2wwpUKAppHg9o1uD0T.mkv``).
Sonarr cannot parse an episode out of those filenames, so completed-download
handling silently fails and the files rot under a "Season Unknown" / wrong season
(see "The Pitt" Season 2). The *folder* name, however, is the real release name and
carries ``SxxExx`` (e.g. ``The.Pitt.S02E05.1080p.WEB-DL...``). Sonarr can rename
correctly from the folder name, so we stage the folder to local disk with the real
media bytes and let Sonarr import it.

Flow (per completed folder)
---------------------------
1. ``rclone copy`` the completed-symlinks folder (resolving ``.rclonelink`` files to
   their real remote targets) into a local staging dir Sonarr can read.
2. Trigger Sonarr ``DownloadedEpisodesScan`` on the staged folder; Sonarr parses the
   folder name, imports the single media file, and renames it to the library format.
3. Record the folder in a state file so subsequent cron runs skip it.

Usage
-----
    stage_sonarr_imports_tv.py                 # dry-run (default; prints plan)
    stage_sonarr_imports_tv.py --execute       # stage + import
    stage_sonarr_imports_tv.py --execute --limit 5
    stage_sonarr_imports_tv.py --execute --folder "The.Pitt.S02E05..."
    stage_sonarr_imports_tv.py --status

Cron (every 15 min, single instance via flock)::

    */15 * * * * flock -n /home/admin/stage-import-tv.lock \\
      /usr/bin/python3 /home/admin/stage_sonarr_imports_tv.py --execute \\
      >> /home/admin/stage-import-tv.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KEY = os.environ.get("SONARR_API_KEY")
BASE = os.environ.get("SONARR_URL", "http://127.0.0.1:8989/api/v3")
H = {"X-Api-Key": KEY, "Content-Type": "application/json"}

# Host paths (where this script + rclone run).
COMPLETED = os.environ.get("COMPLETED_TV", "/mnt/nzbdav/completed-symlinks/tv")
STAGING = os.environ.get("STAGING_TV", "/mnt/media/downloads/sonarr-tv/staging")
# Path of STAGING as seen *inside* the Sonarr container (/mnt/media -> /data).
CONTAINER_STAGING = os.environ.get("CONTAINER_STAGING_TV", "/data/downloads/sonarr-tv/staging")
# rclone remote + sub-path that maps to COMPLETED.
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE_TV", "nzbdav:completed-symlinks/tv")
NZBDAV_PREFIX = os.environ.get("NZBDAV_PREFIX", "/mnt/nzbdav/")

LOG = os.environ.get("STAGE_IMPORT_TV_LOG", "/home/admin/stage-import-tv.log")
STATE_FILE = os.environ.get("STAGE_IMPORT_TV_STATE", "/home/admin/stage-import-tv-state.json")

MIN_VIDEO_BYTES = 1_000_000
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v")
# Only act on folders that look like a single parseable episode release.
EPISODE_RE = re.compile(r"[Ss]\d{1,3}[Ee]\d{1,3}")
IMPORT_SETTLE_SECONDS = int(os.environ.get("IMPORT_SETTLE_SECONDS", "15"))


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(open(STATE_FILE).read())
    except (OSError, json.JSONDecodeError):
        return {"processed": {}}


def save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write(json.dumps(state, indent=2))
    except OSError as e:
        log(f"WARN could not save state: {e}")


def api_post(path: str, data: dict) -> dict:
    body = json.dumps(data).encode()
    req = urllib.request.Request(BASE + path, data=body, headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def api_get(path: str) -> dict | list:
    req = urllib.request.Request(BASE + path, headers=H, method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def needs_import(name: str) -> tuple[bool, str]:
    """Ask Sonarr to parse the release/folder name and decide if we should import.

    Returns (should_import, reason). We only import when Sonarr matches the folder
    to a known series AND at least one parsed episode is still missing its file --
    i.e. exactly the gap left when normal completed-download handling failed on an
    obfuscated filename. If the episode already has a file, importing again would
    duplicate, so we skip.
    """
    try:
        parsed = api_get(f"/parse?title={urllib.parse.quote(name)}")
    except Exception as e:  # noqa: BLE001
        return False, f"parse-error: {e}"
    series = parsed.get("series") if isinstance(parsed, dict) else None
    if not series:
        return False, "no series match"
    episodes = parsed.get("episodes") or []
    if not episodes:
        return False, "no episodes parsed"
    missing = [e for e in episodes if not e.get("hasFile")]
    if not missing:
        return False, "already has file(s)"
    eps = ", ".join(f"S{e.get('seasonNumber')}E{e.get('episodeNumber')}" for e in missing)
    return True, f"{series.get('title')} missing {eps}"


def has_media(folder: str) -> bool:
    if not os.path.isdir(folder):
        return False
    for fn in os.listdir(folder):
        p = os.path.join(folder, fn)
        if fn.lower().endswith(VIDEO_EXT) and os.path.isfile(p) and os.path.getsize(p) > MIN_VIDEO_BYTES:
            return True
    return False


def stage_folder(name: str, execute: bool) -> bool:
    """Materialize the completed folder's real media into local staging."""
    dest = os.path.join(STAGING, name)
    if has_media(dest):
        log(f"  skip-stage (already staged): {name[:70]}")
        return True
    if not execute:
        log(f"  DRY-RUN would stage: {name[:70]}")
        return True

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    # Pull the folder (rclone represents symlinks as *.rclonelink files).
    src = f"{RCLONE_REMOTE}/{name}"
    subprocess.run(["rclone", "copy", src, dest, "--transfers", "1"], timeout=300, check=False)

    ok = 0
    for fn in os.listdir(dest):
        if not fn.endswith(".rclonelink"):
            continue
        link = os.path.join(dest, fn)
        target = open(link).read().strip().replace(NZBDAV_PREFIX, "nzbdav:")
        out = os.path.join(dest, fn[: -len(".rclonelink")])
        r = subprocess.run(["rclone", "copyto", target, out, "--timeout", "60m"], timeout=3900)
        try:
            os.remove(link)
        except OSError:
            pass
        if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > MIN_VIDEO_BYTES:
            ok += 1
    if ok:
        log(f"  staged {ok} file(s): {name[:70]}")
        return True
    log(f"  FAILED stage: {name[:70]}")
    return False


def import_folder(name: str, execute: bool) -> None:
    path = f"{CONTAINER_STAGING}/{name}"
    if not execute:
        log(f"  DRY-RUN would DownloadedEpisodesScan: {path}")
        return
    api_post("/command", {"name": "DownloadedEpisodesScan", "path": path})
    time.sleep(IMPORT_SETTLE_SECONDS)
    log(f"  import triggered: {name[:70]}")


def iter_candidates() -> list[str]:
    if not os.path.isdir(COMPLETED):
        log(f"completed dir missing: {COMPLETED}")
        return []
    out = []
    for name in sorted(os.listdir(COMPLETED)):
        full = os.path.join(COMPLETED, name)
        if not os.path.isdir(full):
            continue
        if not EPISODE_RE.search(name):
            # No SxxExx in the release/folder name -> Sonarr cannot map it; skip.
            continue
        out.append(name)
    return out


def cmd_status() -> int:
    state = load_state()
    cands = iter_candidates()
    log("=== stage_sonarr_imports_tv status ===")
    log(f"  completed dir: {COMPLETED} (exists={os.path.isdir(COMPLETED)})")
    log(f"  parseable folders: {len(cands)}")
    log(f"  already processed: {len(state.get('processed', {}))}")
    pending = [c for c in cands if c not in state.get("processed", {})]
    log(f"  pending (not yet seen): {len(pending)}")
    actionable = []
    for c in pending:
        should, reason = needs_import(c)
        if should:
            actionable.append((c, reason))
    log(f"  actionable (series matched + episode missing file): {len(actionable)}")
    for c, reason in actionable[:30]:
        log(f"    - {c[:70]}  [{reason}]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage + import NzbDAV completed TV downloads via Sonarr")
    ap.add_argument("--execute", action="store_true", help="Actually stage and import (default: dry-run)")
    ap.add_argument("--status", action="store_true", help="Show pending folders and exit")
    ap.add_argument("--limit", type=int, default=0, help="Max folders to process this run (0 = all)")
    ap.add_argument("--folder", help="Process only this exact folder name")
    ap.add_argument("--reprocess", action="store_true", help="Ignore processed-state and redo folders")
    args = ap.parse_args()

    if args.status:
        return cmd_status()

    state = load_state()
    processed = state.setdefault("processed", {})

    candidates = iter_candidates()
    if args.folder:
        candidates = [c for c in candidates if c == args.folder]
        if not candidates:
            log(f"folder not found under {COMPLETED}: {args.folder}")
            return 1
    if not args.reprocess:
        candidates = [c for c in candidates if c not in processed]
    if args.limit:
        candidates = candidates[: args.limit]

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    log(f"start [{mode}] {len(candidates)} folder(s) to process")

    done = 0
    skipped = 0
    for name in candidates:
        should, reason = needs_import(name)
        if not should:
            skipped += 1
            # Folders Sonarr already satisfied are recorded so we don't re-check forever.
            if args.execute and reason == "already has file(s)":
                processed[name] = f"skip:{reason}"
                save_state(state)
            log(f"skip [{reason}]: {name[:70]}")
            continue
        log(f"folder: {name[:80]}  [{reason}]")
        try:
            if stage_folder(name, args.execute):
                import_folder(name, args.execute)
                if args.execute:
                    processed[name] = datetime.now(timezone.utc).isoformat()
                    save_state(state)
                done += 1
        except Exception as e:  # noqa: BLE001 - keep cron resilient
            log(f"  ERROR {name[:50]}: {e}")

    log(f"done ({done} imported, {skipped} skipped, of {len(candidates)} candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
