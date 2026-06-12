#!/usr/bin/env python3
"""Audit Sonarr anime episode filenames against standard naming."""
from __future__ import annotations

import argparse
import os
import re
import sys

import requests

SONARR_EP_RE = re.compile(r"^.+ - S\d+E\d+ - .+\.(mkv|mp4|avi|m4v)$", re.I)
RELEASE_STYLE = re.compile(
    r"^\[|\.S\d+E\d+\.|\.S\d+\.|^[^-]+\.S\d+E\d+|^[A-Za-z0-9]{20,}\.mkv", re.I
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8989")
    p.add_argument("--api-key", required=True)
    p.add_argument("--prefix", default="/data/anime")
    args = p.parse_args()
    H = {"X-Api-Key": args.api_key}
    series = requests.get(f"{args.url}/api/v3/series", headers=H, timeout=60).json()
    anime = [s for s in series if s.get("path", "").startswith(args.prefix)]
    bad = []
    for s in anime:
        efs = requests.get(
            f"{args.url}/api/v3/episodefile",
            headers=H,
            params={"seriesId": s["id"]},
            timeout=30,
        ).json()
        for ef in efs:
            fn = os.path.basename(ef.get("relativePath") or "")
            if fn and (not SONARR_EP_RE.match(fn) or RELEASE_STYLE.search(fn)):
                bad.append((s["title"], fn))
    print(f"Anime series: {len(anime)}")
    print(f"Non-standard files: {len(bad)}")
    for title, fn in bad[:30]:
        print(f"  {title}: {fn[:80]}")
    if len(bad) > 30:
        print(f"  ... and {len(bad) - 30} more")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
