"""Create a test channel (with characters and settings) through the public API.

This is an operator tool, not a seeder: nothing here runs at boot, and no story content is
hardcoded. Every field comes from a small JSON spec file or from the flags below, and the channel
is created exactly the way the dashboard creates one (same endpoint, same validation, same audit).

Why an API client rather than direct database inserts: a channel created here behaves like any
other — it is visible in the dashboard, it can be edited or deleted, and it exercises the real
request path. Rows injected into the database would skip validation and could not be trusted as a
test of the product.

Usage (from apps/api):

    # create the default demo channel for testing
    python -m scripts.create_test_channel

    # from a spec file (see scripts/channel-spec.example.json)
    python -m scripts.create_test_channel --spec scripts/channel-spec.example.json

    # remove it again
    python -m scripts.create_test_channel --delete my-test-channel

Environment:
    FACTORY_API       base URL of the API (default http://127.0.0.1:8000/api/v1)
    FACTORY_EMAIL     admin email (default: BOOTSTRAP_ADMIN_EMAIL)
    FACTORY_PASSWORD  admin password (default: BOOTSTRAP_ADMIN_PASSWORD)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXAMPLE_SPEC = Path(__file__).with_name("channel-spec.example.json")


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.cookie: str | None = None

    def _request(self, path: str, method: str = "GET", body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.cookie:
            request.add_header("Cookie", self.cookie)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = json.loads(raw) if raw else None
            message = (payload or {}).get("error", {}).get("message") or str(payload)
            raise ApiError(f"{method} {path} -> {exc.code}: {message}") from None

    def login(self) -> None:
        data = json.dumps({"email": self.email, "password": self.password}).encode()
        request = urllib.request.Request(f"{self.base}/auth/login", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                self.cookie = (response.headers.get("set-cookie") or "").split(";")[0]
        except urllib.error.HTTPError as exc:
            raise ApiError(f"login failed ({exc.code}) for {self.email}") from None
        if not self.cookie:
            raise ApiError("login succeeded but no session cookie was returned")

    def channels(self) -> list[dict]:
        _, data = self._request("/channels")
        return data if isinstance(data, list) else data.get("items", [])

    def create(self, spec: dict, *, dry_run: bool) -> dict:
        settings = spec.get("settings") or {}
        payload = {k: v for k, v in spec.items() if k in
                   ("name", "handle", "description", "niche", "audience", "language",
                    "video_format", "visual_mode", "accent_color")}
        if settings:
            payload["settings"] = settings
        if dry_run:
            return payload
        _, channel = self._request("/channels", "POST", payload)
        return channel

    def update(self, channel_id: str, spec: dict) -> dict:
        payload = {k: v for k, v in spec.items() if k in
                   ("name", "handle", "description", "niche", "audience", "language",
                    "video_format", "visual_mode", "accent_color", "settings")}
        _, channel = self._request(f"/channels/{channel_id}", "PATCH", payload)
        return channel

    def set_characters(self, channel_id: str, characters: list[dict]) -> list[dict]:
        _, data = self._request(f"/channels/{channel_id}/characters", "PUT", characters)
        return data or []

    def delete(self, channel_id: str) -> None:
        self._request(f"/channels/{channel_id}", "DELETE")

    def start_production(self, channel_id: str) -> dict:
        _, workflow = self._request(f"/workflows?channel_id={channel_id}&force_approval=true", "POST")
        return workflow


def load_spec(path: str | None) -> dict:
    """Read the channel spec.

    The example file is the default, so the documented sample and the tool's default can never
    drift apart (they did: the default dict still described the old demo channel).
    """
    return json.loads(Path(path or EXAMPLE_SPEC).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    from app.core.settings import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", help="JSON file describing the channel (default: a small demo)")
    parser.add_argument("--handle", help="override the handle")
    parser.add_argument("--name", help="override the name")
    parser.add_argument("--api", default=os.getenv("FACTORY_API", "http://127.0.0.1:8000/api/v1"))
    parser.add_argument("--email", default=os.getenv("FACTORY_EMAIL", settings.bootstrap_admin_email))
    parser.add_argument(
        "--password", default=os.getenv("FACTORY_PASSWORD", settings.bootstrap_admin_password)
    )
    parser.add_argument("--dry-run", action="store_true", help="print the payload without calling the API")
    parser.add_argument(
        "--update",
        action="store_true",
        help="apply the spec to the existing channel with this handle instead of creating one",
    )
    parser.add_argument("--delete", metavar="HANDLE", help="delete the channel with this handle and exit")
    parser.add_argument(
        "--produce", action="store_true", help="after creating, start one production run on the channel"
    )
    args = parser.parse_args(argv)

    client = Client(args.api, args.email, args.password)
    try:
        client.login()
        if args.delete:
            matches = [c for c in client.channels() if c["handle"] == args.delete]
            if not matches:
                print(f"no channel with handle '{args.delete}'")
                return 1
            for channel in matches:
                client.delete(channel["id"])
                print(f"deleted {channel['name']} ({channel['handle']})")
            return 0

        spec = load_spec(args.spec)
        if args.name:
            spec["name"] = args.name
        if args.handle:
            spec["handle"] = args.handle

        if any(c["handle"] == spec["handle"] for c in client.channels()):
            if not args.update:
                print(f"channel '{spec['handle']}' already exists — use --update to fill it, "
                      f"or --delete it first")
                return 1
            existing = next(c for c in client.channels() if c["handle"] == spec["handle"])
            if args.dry_run:
                print(json.dumps({"patch": spec, "target": existing["id"]}, indent=2, ensure_ascii=False))
                return 0
            channel = client.update(existing["id"], spec)
            print(f"updated channel {channel['name']} ({channel['handle']}) id={channel['id']}")
        else:
            channel = client.create(spec, dry_run=args.dry_run)
            if args.dry_run:
                print(json.dumps(channel, indent=2, ensure_ascii=False))
                return 0
            print(f"created channel {channel['name']} ({channel['handle']}) id={channel['id']}")

        characters = spec.get("characters") or []
        if characters:
            saved = client.set_characters(channel["id"], characters)
            print(f"characters: {', '.join(c['name'] for c in saved)}")
        else:
            print("characters: none (kept as they are)" if args.update else "characters: none")

        if args.produce:
            workflow = client.start_production(channel["id"])
            print(f"production started: workflow {workflow['id']}")
            print(f"  watch it with: GET /api/v1/workflows/{workflow['id']}")
        return 0
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
