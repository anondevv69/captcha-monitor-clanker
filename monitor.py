#!/usr/bin/env python3
"""CAPTCHA feed monitor with Discord/Telegram alerts.

This script polls the CAPTCHA feed API and sends notifications when posts
contain tracked keywords and/or contract-address-like strings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ETH_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
# Heuristic for Solana-style base58 contract addresses.
SOL_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])")

MAX_TRACKED_IDS = 5000


@dataclass
class Config:
    base_url: str
    app_base_url: str
    api_key: str
    poll_interval_seconds: int
    feed_sort: str
    feed_limit: int
    feed_max_pages: int
    keywords: List[str]
    state_file: Path
    bootstrap_skip_existing: bool
    discord_webhook_url: Optional[str]
    telegram_bot_token: Optional[str]
    telegram_chat_id: Optional[str]
    dry_run: bool

    @staticmethod
    def from_env() -> "Config":
        # Official API: https://docs.captcha.social/api-reference/posts/get-feed
        base_url = os.getenv("CAPTCHA_BASE_URL", "https://api.captcha.social").rstrip("/")
        # Human-readable links in alerts (web app). API host is used for JSON calls only.
        app_base_url = os.getenv("CAPTCHA_APP_BASE_URL", "https://captcha.social").rstrip("/")
        api_key = os.getenv("CAPTCHA_API_KEY", "").strip()
        if not api_key:
            raise ValueError("CAPTCHA_API_KEY is required")

        poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "20"))
        # trending = what's hot now; latest = chronological (see cursor_type in API response).
        feed_sort = os.getenv("FEED_SORT", "trending").strip().lower()
        if feed_sort not in {"latest", "trending"}:
            raise ValueError("FEED_SORT must be either 'latest' or 'trending'")

        feed_limit = int(os.getenv("FEED_LIMIT", "50"))
        if not (1 <= feed_limit <= 50):
            raise ValueError("FEED_LIMIT must be between 1 and 50")

        # Paginate GET /api/v1/feed using next_cursor until has_more is false or this cap is hit.
        feed_max_pages = int(os.getenv("FEED_MAX_PAGES", "5"))
        if not (1 <= feed_max_pages <= 50):
            raise ValueError("FEED_MAX_PAGES must be between 1 and 50")

        keywords_env = os.getenv("ALERT_KEYWORDS", "clank,pump,ba3")
        keywords = [k.strip().lower() for k in keywords_env.split(",") if k.strip()]
        if not keywords:
            raise ValueError("ALERT_KEYWORDS must contain at least one keyword")

        state_file = Path(os.getenv("STATE_FILE", ".captcha_monitor_state.json")).expanduser()
        bootstrap_skip_existing = os.getenv("BOOTSTRAP_SKIP_EXISTING", "true").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None
        telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None

        dry_run = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes"}
        if not dry_run and not discord_webhook_url and not (telegram_bot_token and telegram_chat_id):
            raise ValueError(
                "Configure DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID, "
                "or set DRY_RUN=true"
            )

        return Config(
            base_url=base_url,
            app_base_url=app_base_url,
            api_key=api_key,
            poll_interval_seconds=poll_interval_seconds,
            feed_sort=feed_sort,
            feed_limit=feed_limit,
            feed_max_pages=feed_max_pages,
            keywords=keywords,
            state_file=state_file,
            bootstrap_skip_existing=bootstrap_skip_existing,
            discord_webhook_url=discord_webhook_url,
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
            dry_run=dry_run,
        )


class CaptchaAPI:
    def __init__(self, config: Config) -> None:
        self._base_url = config.base_url
        self._api_key = config.api_key

    def _request_json(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        if query:
            cleaned_query = {k: v for k, v in query.items() if v is not None}
            if cleaned_query:
                url = f"{url}?{urlencode(cleaned_query)}"

        payload: Optional[bytes] = None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(url=url, data=payload, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed ({exc.code}): {body_text}") from exc
        except URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc}") from exc

    def get_me(self) -> Dict[str, Any]:
        data = self._request_json("GET", "/api/v1/me")
        return data if isinstance(data, dict) else {}

    def get_feed(
        self,
        sort: str,
        limit: int,
        cursor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        data = self._request_json(
            "GET",
            "/api/v1/feed",
            query={"sort": sort, "limit": limit, "cursor": cursor},
        )
        return data if isinstance(data, dict) else {"data": data}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bootstrapped = False
        self.checked_ids: List[str] = []
        self._checked_set: Set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logging.warning("State file is invalid JSON, starting with empty state")
            return

        self.bootstrapped = bool(data.get("bootstrapped", False))
        checked_ids = data.get("checked_ids", [])
        if isinstance(checked_ids, list):
            normalized = [str(x) for x in checked_ids if x]
            self.checked_ids = normalized[-MAX_TRACKED_IDS:]
            self._checked_set = set(self.checked_ids)

    def has_seen(self, post_id: str) -> bool:
        return post_id in self._checked_set

    def mark_seen(self, post_id: str) -> None:
        if post_id in self._checked_set:
            return
        self.checked_ids.append(post_id)
        self._checked_set.add(post_id)
        if len(self.checked_ids) > MAX_TRACKED_IDS:
            overflow = len(self.checked_ids) - MAX_TRACKED_IDS
            for stale_id in self.checked_ids[:overflow]:
                self._checked_set.discard(stale_id)
            self.checked_ids = self.checked_ids[overflow:]

    def save(self) -> None:
        payload = {
            "bootstrapped": self.bootstrapped,
            "checked_ids": self.checked_ids[-MAX_TRACKED_IDS:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class Notifier:
    def __init__(
        self,
        discord_webhook_url: Optional[str],
        telegram_bot_token: Optional[str],
        telegram_chat_id: Optional[str],
        dry_run: bool,
    ) -> None:
        self.discord_webhook_url = discord_webhook_url
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.dry_run = dry_run

    def send(self, message: str) -> None:
        if self.dry_run:
            logging.info("DRY_RUN alert:\n%s", message)
            return

        failures: List[str] = []
        if self.discord_webhook_url:
            try:
                self._send_discord(message)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"discord: {exc}")

        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                self._send_telegram(message)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"telegram: {exc}")

        if failures:
            raise RuntimeError("; ".join(failures))

    def _send_discord(self, message: str) -> None:
        body = {"content": message}
        # Discord sits behind Cloudflare; the default Python-urllib User-Agent is often
        # blocked (HTTP 1010 browser_signature_banned). Use an explicit webhook client UA.
        ua = os.getenv(
            "DISCORD_WEBHOOK_USER_AGENT",
            "DiscordWebhook/1.0 (+https://discord.com)",
        ).strip()
        extra = {"User-Agent": ua} if ua else {}
        self._post_json(self.discord_webhook_url, body, extra_headers=extra)

    def _send_telegram(self, message: str) -> None:
        assert self.telegram_bot_token is not None
        assert self.telegram_chat_id is not None
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        body = {
            "chat_id": self.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
        self._post_json(url, body)

    @staticmethod
    def _post_json(
        url: str,
        body: Dict[str, Any],
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = Request(
            url=url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(req, timeout=20) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}")
                raw = resp.read()
                if raw:
                    maybe_json = raw.decode("utf-8", errors="replace").strip()
                    if maybe_json and maybe_json.startswith("{"):
                        payload = json.loads(maybe_json)
                        if payload.get("ok") is False:
                            raise RuntimeError(f"Telegram error: {payload}")
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body_text}") from exc
        except URLError as exc:
            raise RuntimeError(str(exc)) from exc


def parse_feed_payload(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Any]]:
    """Parse GET /api/v1/feed body: posts, has_more, next_cursor, cursor_type (official API)."""
    posts: List[Dict[str, Any]] = []
    next_cursor: Optional[Any] = None

    if isinstance(payload.get("posts"), list):
        posts = [p for p in payload["posts"] if isinstance(p, dict)]
    elif isinstance(payload.get("items"), list):
        posts = [p for p in payload["items"] if isinstance(p, dict)]
    elif isinstance(payload.get("results"), list):
        posts = [p for p in payload["results"] if isinstance(p, dict)]
    elif isinstance(payload.get("data"), list):
        posts = [p for p in payload["data"] if isinstance(p, dict)]
    elif isinstance(payload.get("data"), dict):
        data = payload["data"]
        for key in ("posts", "items", "results"):
            if isinstance(data.get(key), list):
                posts = [p for p in data[key] if isinstance(p, dict)]
                break
        next_cursor = (
            data.get("next_cursor")
            or data.get("nextCursor")
            or data.get("cursor")
            or data.get("next")
        )

    if next_cursor is None:
        next_cursor = (
            payload.get("next_cursor")
            or payload.get("nextCursor")
            or payload.get("cursor")
            or payload.get("next")
        )

    return posts, next_cursor


def extract_post_id(post: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "_id", "post_id"):
        val = post.get(key)
        if val:
            return str(val)
    return None


def extract_content(post: Dict[str, Any]) -> str:
    value = post.get("content")
    return str(value) if value is not None else ""


def extract_author_handle(post: Dict[str, Any]) -> str:
    for key in ("handle", "author_handle", "user_handle", "username"):
        val = post.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    author = post.get("author")
    if isinstance(author, dict):
        for key in ("handle", "username"):
            val = author.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return "unknown"


def find_matches(content: str, keywords: Sequence[str]) -> Tuple[List[str], List[str]]:
    normalized = content.lower()

    found_keywords: List[str] = []
    for keyword in keywords:
        if keyword in normalized:
            found_keywords.append(keyword)

    eth_matches = ETH_ADDRESS_RE.findall(content)
    sol_matches = SOL_ADDRESS_RE.findall(content)
    addresses: List[str] = sorted(set(eth_matches + sol_matches))

    return found_keywords, addresses


def build_alert_message(
    post: Dict[str, Any],
    found_keywords: Sequence[str],
    addresses: Sequence[str],
    app_base_url: str,
    api_base_url: str,
) -> str:
    post_id = extract_post_id(post) or "unknown"
    author = extract_author_handle(post)
    content = extract_content(post).strip().replace("\n", " ")
    if len(content) > 350:
        content = f"{content[:347]}..."

    match_parts: List[str] = []
    if found_keywords:
        match_parts.append(f"keywords={', '.join(found_keywords)}")
    if addresses:
        match_parts.append(f"contracts={', '.join(addresses)}")
    match_text = "; ".join(match_parts) if match_parts else "unknown"

    web_url = f"{app_base_url.rstrip('/')}/post/{post_id}"
    api_ref = f"{api_base_url.rstrip('/')}/api/v1/posts/{post_id}/replies"
    return (
        "🚨 CAPTCHA feed alert\n"
        f"Author: @{author}\n"
        f"Post ID: {post_id}\n"
        f"Matches: {match_text}\n"
        f"Content: {content}\n"
        f"Open: {web_url}\n"
        f"API: {api_ref}"
    )


class FeedMonitor:
    def __init__(self, config: Config, api: CaptchaAPI, state: StateStore, notifier: Notifier) -> None:
        self.config = config
        self.api = api
        self.state = state
        self.notifier = notifier

    def poll_once(self) -> None:
        posts: List[Dict[str, Any]] = []
        cursor: Optional[Any] = None
        last_cursor_type: Optional[str] = None
        pages_fetched = 0

        for _ in range(self.config.feed_max_pages):
            payload = self.api.get_feed(
                sort=self.config.feed_sort,
                limit=self.config.feed_limit,
                cursor=cursor,
            )
            if not isinstance(payload, dict):
                break
            if last_cursor_type is None and payload.get("cursor_type"):
                last_cursor_type = str(payload["cursor_type"])

            page_posts, next_cursor = parse_feed_payload(payload)
            pages_fetched += 1
            posts.extend(page_posts)

            if not payload.get("has_more"):
                break
            if next_cursor is None:
                break
            cursor = next_cursor

        # De-dupe by post id while preserving order (pagination overlap is unlikely).
        seen_ids: Set[str] = set()
        deduped: List[Dict[str, Any]] = []
        for post in posts:
            pid = extract_post_id(post)
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            deduped.append(post)
        posts = deduped

        if not posts:
            logging.info(
                "No feed posts in response (sort=%s pages=%s cursor_type=%s)",
                self.config.feed_sort,
                pages_fetched,
                last_cursor_type,
            )
            return

        logging.info(
            "Feed fetch: sort=%s pages=%d posts=%d cursor_type=%s",
            self.config.feed_sort,
            pages_fetched,
            len(posts),
            last_cursor_type,
        )

        # Process older-to-newer for stable alert ordering.
        posts = list(reversed(posts))

        if not self.state.bootstrapped:
            self.state.bootstrapped = True
            if self.config.bootstrap_skip_existing:
                for post in posts:
                    post_id = extract_post_id(post)
                    if post_id:
                        self.state.mark_seen(post_id)
                self.state.save()
                logging.info(
                    "Bootstrap complete. Marked %d existing posts as seen (no alerts sent).",
                    len(posts),
                )
                return
            self.state.save()

        alerts_sent = 0
        scanned = 0
        for post in posts:
            post_id = extract_post_id(post)
            if not post_id:
                continue
            if self.state.has_seen(post_id):
                continue

            scanned += 1
            content = extract_content(post)
            found_keywords, addresses = find_matches(content, self.config.keywords)
            if found_keywords or addresses:
                message = build_alert_message(
                    post,
                    found_keywords,
                    addresses,
                    self.config.app_base_url,
                    self.config.base_url,
                )
                self.notifier.send(message)
                alerts_sent += 1

            self.state.mark_seen(post_id)

        self.state.save()
        logging.info("Poll done. scanned=%d alerts_sent=%d", scanned, alerts_sent)


def _micro_usdc_to_str(micro: Any) -> str:
    try:
        n = int(micro)
    except (TypeError, ValueError):
        return str(micro)
    return f"${n / 1_000_000:.2f}"


def log_profile_balance(api: CaptchaAPI) -> None:
    """Log GET /api/v1/me — see https://docs.captcha.social/api-reference/users/get-me"""
    try:
        me = api.get_me()
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not fetch /api/v1/me: %s", exc)
        return

    handle = me.get("handle") or me.get("username") or "unknown"
    display = me.get("display_name") or ""
    name_part = f"{display} (@{handle})" if display else f"@{handle}"

    earned = me.get("earned_usdc_micro")
    total_earned = me.get("total_earned_micro")
    total_spent = me.get("total_spent_micro")

    parts = [f"Connected as {name_part}"]
    if earned is not None:
        parts.append(f"earned (engagement) {_micro_usdc_to_str(earned)}")
    if total_earned is not None:
        parts.append(f"total earned {_micro_usdc_to_str(total_earned)}")
    if total_spent is not None:
        parts.append(f"total spent {_micro_usdc_to_str(total_spent)}")

    logging.info("CAPTCHA profile: %s", " | ".join(parts))


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = Config.from_env()
    except Exception as exc:  # noqa: BLE001
        logging.error("Config error: %s", exc)
        return 2

    api = CaptchaAPI(config)
    state = StateStore(config.state_file)
    notifier = Notifier(
        discord_webhook_url=config.discord_webhook_url,
        telegram_bot_token=config.telegram_bot_token,
        telegram_chat_id=config.telegram_chat_id,
        dry_run=config.dry_run,
    )
    monitor = FeedMonitor(config, api, state, notifier)

    log_profile_balance(api)

    run_once = os.getenv("RUN_ONCE", "false").strip().lower() in {"1", "true", "yes"}
    if run_once:
        try:
            monitor.poll_once()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Poll failed: %s", exc)
            return 1
        return 0

    logging.info(
        "Starting monitor. api=%s sort=%s limit=%d max_pages=%d interval=%ss keywords=%s dry_run=%s",
        config.base_url,
        config.feed_sort,
        config.feed_limit,
        config.feed_max_pages,
        config.poll_interval_seconds,
        ",".join(config.keywords),
        config.dry_run,
    )
    while True:
        try:
            monitor.poll_once()
        except KeyboardInterrupt:
            logging.info("Shutting down on keyboard interrupt")
            return 0
        except Exception as exc:  # noqa: BLE001
            logging.exception("Poll failed: %s", exc)
        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
