#!/usr/bin/env python3
"""Fetch Claude conversations from claude.ai via Playwright auth + API.

Uses Playwright to log in once and persist session cookies. Subsequent runs
reuse cookies (headless) until they expire. Hits claude.ai's internal API
to list and fetch conversations, then writes staged JSON files in the same
format as stage_claude_web.py.

Usage:
    uv run --with pyyaml,playwright _system/scripts/fetch_claude_web.py --login
    uv run --with pyyaml,playwright _system/scripts/fetch_claude_web.py --since 2026-05-01
    uv run --with pyyaml,playwright _system/scripts/fetch_claude_web.py --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

from mictlan.analyzer import list_existing_aliases, list_existing_slugs
from mictlan.stagers.claude_web import parse_conversation, pre_grep_entities

from mictlan.paths import VAULT
STAGING = VAULT / "_system" / "ingestion" / "staging" / "claude-web"
COOKIE_PATH = VAULT / "_system" / "ingestion" / ".claude-web-cookies.json"
BROWSER_DATA = VAULT / "_system" / "ingestion" / ".claude-web-browser"
BASE_URL = "https://claude.ai"
API_BASE = f"{BASE_URL}/api"
PAGE_SIZE = 20


def save_cookies(cookies: list[dict]) -> None:
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def load_cookies() -> list[dict] | None:
    if not COOKIE_PATH.exists():
        return None
    try:
        cookies = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        if not isinstance(cookies, list) or not cookies:
            return None
        return cookies
    except (json.JSONDecodeError, OSError):
        return None


def playwright_login() -> list[dict]:
    from playwright.sync_api import sync_playwright

    BROWSER_DATA.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA),
            headless=False,
            channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{BASE_URL}/login")

        print("Log in to claude.ai in the browser window.")
        print("Waiting for successful authentication (up to 5 min)...")

        page.wait_for_url(
            lambda url: "/new" in url or "/chat" in url or "/recents" in url,
            timeout=300_000,
        )
        time.sleep(2)

        cookies = context.cookies()
        save_cookies(cookies)
        print(f"Saved {len(cookies)} cookies to {COOKIE_PATH.relative_to(VAULT)}")

        context.close()

    return cookies


class WebAPIError(RuntimeError):
    """API call failed. Carries the HTTP status when there is one."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class _Response:
    """Minimal httpx-Response-shaped shim over a Playwright APIResponse."""

    def __init__(self, status_code: int, ok: bool, url: str, payload: object):
        self.status_code = status_code
        self._ok = ok
        self._url = url
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if not self._ok:
            raise WebAPIError(f"HTTP {self.status_code} for {self._url}", self.status_code)


class WebClient:
    """API client backed by Playwright's request context.

    Routes every call through the authenticated browser session, so cookies,
    User-Agent, and TLS fingerprint all match the context that cleared
    Cloudflare. A plain httpx client cannot reproduce the browser's TLS
    fingerprint and gets a 403 at /api/organizations even with valid cookies —
    cf_clearance is bound to the UA+TLS of the session that solved the challenge.
    """

    def __init__(self):
        from playwright.sync_api import sync_playwright

        BROWSER_DATA.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA),
            headless=True,
            channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
        )

    def get(self, path: str, params: dict | None = None) -> _Response:
        url = f"{API_BASE}{path}"
        try:
            resp = self._context.request.get(url, params=params or {})
        except Exception as e:  # transport / navigation failure
            raise WebAPIError(f"request error: {e}") from e
        payload: object = None
        if resp.ok:
            try:
                payload = resp.json()
            except Exception:
                payload = None
        return _Response(resp.status, resp.ok, url, payload)

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            self._pw.stop()


def get_org_id(client: WebClient) -> str:
    resp = client.get("/organizations")
    resp.raise_for_status()
    orgs = resp.json()
    if not orgs:
        raise RuntimeError("No organizations found. Cookies may be expired — re-run with --login.")
    return orgs[0]["uuid"]


def list_conversations(client: WebClient, org_id: str, since: str | None = None, limit: int | None = None) -> list[dict]:
    conversations: list[dict] = []
    offset = 0
    cutoff = datetime.fromisoformat(since) if since else None

    while True:
        resp = client.get(
            f"/organizations/{org_id}/chat_conversations",
            params={"limit": PAGE_SIZE, "offset": offset},
        )
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break

        for c in page:
            updated = c.get("updated_at") or c.get("created_at") or ""
            if cutoff and updated:
                try:
                    # API timestamps are tz-aware; the cutoff is naive — strip tz to compare.
                    updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
                    if updated_dt < cutoff:
                        return conversations
                except (ValueError, TypeError):
                    pass
            conversations.append(c)
            if limit and len(conversations) >= limit:
                return conversations

        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.3)

    return conversations


def fetch_full_conversation(client: WebClient, org_id: str, conv_uuid: str) -> dict:
    resp = client.get(f"/organizations/{org_id}/chat_conversations/{conv_uuid}")
    resp.raise_for_status()
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--login", action="store_true", help="Force interactive login via Playwright")
    ap.add_argument("--since", type=str, default=None, help="Only fetch conversations updated after YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=None, help="Max conversations to fetch")
    ap.add_argument("--clean", action="store_true", help="Remove existing staged files before writing")
    ap.add_argument("--dry-run", action="store_true", help="List conversations without staging")
    args = ap.parse_args()

    cookies = load_cookies()
    if args.login or not cookies:
        if not args.login and not cookies:
            print("No saved cookies found. Opening browser for login...")
        cookies = playwright_login()

    client = WebClient()

    try:
        org_id = get_org_id(client)
    except (WebAPIError, RuntimeError) as e:
        print(f"Auth failed ({e}). Re-run with --login to refresh cookies.", file=sys.stderr)
        client.close()
        return 1

    print(f"org: {org_id[:8]}...")
    print(f"fetching conversation list{f' (since {args.since})' if args.since else ''}...")

    conv_summaries = list_conversations(client, org_id, since=args.since, limit=args.limit)
    print(f"found {len(conv_summaries)} conversations")

    if args.dry_run:
        for c in conv_summaries:
            title = c.get("name") or "(untitled)"
            updated = (c.get("updated_at") or "")[:10]
            print(f"  {c['uuid'][:8]}  {updated}  {title[:80]}")
        client.close()
        return 0

    if args.clean and STAGING.exists():
        for p in STAGING.glob("*.json"):
            if not p.name.startswith("_"):
                p.unlink()
    STAGING.mkdir(parents=True, exist_ok=True)

    aliases = list_existing_aliases()
    slugs = list_existing_slugs()

    staged = 0
    skipped_exists = 0
    skipped_trivial = 0
    errors = 0

    for i, summary in enumerate(conv_summaries):
        conv_uuid = summary["uuid"]
        title = summary.get("name") or "(untitled)"
        out_path = STAGING / f"{conv_uuid}.json"

        if out_path.exists():
            skipped_exists += 1
            continue

        try:
            full = fetch_full_conversation(client, org_id, conv_uuid)
        except WebAPIError as e:
            print(f"  [{conv_uuid[:8]}] fetch error: {e.status_code or e}", file=sys.stderr)
            errors += 1
            if e.status_code in (401, 403):
                print("Session expired. Re-run with --login.", file=sys.stderr)
                break
            continue

        unit = parse_conversation(full)
        if not unit:
            skipped_trivial += 1
            continue

        if unit["total_chars"] > 500_000:
            unit["oversized"] = True
        unit["candidate_entities"] = pre_grep_entities(unit, aliases, slugs)

        out_path.write_text(
            json.dumps(unit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staged += 1

        if (i + 1) % 10 == 0:
            print(f"  progress: {i + 1}/{len(conv_summaries)} (staged={staged})")
        time.sleep(0.2)

    client.close()

    print(f"\nstaged={staged} skipped_exists={skipped_exists} skipped_trivial={skipped_trivial} errors={errors}")
    print(f"staging dir: {STAGING.relative_to(VAULT)}/")
    print(f"with entities: {len(aliases)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
