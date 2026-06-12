#!/usr/bin/env python3
"""Update Cloudflare A records to the current public IPv4."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

IP_SERVICES = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def fetch_public_ip() -> str:
    for url in IP_SERVICES:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                ip = resp.read().decode().strip()
                if ip.count(".") == 3:
                    return ip
        except OSError:
            continue
    raise RuntimeError("Could not determine public IPv4")


def cf_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("success", False):
        raise RuntimeError(json.dumps(body.get("errors", body), indent=2))
    return body


def main() -> int:
    env_path = os.environ.get("ENV_FILE", "/opt/plex-stack/.cloudflare-ddns.env")
    if not os.path.exists(env_path):
        print(f"Missing {env_path}", file=sys.stderr)
        return 1

    env = load_env(env_path)
    token = env.get("CF_API_TOKEN")
    zone_id = env.get("CF_ZONE_ID")
    if not token or not zone_id:
        print("CF_API_TOKEN and CF_ZONE_ID are required", file=sys.stderr)
        return 1

    current_ip = fetch_public_ip()
    updated = 0
    unchanged = 0
    page = 1

    while True:
        url = (
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
            f"?type=A&per_page=100&page={page}"
        )
        body = cf_request("GET", url, token)
        records = body.get("result", [])
        if not records:
            break

        for record in records:
            name = record["name"]
            old_ip = record["content"]
            if old_ip == current_ip:
                print(f"unchanged: {name} -> {old_ip}")
                unchanged += 1
                continue

            update_url = (
                "https://api.cloudflare.com/client/v4"
                f"/zones/{zone_id}/dns_records/{record['id']}"
            )
            cf_request(
                "PUT",
                update_url,
                token,
                {
                    "type": "A",
                    "name": name,
                    "content": current_ip,
                    "proxied": record.get("proxied", False),
                },
            )
            print(f"updated: {name} {old_ip} -> {current_ip}")
            updated += 1

        page += 1

    print(f"done: updated={updated} unchanged={unchanged} current_ip={current_ip}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode(), file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        raise SystemExit(1) from exc
