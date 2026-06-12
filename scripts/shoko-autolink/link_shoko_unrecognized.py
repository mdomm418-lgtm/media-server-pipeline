#!/usr/bin/env python3
"""Auto-link Shoko unrecognized anime files via Sonarr + Anime-Lists."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

from config import load_config
from linker import Linker
from tools.bootstrap_map import bootstrap_pins


def acquire_lock(lock_path: Path) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    if args.dry_run:
        cfg.setdefault("behavior", {})["dry_run"] = True
    lock_file = Path(cfg.get("behavior", {}).get("lock_file", "/opt/shoko-autolink/state/.lock"))
    lock_fd = None
    # Cron should use `flock` on lock_file and pass --no-lock (nested flock always fails).
    skip_lock = args.no_lock or os.environ.get("SHOKO_AUTOLINK_NO_LOCK") == "1"
    if not skip_lock:
        lock_fd = acquire_lock(lock_file)
        if lock_fd is None:
            print("Another instance holds the lock.", file=sys.stderr)
            return 2
    try:
        linker = Linker(cfg)
        stats = linker.run(series_filter=args.series)
        print(json.dumps(stats, indent=2))
        return 0
    finally:
        if not skip_lock and lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    state_dir = Path(cfg.get("paths", {}).get("state_dir", "/opt/shoko-autolink/state"))
    state_path = state_dir / "last_run.json"
    review_path = state_dir / "needs_review.jsonl"
    if state_path.exists():
        print("=== last_run.json ===")
        print(state_path.read_text())
    else:
        print("No last_run.json")
    if review_path.exists():
        lines = review_path.read_text().strip().splitlines()
        reasons: dict[str, int] = {}
        for line in lines:
            if not line:
                continue
            r = json.loads(line).get("reason", "?")
            reasons[r] = reasons.get(r, 0) + 1
        print(f"\n=== needs_review ({len(lines)} entries) ===")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")
    return 0


def cmd_bootstrap_map(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    result = bootstrap_pins(cfg, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


def cmd_retry_review(args: argparse.Namespace) -> int:
    cfg = load_config(getattr(args, "config", None))
    state_dir = Path(cfg.get("paths", {}).get("state_dir", "/opt/shoko-autolink/state"))
    review_path = state_dir / "needs_review.jsonl"
    if review_path.exists() and not args.keep_review_log:
        backup = review_path.with_suffix(".jsonl.bak")
        backup.write_text(review_path.read_text())
        review_path.write_text("")
        print(f"Rotated review log to {backup}")
    return cmd_run(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shoko unrecognized file auto-linker")
    parser.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run auto-link pass")
    run_p.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--series", help="Limit to one series folder name")
    run_p.add_argument("--no-lock", action="store_true")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="Show last run and review counts")
    status_p.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    status_p.set_defaults(func=cmd_status)

    boot_p = sub.add_parser("bootstrap-map", help="Auto-generate anidb_map season pins")
    boot_p.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    boot_p.add_argument("--dry-run", action="store_true")
    boot_p.set_defaults(func=cmd_bootstrap_map)

    retry_p = sub.add_parser("retry-review", help="Clear review log and re-run linker")
    retry_p.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    retry_p.add_argument("--dry-run", action="store_true")
    retry_p.add_argument("--series", help="Limit to one series folder name")
    retry_p.add_argument("--no-lock", action="store_true")
    retry_p.add_argument(
        "--keep-review-log",
        action="store_true",
        help="Do not rotate needs_review.jsonl before run",
    )
    retry_p.set_defaults(func=cmd_retry_review)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
