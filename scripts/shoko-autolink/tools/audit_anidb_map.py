#!/usr/bin/env python3
"""Audit anidb_map.json against anime-list.xml for corrupted/wrong entries."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import argparse
import difflib
import json
import xml.etree.ElementTree as ET
from typing import Any

from config import load_config


def _unaccent(s: str) -> str:
    """Strip accents and normalize unicode for comparison."""
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def _fuzzy_match(a: str, b: str) -> bool:
    """True if two anime names are close enough to be the same series."""
    a = _unaccent(a).lower().strip()
    b = _unaccent(b).lower().strip()
    # Direct containment (e.g. "Sword Art Online" in "Sword Art Online: Alicization")
    if a in b or b in a:
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio > 0.5


def _parse_map_key(key: str) -> tuple[str, int | None]:
    """Parse 'Folder Name::S01' → ('Folder Name', 1)."""
    if "::S" in key:
        base, season_str = key.rsplit("::S", 1)
        try:
            return base, int(season_str)
        except ValueError:
            return key, None
    return key, None


def _build_anidb_index(xml_path: Path) -> dict[int, dict[str, Any]]:
    """Build anidb_id → {tvdb_id, name, season} index from anime-list.xml."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    index: dict[int, dict[str, Any]] = {}
    for node in root.findall("anime"):
        aid_raw = node.get("anidbid")
        tid_raw = node.get("tvdbid")
        if not aid_raw or not tid_raw or tid_raw == "movie":
            continue
        try:
            anidb_id = int(aid_raw)
            tvdb_id = int(tid_raw)
        except ValueError:
            continue
        name_el = node.find("name")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        if not name:
            continue
        # Only keep the first entry per anidb_id (anidb IDs are unique per series)
        if anidb_id not in index:
            index[anidb_id] = {
                "tvdb_id": tvdb_id,
                "name": name,
            }
    return index


def audit(cfg: dict[str, Any]) -> dict[str, Any]:
    paths = cfg.get("paths", {})
    data_dir = Path(paths.get("data_dir", "/opt/shoko-autolink/data"))
    al_cfg = cfg.get("anime_lists", {})
    xml_path = Path(al_cfg.get("cache_path", str(data_dir / "anime-list.xml")))

    map_path = data_dir / "anidb_map.json"
    learned_path = data_dir / "learned_map.json"

    if not map_path.exists():
        print(f"ERROR: anidb_map.json not found at {map_path}", file=sys.stderr)
        sys.exit(1)

    if not xml_path.exists():
        print(f"ERROR: anime-list.xml not found at {xml_path}", file=sys.stderr)
        sys.exit(1)

    with map_path.open() as f:
        anidb_map = json.load(f)

    anidb_index = _build_anidb_index(xml_path)

    learned_map: dict[str, Any] = {}
    if learned_path.exists():
        with learned_path.open() as f:
            learned_map = json.load(f)

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    ok: list[dict[str, Any]] = []

    for key, entry in anidb_map.items():
        folder_name, season = _parse_map_key(key)
        tvdb_in_map = entry.get("tvdb_id")
        note = entry.get("note", "")

        # Collect all anidb_ids to check (handles both legacy and ranges schema)
        aids_to_check: list[dict[str, Any]] = []
        if "ranges" in entry:
            for r in entry["ranges"]:
                aids_to_check.append({
                    "anidb_id": r.get("anidb_id"),
                    "offset": r.get("episode_offset", 0),
                    "range": f"{r.get('start','?')}-{r.get('end','?')}",
                })
        elif "anidb_id" in entry:
            aids_to_check.append({
                "anidb_id": entry.get("anidb_id"),
                "offset": entry.get("episode_offset", 0),
            })

        if not aids_to_check:
            errors.append({
                "key": key,
                "anidb_id": None,
                "issue": "no_ids",
                "detail": "Entry has no anidb_id or ranges",
            })
            continue

        # Check each anidb_id in the entry
        entry_ok = True
        for aid_info in aids_to_check:
            anidb_id = aid_info["anidb_id"]

            # Check 1: Placeholder ID
            if anidb_id is None or anidb_id == 0:
                errors.append({
                    "key": key,
                    "anidb_id": anidb_id,
                    "issue": "placeholder_id",
                    "detail": f"anidb_id is 0 or null in entry (range={aid_info.get('range','legacy')})",
                })
                entry_ok = False
                continue

            # Check 2: Missing from anime-lists
            al_info = anidb_index.get(int(anidb_id))
            if al_info is None:
                errors.append({
                    "key": key,
                    "anidb_id": anidb_id,
                    "issue": "not_in_anime_lists",
                    "detail": f"AniDB ID {anidb_id} not found in anime-list.xml (range={aid_info.get('range','legacy')})",
                })
                entry_ok = False
                continue

            al_name = al_info["name"]
            al_tvdb = al_info["tvdb_id"]

            # Check 3: Cross-ID contamination
            tvdb_mismatch = tvdb_in_map is not None and int(tvdb_in_map) != al_tvdb
            if not _fuzzy_match(folder_name, al_name) and tvdb_mismatch:
                warnings.append({
                    "key": key,
                    "anidb_id": anidb_id,
                    "issue": "cross_id_contamination",
                    "detail": (
                        f"AniDB ID {anidb_id} maps to '{al_name}' (TVDB {al_tvdb}), "
                        f"but key suggests '{folder_name}' (map tvdb={tvdb_in_map})"
                    ),
                    "al_name": al_name,
                    "al_tvdb": al_tvdb,
                })
                entry_ok = False
                continue

            # TVDB mismatch with name match (weaker signal - might be correct)
            if tvdb_mismatch:
                warnings.append({
                    "key": key,
                    "anidb_id": anidb_id,
                    "issue": "tvdb_mismatch",
                    "detail": (
                        f"Manual map says TVDB {tvdb_in_map}, "
                        f"but anime-lists says TVDB {al_tvdb} for AniDB {anidb_id}"
                    ),
                    "al_tvdb": al_tvdb,
                    "map_tvdb": tvdb_in_map,
                })
                entry_ok = False

            # Check 4: Overrides learned entry with different anidb
            learned = learned_map.get(key)
            if learned and int(learned.get("anidb_id", 0)) != int(anidb_id):
                warnings.append({
                    "key": key,
                    "anidb_id": anidb_id,
                    "issue": "overrides_learned",
                    "detail": (
                        f"Manual map has anidb_id={anidb_id}, "
                        f"but learned_map has anidb_id={learned.get('anidb_id')} "
                        f"(linked {learned.get('linked', '?')} files)"
                    ),
                    "learned_anidb": learned.get("anidb_id"),
                })
                entry_ok = False

        # If we reach here with no issues above, it's OK
        if entry_ok:
            ok.append({
                "key": key,
                "anidb_id": entry.get("anidb_id") or "ranges",
                "al_name": al_info["name"] if 'al_info' in dir() else "(ranges)",
                "al_tvdb": al_info["tvdb_id"] if 'al_info' in dir() else None,
            })

    return {
        "total_entries": len(anidb_map),
        "errors": errors,
        "warnings": warnings,
        "ok": ok,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit anidb_map.json against anime-list.xml"
    )
    parser.add_argument("--config", default="/opt/shoko-autolink/config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    result = audit(cfg)

    # Print report
    print(f"=== anidb_map.json Audit ===\n")
    print(f"Total entries: {result['total_entries']}")
    print(f"  OK:       {len(result['ok'])}")
    print(f"  Warnings: {len(result['warnings'])}")
    print(f"  Errors:   {len(result['errors'])}\n")

    if result["errors"]:
        print("--- ERRORS ---")
        for e in result["errors"]:
            print(f"  [{e['key']}]")
            print(f"    anidb_id={e['anidb_id']}")
            print(f"    {e['issue']}: {e['detail']}\n")

    if result["warnings"]:
        print("--- WARNINGS ---")
        for w in result["warnings"]:
            print(f"  [{w['key']}]")
            print(f"    anidb_id={w['anidb_id']}")
            print(f"    {w['issue']}: {w['detail']}\n")

    if result["ok"]:
        print("--- OK ---")
        for o in result["ok"]:
            print(f"  [{o['key']}] → anidb={o['anidb_id']} [{o['al_name']}]")

    print(f"\nSummary: {len(result['errors'])} errors, {len(result['warnings'])} warnings, {len(result['ok'])} OK")

    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
