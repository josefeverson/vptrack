#!/usr/bin/env python3
"""Vote Party tracker for a Minecraft server.

This tool reads public vote pages, estimates the in-game Vote Party counter,
and optionally runs local Minecraft launcher/disconnect commands when a party
is imminent. It never votes, solves CAPTCHAs, or interacts with voting forms.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import platform
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo
import webbrowser


APP_NAME = "vp_tracker"
DEFAULT_CONFIG_PATH = Path("config.json")
DEFAULT_DB_PATH = Path("vp_tracker.sqlite3")


DEFAULT_CONFIG: dict[str, Any] = {
    "vp_party_size": 120,
    "database_path": str(DEFAULT_DB_PATH),
    "polling": {
        "normal_interval_seconds": 15,
        "near_interval_seconds": 10,
        "trigger_interval_seconds": 5,
        "near_threshold": 100,
        "trigger_threshold": 116,
        "http_timeout_seconds": 20,
        "failed_source_backoff_seconds": 180,
        "failed_source_max_backoff_seconds": 1800,
    },
    "minecraft": {
        "mode": "notify",
        "auto_join_enabled": False,
        "prelaunch_threshold": 110,
        "join_threshold_high_confidence": 118,
        "join_threshold_medium_confidence": 119,
        "estimated_join_seconds": 35,
        "join_safety_buffer_votes": 1,
        "max_online_seconds": 240,
        "hard_disconnect_seconds": 300,
        "disconnect_if_chat_vp_below": 115,
        "reward_disconnect_delay_seconds": 8,
        "latest_log_path": "",
        "reward_detection_patterns": [
            "plushie",
            "pokemon plushie",
            "pokémon plushie",
        ],
        "prelaunch_command": "",
        "join_command": "",
        "disconnect_command": "",
    },
    "confidence": {
        "require_count_sources": 3,
        "max_minutes_since_successful_poll": 2,
        "max_minutes_since_calibration": 180,
        "large_delta_warning": 24,
    },
    "estimation": {
        "poll_lock_ttl_seconds": 120,
        "single_source_burst_limit": 3,
        "reader_single_source_burst_limit": 0,
        "stale_count_source_inference_enabled": False,
        "stale_count_source_min_peer_sources": 3,
        "stale_count_source_min_peer_delta": 1,
        "stale_count_source_max_inferred_per_poll": 6,
    },
    "alerts": {
        "desktop_notifications": True,
        "discord_webhook_url": "",
    },
    "gui": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "refresh_seconds": 5,
        "vote_notifications": True,
        "vote_sound": True,
    },
    "service": {
        "healthcheck_max_poll_age_seconds": 180,
        "minimum_successful_sources": 3,
    },
    "http": {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "VotePartyTracker/1.0"
        ),
        "reader_base_url": "https://r.jina.ai/",
    },
    "sources": [
        {
            "name": "count_source_1",
            "url": "https://example.invalid/vote-source-1",
            "type": "count",
            "parser": "votes_this_month",
            "recent_parser": "",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
        {
            "name": "count_source_2",
            "url": "https://example.invalid/vote-source-2",
            "type": "count",
            "parser": "vote_s_count",
            "recent_parser": "",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
        {
            "name": "count_source_3",
            "url": "https://example.invalid/vote-source-3",
            "type": "count",
            "parser": "players_have_voted",
            "recent_parser": "",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
        {
            "name": "count_source_4",
            "url": "https://example.invalid/vote-source-4",
            "type": "count",
            "parser": "votes_heading_count",
            "recent_parser": "",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
        {
            "name": "recent_list_source",
            "url": "https://example.invalid/recent-voters",
            "type": "recent",
            "parser": "recent_username_dates",
            "recent_parser": "",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
        {
            "name": "recent_total_source",
            "url": "https://example.invalid/recent-total",
            "type": "count",
            "parser": "recent_votes_count",
            "recent_parser": "recent_username_dates",
            "fetcher": "http",
            "fallback_fetcher": "",
            "date_timezone": "local",
            "enabled": False,
        },
    ],
}


COUNT_PARSERS: dict[str, re.Pattern[str]] = {
    "votes_this_month": re.compile(r"([\d,]+)\s+Votes\s+this\s+month", re.IGNORECASE),
    "vote_s_count": re.compile(r"Vote\s*\(s\)\s*([\d,]+)", re.IGNORECASE),
    "players_have_voted": re.compile(
        r"([\d,]+)\s+Players\s+have\s+voted", re.IGNORECASE
    ),
    "votes_heading_count": re.compile(r"\bVotes\s+([\d,]+)\b", re.IGNORECASE),
    "recent_votes_count": re.compile(
        r"Recent\s+votes\s+([\d,]+)\s+in\s+\w+", re.IGNORECASE
    ),
}

VP_CHAT_RE = re.compile(r"VP-count;\s*(\d+)\s*/\s*(\d+)\s*!", re.IGNORECASE)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{2,16}$")
DATEISH_RE = re.compile(
    r"("
    r"(today|yesterday)\s+at\s+\d{1,2}:\d{2}\s*(AM|PM)?"
    r"|"
    r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}(:\d{2})?\s*(UTC)?"
    r"|"
    r"\d{1,2}/\d{1,2}/\d{2,4}"
    r")",
    re.IGNORECASE,
)


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag.lower() in {
            "br",
            "p",
            "div",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "section",
        }:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in {
            "p",
            "div",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "section",
        }:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        lines = []
        for line in "".join(self._parts).splitlines():
            stripped = normalize_space(line)
            if stripped:
                lines.append(stripped)
        return "\n".join(lines)


@dataclass
class ParsedEvent:
    external_id: str
    username: str
    vote_time: str
    vote_time_utc: str | None = None


@dataclass
class ParsedSource:
    count: int | None = None
    raw_summary: str = ""
    events: list[ParsedEvent] = field(default_factory=list)


@dataclass
class PollSourceResult:
    name: str
    success: bool
    count: int | None = None
    delta: int = 0
    new_events: int = 0
    reset_detected: bool = False
    skipped: bool = False
    error: str = ""
    raw_summary: str = ""
    fetcher_used: str = ""
    raw_delta: int | None = None
    suppressed_delta: int = 0
    adjustment_note: str = ""


@dataclass
class PollCycleResult:
    started_at: datetime
    ended_at: datetime
    estimate_before: int
    total_delta: int
    successes: int
    failures: int
    count_successes: int
    resets: int
    large_jumps: int
    confidence: str
    estimate_after: int
    vote_parties_crossed: int
    source_results: list[PollSourceResult]
    log_events: list[str] = field(default_factory=list)
    action_messages: list[str] = field(default_factory=list)


@dataclass
class VelocityWindow:
    minutes: int
    votes: int
    velocity_per_minute: float
    eta_seconds: int | None


@dataclass
class DashboardSnapshot:
    generated_at: str
    state: dict[str, Any]
    stats: dict[str, Any]
    history: dict[str, Any]
    sources: list[dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def html_to_visible_text(html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()
    return parser.text()


def compact_text(text: str) -> str:
    return normalize_space(text.replace("\n", " "))


def parse_int(text: str) -> int:
    return int(text.replace(",", ""))


def merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return DEFAULT_CONFIG
    with path.open("r", encoding="utf-8") as fh:
        user_config = json.load(fh)
    return merge_dict(DEFAULT_CONFIG, user_config)


def write_default_config(path: Path, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; use --force to overwrite")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, indent=2)
        fh.write("\n")


def source_enabled(source: dict[str, Any]) -> bool:
    return bool(source.get("enabled", True))


def source_value(source: Any, key: str, default: Any = None) -> Any:
    try:
        value = source[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def parse_count_source(parser_name: str, visible_text: str, html: str = "") -> ParsedSource:
    pattern = COUNT_PARSERS.get(parser_name)
    if not pattern:
        raise ValueError(f"unknown count parser: {parser_name}")

    search_texts = [visible_text, compact_text(visible_text), html]
    match: re.Match[str] | None = None
    for search_text in search_texts:
        match = pattern.search(search_text)
        if match:
            break
    if not match:
        sample = compact_text(visible_text)[:240]
        raise ValueError(f"could not find count using parser {parser_name}; sample={sample!r}")

    count = parse_int(match.group(1))
    return ParsedSource(count=count, raw_summary=normalize_space(match.group(0)))


def parse_recent_source(
    source_name: str,
    parser_name: str,
    visible_text: str,
    date_timezone: str = "local",
    reference_dt: datetime | None = None,
) -> ParsedSource:
    if parser_name != "recent_username_dates":
        raise ValueError(f"unknown recent parser: {parser_name}")
    lines = [line.strip() for line in visible_text.splitlines() if line.strip()]
    events: list[ParsedEvent] = []
    reference = reference_dt or utc_now()

    for idx, line in enumerate(lines[:-1]):
        username = normalize_space(line)
        date_text = normalize_space(lines[idx + 1])
        if USERNAME_RE.match(username) and DATEISH_RE.search(date_text):
            vote_time_utc = parse_vote_time_to_utc(date_text, date_timezone, reference)
            external_id = make_external_id(
                source_name,
                username,
                date_text,
                vote_time_utc,
                date_timezone,
                reference,
            )
            events.append(
                ParsedEvent(
                    external_id=external_id,
                    username=username,
                    vote_time=date_text,
                    vote_time_utc=iso(vote_time_utc) if vote_time_utc else None,
                )
            )

    summary = f"{len(events)} recent voters parsed"
    return ParsedSource(raw_summary=summary, events=events)


def make_external_id(
    source_name: str,
    username: str,
    vote_time: str,
    vote_time_utc: datetime | None = None,
    date_timezone: str = "local",
    reference_dt: datetime | None = None,
) -> str:
    stable_time = canonical_vote_time(
        vote_time,
        vote_time_utc=vote_time_utc,
        date_timezone=date_timezone,
        reference_dt=reference_dt,
    )
    return f"{source_name}:{username.lower()}:{stable_time}"


def canonical_vote_time(
    vote_time: str,
    vote_time_utc: datetime | None = None,
    date_timezone: str = "local",
    reference_dt: datetime | None = None,
) -> str:
    if vote_time_utc:
        return iso(vote_time_utc)
    stable_time = normalize_space(vote_time).lower()
    source_tz = timezone_from_label(date_timezone)
    reference = (reference_dt or utc_now()).astimezone(source_tz)
    today = reference.date()
    if stable_time.startswith("today at "):
        return f"{today.isoformat()} {stable_time.removeprefix('today at ')}"
    if stable_time.startswith("yesterday at "):
        yesterday = today - timedelta(days=1)
        return f"{yesterday.isoformat()} {stable_time.removeprefix('yesterday at ')}"
    return stable_time


def timezone_from_label(label: str | None) -> Any:
    text = normalize_space(str(label or "local"))
    lowered = text.lower()
    if lowered in {"", "local", "system"}:
        return datetime.now().astimezone().tzinfo or timezone.utc
    if lowered in {"utc", "z", "gmt", "+00:00", "-00:00"}:
        return timezone.utc
    offset = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", text)
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        hours = int(offset.group(2))
        minutes = int(offset.group(3))
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    return ZoneInfo(text)


def parse_vote_time_to_utc(
    vote_time: str,
    date_timezone: str = "local",
    reference_dt: datetime | None = None,
) -> datetime | None:
    text = normalize_space(vote_time)
    lowered = text.lower()
    source_tz = timezone_from_label(date_timezone)
    reference = (reference_dt or utc_now()).astimezone(source_tz)
    today = reference.date()
    date_part: datetime.date | None = None
    time_text = ""
    if lowered.startswith("today at "):
        date_part = today
        time_text = text[9:]
    elif lowered.startswith("yesterday at "):
        date_part = today - timedelta(days=1)
        time_text = text[13:]
    if date_part and time_text:
        for fmt in ("%I:%M %p", "%H:%M"):
            try:
                parsed_time = datetime.strptime(time_text.strip(), fmt).time()
                source_dt = datetime.combine(date_part, parsed_time, tzinfo=source_tz)
                return source_dt.astimezone(timezone.utc)
            except ValueError:
                pass

    for fmt in ("%Y-%m-%d %H:%M %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            parsed = datetime.strptime(text, fmt)
            if text.upper().endswith("UTC"):
                return parsed.replace(tzinfo=timezone.utc).astimezone(timezone.utc)
        except ValueError:
            pass

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            source_dt = datetime.strptime(text, fmt).replace(tzinfo=source_tz)
            return source_dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def local_time_label(value: str | None) -> str | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def normalize_fetcher(value: Any) -> str:
    fetcher = normalize_space(str(value or "http")).lower()
    aliases = {
        "": "http",
        "default": "http",
        "direct": "http",
        "urllib": "http",
        "jina": "reader",
        "markdown": "reader",
        "markdown_reader": "reader",
    }
    fetcher = aliases.get(fetcher, fetcher)
    if fetcher not in {"http", "reader"}:
        raise ValueError(f"unknown fetcher: {fetcher}")
    return fetcher


def reader_url_for(url: str, config: dict[str, Any]) -> str:
    base = str(config.get("http", {}).get("reader_base_url") or "https://r.jina.ai/")
    if "{url}" in base:
        encoded = urllib.parse.quote(url, safe=":/?&=%#.-_~+")
        return base.replace("{url}", encoded)
    return base.rstrip("/") + "/" + url


def fetch_url(url: str, config: dict[str, Any], fetcher: str = "http") -> str:
    fetcher_name = normalize_fetcher(fetcher)
    if fetcher_name == "reader":
        return fetch_reader_url(url, config)
    return fetch_http_url(url, config)


def fetch_source_url(
    url: str, config: dict[str, Any], source: Any
) -> tuple[str, str]:
    primary = normalize_fetcher(source_value(source, "fetcher", "http"))
    fallback_raw = source_value(source, "fallback_fetcher", "")
    fallback = normalize_fetcher(fallback_raw) if fallback_raw else ""
    try:
        return fetch_url(url, config, primary), primary
    except Exception as primary_exc:
        if fallback and fallback != primary:
            try:
                return fetch_url(url, config, fallback), fallback
            except Exception as fallback_exc:
                raise RuntimeError(
                    f"{friendly_error(primary_exc)}; {fallback} fallback failed: "
                    f"{friendly_error(fallback_exc)}"
                ) from fallback_exc
        raise


def fetch_http_url(url: str, config: dict[str, Any]) -> str:
    timeout = int(config["polling"]["http_timeout_seconds"])
    headers = {
        "User-Agent": config["http"]["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
    return raw.decode(charset, errors="replace")


def fetch_reader_url(url: str, config: dict[str, Any]) -> str:
    timeout = int(config["polling"]["http_timeout_seconds"])
    headers = {
        "User-Agent": config["http"]["user_agent"],
        "Accept": "text/plain,text/markdown,text/html;q=0.8,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    request = urllib.request.Request(reader_url_for(url, config), headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read()
    return raw.decode(charset, errors="replace")


class Store:
    def __init__(self, db_path: Path, config: dict[str, Any]) -> None:
        self.db_path = db_path
        self.config = config
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()
        self.seed_sources()
        self.repair_relative_vote_times()
        self.ensure_state()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vote_sources (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              url TEXT NOT NULL,
              type TEXT NOT NULL,
              parser TEXT NOT NULL,
              recent_parser TEXT,
              fetcher TEXT,
              fallback_fetcher TEXT,
              date_timezone TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              last_count INTEGER,
              last_checked_at TEXT,
              failure_count INTEGER NOT NULL DEFAULT 0,
              next_allowed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS vote_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_name TEXT NOT NULL,
              count INTEGER,
              raw_summary TEXT,
              checked_at TEXT NOT NULL,
              success INTEGER NOT NULL,
              error TEXT
            );

            CREATE TABLE IF NOT EXISTS vote_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_name TEXT NOT NULL,
              external_id TEXT NOT NULL,
              username TEXT,
              vote_time TEXT,
              vote_time_utc TEXT,
              detected_at TEXT NOT NULL,
              UNIQUE(source_name, external_id)
            );

            CREATE TABLE IF NOT EXISTS vp_state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              current_estimate INTEGER NOT NULL,
              confidence TEXT NOT NULL,
              last_calibrated_at TEXT,
              last_updated_at TEXT NOT NULL,
              total_observed_votes INTEGER NOT NULL DEFAULT 0,
              calibration_source TEXT
            );

            CREATE TABLE IF NOT EXISTS estimate_errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              calibrated_at TEXT NOT NULL,
              previous_estimate INTEGER NOT NULL,
              actual_estimate INTEGER NOT NULL,
              signed_error INTEGER NOT NULL,
              absolute_error INTEGER NOT NULL,
              severity TEXT NOT NULL DEFAULT 'exact',
              message TEXT,
              minutes_since_last_calibration REAL,
              confidence_before TEXT,
              calibration_source TEXT
            );

            CREATE TABLE IF NOT EXISTS join_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              triggered_at TEXT NOT NULL,
              reason TEXT NOT NULL,
              vp_estimate_at_trigger INTEGER NOT NULL,
              confidence TEXT,
              joined_successfully INTEGER NOT NULL DEFAULT 0,
              reward_detected INTEGER NOT NULL DEFAULT 0,
              disconnected_at TEXT
            );

            CREATE TABLE IF NOT EXISTS poll_cycles (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              started_at TEXT NOT NULL,
              ended_at TEXT NOT NULL,
              estimate_before INTEGER,
              total_delta INTEGER NOT NULL,
              successes INTEGER NOT NULL,
              failures INTEGER NOT NULL,
              confidence TEXT NOT NULL,
              estimate_after INTEGER NOT NULL,
              vote_parties_crossed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS source_poll_results (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              poll_cycle_id INTEGER,
              source_name TEXT NOT NULL,
              checked_at TEXT NOT NULL,
              success INTEGER NOT NULL,
              skipped INTEGER NOT NULL DEFAULT 0,
              count INTEGER,
              delta INTEGER NOT NULL DEFAULT 0,
              raw_delta INTEGER NOT NULL DEFAULT 0,
              suppressed_delta INTEGER NOT NULL DEFAULT 0,
              new_events INTEGER NOT NULL DEFAULT 0,
              reset_detected INTEGER NOT NULL DEFAULT 0,
              fetcher_used TEXT,
              adjustment_note TEXT,
              error TEXT,
              raw_summary TEXT,
              FOREIGN KEY(poll_cycle_id) REFERENCES poll_cycles(id)
            );

            CREATE INDEX IF NOT EXISTS idx_source_poll_results_time
            ON source_poll_results(checked_at);

            CREATE INDEX IF NOT EXISTS idx_source_poll_results_source_time
            ON source_poll_results(source_name, checked_at);

            CREATE TABLE IF NOT EXISTS vote_party_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              estimated_at TEXT NOT NULL,
              poll_cycle_id INTEGER,
              estimate_before INTEGER NOT NULL,
              estimate_after INTEGER NOT NULL,
              delta INTEGER NOT NULL,
              confidence TEXT NOT NULL,
              source_count INTEGER NOT NULL DEFAULT 0,
              reason TEXT NOT NULL,
              FOREIGN KEY(poll_cycle_id) REFERENCES poll_cycles(id)
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
              key TEXT PRIMARY KEY,
              value TEXT,
              updated_at TEXT NOT NULL
            );
            """
        )
        self.ensure_schema_compat()
        self.conn.commit()

    def ensure_schema_compat(self) -> None:
        self.ensure_column("poll_cycles", "estimate_before", "INTEGER")
        self.ensure_column(
            "poll_cycles", "vote_parties_crossed", "INTEGER NOT NULL DEFAULT 0"
        )
        self.ensure_column("vote_sources", "recent_parser", "TEXT")
        self.ensure_column("vote_sources", "fetcher", "TEXT")
        self.ensure_column("vote_sources", "fallback_fetcher", "TEXT")
        self.ensure_column("vote_sources", "date_timezone", "TEXT")
        self.ensure_column("vote_events", "vote_time_utc", "TEXT")
        self.ensure_column("source_poll_results", "fetcher_used", "TEXT")
        self.ensure_column("source_poll_results", "raw_delta", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column(
            "source_poll_results", "suppressed_delta", "INTEGER NOT NULL DEFAULT 0"
        )
        self.ensure_column("source_poll_results", "adjustment_note", "TEXT")
        self.ensure_column("estimate_errors", "severity", "TEXT NOT NULL DEFAULT 'exact'")
        self.ensure_column("estimate_errors", "message", "TEXT")
        self.backfill_estimate_error_metadata()
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vote_events_time
            ON vote_events(COALESCE(vote_time_utc, detected_at))
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_vote_events_username_time
            ON vote_events(username, COALESCE(vote_time_utc, detected_at))
            """
        )

    def ensure_column(self, table: str, column: str, column_type: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def backfill_estimate_error_metadata(self) -> None:
        party_size = int(self.config["vp_party_size"])
        rows = self.conn.execute(
            """
            SELECT id, previous_estimate, actual_estimate, signed_error,
                   absolute_error, severity, message
            FROM estimate_errors
            """
        ).fetchall()
        for row in rows:
            absolute_error = int(row["absolute_error"] or 0)
            signed_error = int(row["signed_error"] or 0)
            severity = calibration_error_severity(absolute_error, party_size)
            message = calibration_error_message(
                int(row["previous_estimate"] or 0),
                int(row["actual_estimate"] or 0),
                signed_error,
                severity,
                party_size,
            )
            if (
                row["severity"] != severity
                or not row["message"]
                or (absolute_error > 0 and row["severity"] == "exact")
            ):
                self.conn.execute(
                    """
                    UPDATE estimate_errors
                    SET severity = ?, message = ?
                    WHERE id = ?
                    """,
                    (severity, message, row["id"]),
                )

    def seed_sources(self) -> None:
        configured_names = []
        for source in self.config["sources"]:
            configured_names.append(source["name"])
            self.conn.execute(
                """
                INSERT INTO vote_sources(
                  name, url, type, parser, recent_parser, fetcher,
                  fallback_fetcher, date_timezone, enabled
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  url = excluded.url,
                  type = excluded.type,
                  parser = excluded.parser,
                  recent_parser = excluded.recent_parser,
                  fetcher = excluded.fetcher,
                  fallback_fetcher = excluded.fallback_fetcher,
                  date_timezone = excluded.date_timezone,
                  enabled = excluded.enabled
                """,
                (
                    source["name"],
                    source["url"],
                    source["type"],
                    source["parser"],
                    source.get("recent_parser", ""),
                    source.get("fetcher", "http"),
                    source.get("fallback_fetcher", ""),
                    source.get("date_timezone", "local"),
                    1 if source_enabled(source) else 0,
                ),
            )
        if configured_names:
            placeholders = ", ".join("?" for _ in configured_names)
            self.conn.execute(
                f"UPDATE vote_sources SET enabled = 0 WHERE name NOT IN ({placeholders})",
                configured_names,
            )
        self.conn.commit()

    def repair_relative_vote_times(self) -> None:
        source_rows = self.conn.execute(
            "SELECT name, date_timezone FROM vote_sources"
        ).fetchall()
        timezone_by_source = {
            row["name"]: row["date_timezone"] or "local" for row in source_rows
        }
        rows = self.conn.execute(
            """
            SELECT id, source_name, external_id, username, vote_time,
                   vote_time_utc, detected_at
            FROM vote_events
            WHERE vote_time IS NOT NULL
              AND vote_time != ''
            """
        ).fetchall()
        for row in rows:
            source_name = row["source_name"]
            username = row["username"] or ""
            vote_time = row["vote_time"] or ""
            detected_at = parse_iso(row["detected_at"]) or utc_now()
            date_timezone = timezone_by_source.get(source_name, "local")
            corrected = parse_vote_time_to_utc(vote_time, date_timezone, detected_at)
            if not corrected:
                continue
            corrected_iso = iso(corrected)
            corrected_external_id = make_external_id(
                source_name,
                username,
                vote_time,
                corrected,
                date_timezone,
                detected_at,
            )
            if (
                row["vote_time_utc"] == corrected_iso
                and row["external_id"] == corrected_external_id
            ):
                continue
            try:
                self.conn.execute(
                    """
                    UPDATE vote_events
                    SET vote_time_utc = ?, external_id = ?
                    WHERE id = ?
                    """,
                    (corrected_iso, corrected_external_id, row["id"]),
                )
            except sqlite3.IntegrityError:
                existing = self.conn.execute(
                    """
                    SELECT id, detected_at
                    FROM vote_events
                    WHERE source_name = ? AND external_id = ? AND id != ?
                    """,
                    (source_name, corrected_external_id, row["id"]),
                ).fetchone()
                if existing:
                    existing_detected = parse_iso(existing["detected_at"])
                    current_detected = parse_iso(row["detected_at"])
                    if current_detected and (
                        not existing_detected or current_detected < existing_detected
                    ):
                        self.conn.execute(
                            """
                            UPDATE vote_events
                            SET username = ?, vote_time = ?, vote_time_utc = ?,
                                detected_at = ?
                            WHERE id = ?
                            """,
                            (
                                username,
                                vote_time,
                                corrected_iso,
                                row["detected_at"],
                                existing["id"],
                            ),
                        )
                self.conn.execute("DELETE FROM vote_events WHERE id = ?", (row["id"],))
        self.conn.commit()

    def ensure_state(self) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO vp_state(
              id, current_estimate, confidence, last_calibrated_at,
              last_updated_at, total_observed_votes, calibration_source
            )
            VALUES (1, 0, 'low', NULL, ?, 0, NULL)
            """,
            (iso(),),
        )
        self.conn.commit()

    def get_sources(self) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM vote_sources WHERE enabled = 1 ORDER BY id"
        ).fetchall()
        return list(rows)

    def get_state(self) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM vp_state WHERE id = 1").fetchone()

    def calibrate(self, estimate: int, source: str = "manual") -> None:
        party_size = int(self.config["vp_party_size"])
        normalized = estimate % party_size
        now = iso()
        state = self.get_state()
        previous = int(state["current_estimate"])
        signed_error = circular_signed_error(previous, normalized, party_size)
        absolute_error = abs(signed_error)
        severity = calibration_error_severity(absolute_error, party_size)
        message = calibration_error_message(
            previous, normalized, signed_error, severity, party_size
        )
        last_calibrated_at = parse_iso(state["last_calibrated_at"])
        age_minutes = (
            (utc_now() - last_calibrated_at).total_seconds() / 60.0
            if last_calibrated_at
            else None
        )
        self.conn.execute(
            """
            INSERT INTO estimate_errors(
              calibrated_at, previous_estimate, actual_estimate, signed_error,
              absolute_error, severity, message, minutes_since_last_calibration,
              confidence_before, calibration_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                previous,
                normalized,
                signed_error,
                absolute_error,
                severity,
                message,
                age_minutes,
                state["confidence"],
                source,
            ),
        )
        self.conn.execute(
            """
            UPDATE vp_state
            SET current_estimate = ?,
                confidence = 'high',
                last_calibrated_at = ?,
                last_updated_at = ?,
                calibration_source = ?
            WHERE id = 1
            """,
            (normalized, now, now, source),
        )
        self.reset_count_source_inference_baselines()
        self.conn.commit()

    def apply_vote_delta(self, delta: int) -> int:
        if delta <= 0:
            return int(self.get_state()["current_estimate"])
        party_size = int(self.config["vp_party_size"])
        state = self.get_state()
        updated = (int(state["current_estimate"]) + delta) % party_size
        self.conn.execute(
            """
            UPDATE vp_state
            SET current_estimate = ?,
                last_updated_at = ?,
                total_observed_votes = total_observed_votes + ?
            WHERE id = 1
            """,
            (updated, iso(), delta),
        )
        self.conn.commit()
        return updated

    def set_confidence(self, confidence: str) -> None:
        self.conn.execute(
            "UPDATE vp_state SET confidence = ?, last_updated_at = ? WHERE id = 1",
            (confidence, iso()),
        )
        self.conn.commit()

    def insert_snapshot(
        self,
        source_name: str,
        count: int | None,
        raw_summary: str,
        success: bool,
        error: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO vote_snapshots(
              source_name, count, raw_summary, checked_at, success, error
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_name, count, raw_summary, iso(), 1 if success else 0, error),
        )

    def update_source_success(self, source_name: str, count: int | None) -> None:
        if count is None:
            self.conn.execute(
                """
                UPDATE vote_sources
                SET last_checked_at = ?, failure_count = 0, next_allowed_at = NULL
                WHERE name = ?
                """,
                (iso(), source_name),
            )
        else:
            self.conn.execute(
                """
                UPDATE vote_sources
                SET last_count = ?,
                    last_checked_at = ?,
                    failure_count = 0,
                    next_allowed_at = NULL
                WHERE name = ?
                """,
                (count, iso(), source_name),
            )

    def update_source_failure(self, source: sqlite3.Row) -> None:
        failure_count = int(source["failure_count"] or 0) + 1
        delay = self.source_failure_backoff_seconds(failure_count)
        next_allowed = utc_now() + timedelta(seconds=delay)
        self.conn.execute(
            """
            UPDATE vote_sources
            SET failure_count = ?, last_checked_at = ?, next_allowed_at = ?
            WHERE name = ?
            """,
            (failure_count, iso(), iso(next_allowed), source["name"]),
        )

    def source_failure_backoff_seconds(self, failure_count: int) -> int:
        polling = self.config["polling"]
        configured_base = int(polling["failed_source_backoff_seconds"])
        cap = int(polling["failed_source_max_backoff_seconds"])
        try:
            estimate = int(self.get_state()["current_estimate"])
        except Exception:
            estimate = 0
        active_interval = self.effective_poll_interval_seconds(estimate)
        first_retry = min(configured_base, max(1, active_interval))
        return min(cap, first_retry * (2 ** min(max(0, failure_count - 1), 5)))

    def insert_event(self, source_name: str, event: ParsedEvent) -> bool:
        try:
            self.conn.execute(
                """
                INSERT INTO vote_events(
                  source_name, external_id, username, vote_time, vote_time_utc, detected_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source_name,
                    event.external_id,
                    event.username,
                    event.vote_time,
                    event.vote_time_utc,
                    iso(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def record_poll_cycle(self, result: PollCycleResult) -> None:
        cursor = self.conn.execute(
            """
            INSERT INTO poll_cycles(
              started_at, ended_at, estimate_before, total_delta, successes, failures,
              confidence, estimate_after, vote_parties_crossed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso(result.started_at),
                iso(result.ended_at),
                result.estimate_before,
                result.total_delta,
                result.successes,
                result.failures,
                result.confidence,
                result.estimate_after,
                result.vote_parties_crossed,
            ),
        )
        cycle_id = int(cursor.lastrowid)
        for source_result in result.source_results:
            self.conn.execute(
                """
                INSERT INTO source_poll_results(
                  poll_cycle_id, source_name, checked_at, success, skipped, count,
                  delta, raw_delta, suppressed_delta, new_events, reset_detected,
                  fetcher_used, adjustment_note, error, raw_summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_id,
                    source_result.name,
                    iso(result.ended_at),
                    1 if source_result.success else 0,
                    1 if source_result.skipped else 0,
                    source_result.count,
                    source_result.delta,
                    source_result.raw_delta
                    if source_result.raw_delta is not None
                    else source_result.delta,
                    source_result.suppressed_delta,
                    source_result.new_events,
                    1 if source_result.reset_detected else 0,
                    source_result.fetcher_used,
                    source_result.adjustment_note,
                    source_result.error,
                    source_result.raw_summary,
                ),
            )
        for crossing_index in range(result.vote_parties_crossed):
            self.conn.execute(
                """
                INSERT INTO vote_party_events(
                  estimated_at, poll_cycle_id, estimate_before, estimate_after,
                  delta, confidence, source_count, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso(result.ended_at),
                    cycle_id,
                    result.estimate_before,
                    result.estimate_after,
                    result.total_delta,
                    result.confidence,
                    result.successes,
                    f"estimated crossing {crossing_index + 1}/{result.vote_parties_crossed}",
                ),
            )
        self.conn.commit()

    def votes_in_window(self, minutes: int) -> int:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        row = self.conn.execute(
            "SELECT COALESCE(SUM(total_delta), 0) AS votes FROM poll_cycles WHERE ended_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["votes"] or 0)

    def get_runtime(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    def set_runtime(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, value, iso()),
        )
        self.conn.commit()

    def set_runtime_pending(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, value, iso()),
        )

    def clear_runtime(self, key: str) -> None:
        self.conn.execute("DELETE FROM runtime_state WHERE key = ?", (key,))
        self.conn.commit()

    def acquire_poll_lock(self, ttl_seconds: int) -> bool:
        now = utc_now()
        expires_at = now + timedelta(seconds=max(15, ttl_seconds))
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                row = self.conn.execute(
                    "SELECT value FROM runtime_state WHERE key = 'poll_lock'"
                ).fetchone()
                locked_until = parse_iso(row["value"]) if row else None
                if locked_until and locked_until > now:
                    self.conn.rollback()
                    return False
                self.conn.execute(
                    """
                    INSERT INTO runtime_state(key, value, updated_at)
                    VALUES ('poll_lock', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                      value = excluded.value,
                      updated_at = excluded.updated_at
                    """,
                    (iso(expires_at), iso(now)),
                )
                self.conn.commit()
                return True
            except Exception:
                self.conn.rollback()
                raise

    def release_poll_lock(self) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM runtime_state WHERE key = 'poll_lock'")
            self.conn.commit()

    def auto_join_enabled(self) -> bool:
        override = self.get_runtime("auto_join_enabled")
        if override:
            return override == "1"
        return bool(self.config.get("minecraft", {}).get("auto_join_enabled", False))

    def set_auto_join_enabled(self, enabled: bool) -> None:
        self.set_runtime("auto_join_enabled", "1" if enabled else "0")

    def source_inference_key(self, name: str, field: str) -> str:
        return f"source_inference:{name}:{field}"

    def source_inference_anchor(self, name: str) -> int | None:
        raw = self.get_runtime(self.source_inference_key(name, "anchor_count"))
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def source_inference_credit(self, name: str) -> int:
        raw = self.get_runtime(self.source_inference_key(name, "credit"), "0")
        try:
            return max(0, int(raw))
        except ValueError:
            return 0

    def set_source_inference_credit(self, name: str, credit: int) -> None:
        self.set_runtime_pending(
            self.source_inference_key(name, "credit"), str(max(0, int(credit)))
        )

    def reset_count_source_inference_baselines(self) -> None:
        for row in self.source_rows():
            if row["type"] != "count" or row["last_count"] is None:
                continue
            name = str(row["name"])
            self.set_runtime_pending(
                self.source_inference_key(name, "anchor_count"),
                str(int(row["last_count"])),
            )
            self.set_runtime_pending(self.source_inference_key(name, "credit"), "0")

    def poll_interval_override_seconds(self) -> int | None:
        raw = self.get_runtime("poll_interval_override_seconds")
        if not raw:
            return None
        try:
            seconds = int(raw)
        except ValueError:
            return None
        return seconds if seconds > 0 else None

    def set_poll_interval_override_seconds(self, seconds: int) -> None:
        self.set_runtime("poll_interval_override_seconds", str(max(1, int(seconds))))

    def clear_poll_interval_override_seconds(self) -> None:
        self.clear_runtime("poll_interval_override_seconds")

    def effective_poll_interval_seconds(self, estimate: int) -> int:
        override = self.poll_interval_override_seconds()
        if override is not None:
            return override
        return poll_interval(self.config, estimate)

    def set_next_poll_due_at(self, due_at: datetime) -> None:
        self.set_runtime("next_poll_due_at", iso(due_at))

    def next_poll_due_at(self) -> datetime | None:
        return parse_iso(self.get_runtime("next_poll_due_at"))

    def insert_join_event(
        self,
        reason: str,
        estimate: int,
        confidence: str,
        joined_successfully: bool,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO join_events(
              triggered_at, reason, vp_estimate_at_trigger,
              confidence, joined_successfully
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (iso(), reason, estimate, confidence, 1 if joined_successfully else 0),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def mark_join_reward(self, join_event_id: int) -> None:
        self.conn.execute(
            "UPDATE join_events SET reward_detected = 1 WHERE id = ?",
            (join_event_id,),
        )
        self.conn.commit()

    def mark_join_disconnected(self, join_event_id: int) -> None:
        self.conn.execute(
            "UPDATE join_events SET disconnected_at = ? WHERE id = ?",
            (iso(), join_event_id),
        )
        self.conn.commit()

    def recent_vote_velocity_per_minute(self, minutes: int = 30) -> float:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT started_at, ended_at, total_delta
            FROM poll_cycles
            WHERE ended_at >= ?
            ORDER BY ended_at ASC
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return 0.0
        started = parse_iso(rows[0]["started_at"])
        ended = parse_iso(rows[-1]["ended_at"])
        if not started or not ended:
            return 0.0
        elapsed = max((ended - started).total_seconds() / 60.0, 1.0)
        total = sum(int(row["total_delta"]) for row in rows)
        return total / elapsed

    def velocity_windows(self, remaining: int) -> list[VelocityWindow]:
        windows = [5, 15, 30, 60, 180, 360, 720, 1440]
        results: list[VelocityWindow] = []
        for minutes in windows:
            votes = self.votes_in_window(minutes)
            velocity = self.recent_vote_velocity_per_minute(minutes)
            eta_seconds = eta_from_velocity(remaining, velocity)
            results.append(
                VelocityWindow(
                    minutes=minutes,
                    votes=votes,
                    velocity_per_minute=velocity,
                    eta_seconds=eta_seconds,
                )
            )
        return results

    def latest_snapshots(self, limit: int = 12) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT source_name, count, raw_summary, checked_at, success, error
                FROM vote_snapshots
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )

    def source_rows(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT name, url, type, parser, enabled, last_count, last_checked_at,
                       recent_parser, fetcher, fallback_fetcher, date_timezone,
                       failure_count, next_allowed_at
                FROM vote_sources
                ORDER BY id
                """
            ).fetchall()
        )

    def recent_cycles(self, limit: int = 240) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT id, started_at, ended_at, estimate_before, total_delta,
                       successes, failures, confidence, estimate_after,
                       vote_parties_crossed
                FROM poll_cycles
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )

    def recent_source_results(self, limit: int = 600) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT source_name, checked_at, success, skipped, count, delta,
                       raw_delta, suppressed_delta, new_events, reset_detected,
                       fetcher_used, adjustment_note, error, raw_summary
                FROM source_poll_results
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )

    def vote_party_events(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT estimated_at, estimate_before, estimate_after, delta,
                       confidence, source_count, reason
                FROM vote_party_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )

    def source_stats(self, minutes: int = 1440) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT
              source_name,
              COUNT(*) AS polls,
              SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS ok_polls,
              SUM(CASE WHEN skipped = 1 THEN 1 ELSE 0 END) AS skipped_polls,
              SUM(CASE WHEN success = 0 AND skipped = 0 THEN 1 ELSE 0 END) AS failed_polls,
              SUM(CASE WHEN fetcher_used = 'reader' THEN 1 ELSE 0 END) AS reader_polls,
              COALESCE(SUM(delta), 0) AS votes,
              COALESCE(MAX(delta), 0) AS max_delta,
              COALESCE(SUM(reset_detected), 0) AS resets,
              MAX(checked_at) AS last_seen
            FROM source_poll_results
            WHERE checked_at >= ?
            GROUP BY source_name
            ORDER BY votes DESC, source_name ASC
            """,
            (cutoff,),
        ).fetchall()
        stats = []
        for row in rows:
            polls = int(row["polls"] or 0)
            ok = int(row["ok_polls"] or 0)
            stats.append(
                {
                    "source": row["source_name"],
                    "polls": polls,
                    "ok_polls": ok,
                    "skipped_polls": int(row["skipped_polls"] or 0),
                    "failed_polls": int(row["failed_polls"] or 0),
                    "reader_polls": int(row["reader_polls"] or 0),
                    "success_rate": ok / polls if polls else 0.0,
                    "votes": int(row["votes"] or 0),
                    "max_delta": int(row["max_delta"] or 0),
                    "resets": int(row["resets"] or 0),
                    "last_seen": row["last_seen"],
                }
            )
        return stats

    def source_call_diagnostics(self, minutes: int = 10080) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            WITH cycle_positive AS (
              SELECT poll_cycle_id,
                     SUM(CASE WHEN delta > 0 THEN 1 ELSE 0 END) AS positive_sources
              FROM source_poll_results
              WHERE checked_at >= ?
              GROUP BY poll_cycle_id
            )
            SELECT
              spr.source_name,
              COUNT(*) AS polls,
              SUM(CASE WHEN spr.success = 1 THEN 1 ELSE 0 END) AS ok_polls,
              SUM(CASE WHEN spr.success = 0 AND spr.skipped = 0 THEN 1 ELSE 0 END) AS failed_polls,
              SUM(CASE WHEN spr.skipped = 1 THEN 1 ELSE 0 END) AS skipped_polls,
              SUM(CASE WHEN spr.fetcher_used = 'reader' THEN 1 ELSE 0 END) AS reader_polls,
              COALESCE(SUM(spr.delta), 0) AS votes,
              COALESCE(SUM(CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END), 0) AS raw_votes,
              COALESCE(MAX(CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END), 0) AS max_delta,
              COALESCE(SUM(COALESCE(spr.suppressed_delta, 0)), 0) AS suppressed_votes,
              SUM(CASE WHEN spr.delta > 0 THEN 1 ELSE 0 END) AS positive_polls,
              SUM(CASE WHEN (CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END) > 1 THEN 1 ELSE 0 END) AS catchup_polls,
              COALESCE(SUM(CASE WHEN (CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END) > 1 THEN (CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END) ELSE 0 END), 0) AS catchup_votes,
              SUM(CASE WHEN spr.delta > 0 AND cycle_positive.positive_sources = 1 THEN 1 ELSE 0 END) AS solo_polls,
              COALESCE(SUM(CASE WHEN spr.delta > 0 AND cycle_positive.positive_sources = 1 THEN spr.delta ELSE 0 END), 0) AS solo_votes,
              MAX(CASE WHEN spr.success = 1 THEN spr.checked_at ELSE NULL END) AS last_success_at,
              MAX(CASE WHEN spr.success = 0 AND spr.skipped = 0 THEN spr.checked_at ELSE NULL END) AS last_failure_at,
              MAX(CASE WHEN spr.skipped = 1 THEN spr.checked_at ELSE NULL END) AS last_skip_at
            FROM source_poll_results spr
            LEFT JOIN cycle_positive ON cycle_positive.poll_cycle_id = spr.poll_cycle_id
            WHERE spr.checked_at >= ?
            GROUP BY spr.source_name
            ORDER BY failed_polls + skipped_polls DESC, spr.source_name ASC
            """,
            (cutoff, cutoff),
        ).fetchall()
        latest_errors = {}
        for row in self.conn.execute(
            """
            SELECT source_name, checked_at, skipped, error
            FROM source_poll_results
            WHERE checked_at >= ?
              AND success = 0
            ORDER BY id DESC
            """,
            (cutoff,),
        ).fetchall():
            latest_errors.setdefault(row["source_name"], row)
        now = utc_now()
        diagnostics = []
        for row in rows:
            polls = int(row["polls"] or 0)
            ok = int(row["ok_polls"] or 0)
            failed = int(row["failed_polls"] or 0)
            skipped = int(row["skipped_polls"] or 0)
            positive = int(row["positive_polls"] or 0)
            solo_polls = int(row["solo_polls"] or 0)
            last_success = parse_iso(row["last_success_at"])
            stale_seconds = (
                (now - last_success).total_seconds() if last_success else None
            )
            latest_error = latest_errors.get(row["source_name"])
            health_score = 100.0
            if polls:
                health_score -= ((failed + skipped) / polls) * 45.0
            health_score -= min(float(row["catchup_polls"] or 0) * 3.0, 20.0)
            health_score -= min(solo_polls * 1.5, 15.0)
            if stale_seconds and stale_seconds > 900:
                health_score -= min((stale_seconds - 900) / 60.0, 20.0)
            diagnostics.append(
                {
                    "source": row["source_name"],
                    "polls": polls,
                    "ok_polls": ok,
                    "failed_polls": failed,
                    "skipped_polls": skipped,
                    "reader_polls": int(row["reader_polls"] or 0),
                    "success_rate": ok / polls if polls else 0.0,
                    "failure_rate": (failed + skipped) / polls if polls else 0.0,
                    "votes": int(row["votes"] or 0),
                    "raw_votes": int(row["raw_votes"] or 0),
                    "suppressed_votes": int(row["suppressed_votes"] or 0),
                    "max_delta": int(row["max_delta"] or 0),
                    "positive_polls": positive,
                    "solo_polls": solo_polls,
                    "solo_votes": int(row["solo_votes"] or 0),
                    "catchup_polls": int(row["catchup_polls"] or 0),
                    "catchup_votes": int(row["catchup_votes"] or 0),
                    "last_success_at": row["last_success_at"],
                    "last_failure_at": row["last_failure_at"],
                    "last_skip_at": row["last_skip_at"],
                    "stale_seconds": stale_seconds,
                    "latest_error_at": latest_error["checked_at"] if latest_error else None,
                    "latest_error": latest_error["error"] if latest_error else "",
                    "health_score": max(0.0, min(100.0, health_score)),
                }
            )
        return diagnostics

    def source_issue_events(self, minutes: int = 10080, limit: int = 40) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT source_name, checked_at, skipped, error
            FROM source_poll_results
            WHERE checked_at >= ?
              AND success = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [
            {
                "source": row["source_name"],
                "checked_at": row["checked_at"],
                "checked_at_local": local_time_label(row["checked_at"]),
                "skipped": bool(row["skipped"]),
                "error": row["error"] or "",
            }
            for row in rows
        ]

    def source_issue_count(self, minutes: int = 10080) -> int:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS issue_count
            FROM source_poll_results
            WHERE checked_at >= ?
              AND success = 0
            """,
            (cutoff,),
        ).fetchone()
        return int(row["issue_count"] or 0)

    def source_delta_trace(self, minutes: int = 10080, limit: int = 80) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT pc.id, pc.ended_at, pc.estimate_before, pc.total_delta,
                   pc.estimate_after,
                   SUM(CASE WHEN spr.delta > 0 THEN 1 ELSE 0 END) AS positive_sources,
                   GROUP_CONCAT(
                     CASE
                       WHEN (CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END) > 0
                         THEN spr.source_name || ':' || spr.delta ||
                              CASE
                                WHEN COALESCE(spr.suppressed_delta, 0) > 0
                                  THEN ' raw' || (CASE WHEN COALESCE(spr.raw_delta, 0) > 0 THEN spr.raw_delta ELSE spr.delta END)
                                ELSE ''
                              END
                     END,
                     ', '
                   ) AS positive_detail
            FROM poll_cycles pc
            JOIN source_poll_results spr ON spr.poll_cycle_id = pc.id
            WHERE pc.ended_at >= ?
              AND pc.total_delta > 0
            GROUP BY pc.id
            ORDER BY pc.id DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        trace = []
        for row in rows:
            positive_sources = int(row["positive_sources"] or 0)
            total_delta = int(row["total_delta"] or 0)
            raw_detail = row["positive_detail"] or ""
            if " raw" in raw_detail:
                note = "capped burst"
            elif positive_sources <= 1 and total_delta > 1:
                note = "single-source burst"
            elif positive_sources <= 1:
                note = "single source"
            elif total_delta > positive_sources:
                note = "batched or paired"
            else:
                note = "multi-source"
            trace.append(
                {
                    "cycle_id": row["id"],
                    "ended_at": row["ended_at"],
                    "ended_at_local": local_time_label(row["ended_at"]),
                    "estimate_before": row["estimate_before"],
                    "total_delta": total_delta,
                    "estimate_after": row["estimate_after"],
                    "positive_sources": positive_sources,
                    "positive_detail": raw_detail,
                    "note": note,
                }
            )
        return trace

    def latest_voters(self, limit: int = 30) -> list[dict[str, Any]]:
        future_cutoff = iso(utc_now() + timedelta(minutes=5))
        rows = self.conn.execute(
            """
            SELECT source_name, username, vote_time, vote_time_utc, detected_at,
                   CASE
                     WHEN vote_time_utc IS NOT NULL AND vote_time_utc <= ? THEN vote_time_utc
                     ELSE detected_at
                   END AS sort_time
            FROM vote_events
            WHERE username IS NOT NULL AND username != ''
            ORDER BY sort_time DESC, id DESC
            LIMIT ?
            """,
            (future_cutoff, limit),
        ).fetchall()
        voters = []
        for row in rows:
            event_time = row["vote_time_utc"] or row["detected_at"]
            voters.append(
                {
                    "username": row["username"],
                    "source": row["source_name"],
                    "vote_time_text": row["vote_time"],
                    "vote_time_utc": row["vote_time_utc"],
                    "vote_time_local": local_time_label(event_time),
                    "detected_at": row["detected_at"],
                    "detected_at_local": local_time_label(row["detected_at"]),
                }
            )
        return voters

    def voter_stats(self, minutes: int = 1440, limit: int = 20) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        future_cutoff = iso(utc_now() + timedelta(minutes=5))
        rows = self.conn.execute(
            """
            SELECT username,
                   COUNT(*) AS votes_seen,
                   COUNT(DISTINCT source_name) AS sources_seen,
                   MAX(sort_time) AS last_vote_time,
                   GROUP_CONCAT(DISTINCT source_name) AS sources
            FROM (
              SELECT username, source_name,
                     CASE
                       WHEN vote_time_utc IS NOT NULL AND vote_time_utc <= ? THEN vote_time_utc
                       ELSE detected_at
                     END AS sort_time
              FROM vote_events
              WHERE username IS NOT NULL
                AND username != ''
            )
            WHERE sort_time >= ?
            GROUP BY username
            ORDER BY votes_seen DESC, last_vote_time DESC, username ASC
            LIMIT ?
            """,
            (future_cutoff, cutoff, limit),
        ).fetchall()
        stats = []
        for row in rows:
            stats.append(
                {
                    "username": row["username"],
                    "votes_seen": int(row["votes_seen"] or 0),
                    "sources_seen": int(row["sources_seen"] or 0),
                    "last_vote_time": row["last_vote_time"],
                    "last_vote_time_local": local_time_label(row["last_vote_time"]),
                    "sources": (row["sources"] or "").split(",") if row["sources"] else [],
                }
            )
        return stats

    def hourly_vote_pattern(self, hours: int = 168) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(hours=hours))
        rows = self.conn.execute(
            """
            SELECT strftime('%H', ended_at) AS hour_utc,
                   COALESCE(SUM(total_delta), 0) AS votes,
                   COUNT(*) AS polls
            FROM poll_cycles
            WHERE ended_at >= ?
            GROUP BY hour_utc
            ORDER BY hour_utc
            """,
            (cutoff,),
        ).fetchall()
        by_hour = {int(row["hour_utc"]): row for row in rows if row["hour_utc"] is not None}
        pattern = []
        for hour in range(24):
            row = by_hour.get(hour)
            pattern.append(
                {
                    "hour_utc": hour,
                    "votes": int(row["votes"] or 0) if row else 0,
                    "polls": int(row["polls"] or 0) if row else 0,
                }
            )
        return pattern

    def party_interval_stats(self) -> dict[str, Any]:
        rows = list(
            reversed(
                self.vote_party_events(200)
            )
        )
        intervals: list[float] = []
        previous: datetime | None = None
        for row in rows:
            current = parse_iso(row["estimated_at"])
            if current and previous:
                intervals.append((current - previous).total_seconds())
            if current:
                previous = current
        return summarize_seconds(intervals)

    def estimate_error_stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            """
            SELECT calibrated_at, previous_estimate, actual_estimate, signed_error,
                   absolute_error, severity, message,
                   minutes_since_last_calibration, confidence_before,
                   calibration_source
            FROM estimate_errors
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
        absolute_errors = [float(row["absolute_error"] or 0) for row in rows]
        drift_samples = []
        for row in rows:
            age = row["minutes_since_last_calibration"]
            if age and float(age) > 0:
                drift_samples.append(float(row["absolute_error"] or 0) / float(age))
        latest = row_to_dict(rows[0]) if rows else None
        return {
            "latest": latest,
            "absolute": summarize_numbers(absolute_errors),
            "average_error": sum(absolute_errors) / len(absolute_errors)
            if absolute_errors
            else None,
            "worst_error": max(absolute_errors) if absolute_errors else None,
            "drift_per_hour": (sum(drift_samples) / len(drift_samples) * 60.0)
            if drift_samples
            else None,
        }

    def calibration_mismatch_log(self, limit: int = 80) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT calibrated_at, previous_estimate, actual_estimate, signed_error,
                   absolute_error, severity, message, confidence_before,
                   calibration_source
            FROM estimate_errors
            WHERE absolute_error > 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                **row_to_dict(row),
                "calibrated_at_local": local_time_label(row["calibrated_at"]),
            }
            for row in rows
        ]

    def source_delay_stats(self, minutes: int = 10080) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT source_name, vote_time_utc, detected_at
            FROM vote_events
            WHERE vote_time_utc IS NOT NULL
              AND detected_at >= ?
            """,
            (cutoff,),
        ).fetchall()
        by_source: dict[str, list[float]] = {}
        for row in rows:
            voted = parse_iso(row["vote_time_utc"])
            detected = parse_iso(row["detected_at"])
            if voted and detected:
                by_source.setdefault(row["source_name"], []).append(
                    max(0.0, (detected - voted).total_seconds())
                )
        results = []
        for source, delays in sorted(by_source.items()):
            summary = summarize_seconds(delays)
            results.append(
                {
                    "source": source,
                    "samples": len(delays),
                    "median_delay_seconds": summary["median"],
                    "average_delay_seconds": summary["mean"],
                    "worst_delay_seconds": summary["max"],
                }
            )
        return results

    def downtime_stats(self, expected_interval_seconds: int) -> dict[str, Any]:
        rows = list(reversed(self.recent_cycles(500)))
        gaps: list[float] = []
        previous: datetime | None = None
        threshold = max(expected_interval_seconds * 2, expected_interval_seconds + 60)
        for row in rows:
            ended = parse_iso(row["ended_at"])
            if ended and previous:
                gap = (ended - previous).total_seconds()
                if gap > threshold:
                    gaps.append(gap)
            if ended:
                previous = ended
        velocity = self.recent_vote_velocity_per_minute(1440)
        missed_estimate = sum(gaps) / 60.0 * velocity
        return {
            "gap_count": len(gaps),
            "longest_gap_seconds": max(gaps) if gaps else 0,
            "total_gap_seconds": sum(gaps),
            "missed_vote_estimate": missed_estimate,
        }

    def voter_overlap_stats(self, minutes: int = 10080) -> dict[str, Any]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT username,
                   COUNT(*) AS votes_seen,
                   COUNT(DISTINCT source_name) AS sources_seen,
                   GROUP_CONCAT(DISTINCT source_name) AS sources,
                   MAX(COALESCE(vote_time_utc, detected_at)) AS last_seen
            FROM vote_events
            WHERE username IS NOT NULL
              AND username != ''
              AND COALESCE(vote_time_utc, detected_at) >= ?
            GROUP BY username
            HAVING sources_seen > 1
            ORDER BY sources_seen DESC, votes_seen DESC, last_seen DESC
            LIMIT 30
            """,
            (cutoff,),
        ).fetchall()
        enabled_sources = max(1, len(self.get_sources()))
        likely_threshold = min(enabled_sources, max(3, enabled_sources - 1))
        users = []
        likely_full_site = []
        for row in rows:
            payload = {
                "username": row["username"],
                "votes_seen": int(row["votes_seen"] or 0),
                "sources_seen": int(row["sources_seen"] or 0),
                "source_share": int(row["sources_seen"] or 0) / enabled_sources,
                "sources": (row["sources"] or "").split(",") if row["sources"] else [],
                "last_seen": row["last_seen"],
                "last_seen_local": local_time_label(row["last_seen"]),
            }
            users.append(payload)
            if payload["sources_seen"] >= likely_threshold:
                likely_full_site.append(payload)
        return {
            "overlap_users": users,
            "likely_full_site_voters": likely_full_site,
            "likely_threshold": likely_threshold,
        }

    def voter_streaks(self, minutes: int = 43200) -> list[dict[str, Any]]:
        cutoff = iso(utc_now() - timedelta(minutes=minutes))
        rows = self.conn.execute(
            """
            SELECT username,
                   COUNT(DISTINCT substr(COALESCE(vote_time_utc, detected_at), 1, 10)) AS active_days,
                   COUNT(*) AS votes_seen,
                   MAX(COALESCE(vote_time_utc, detected_at)) AS last_seen
            FROM vote_events
            WHERE username IS NOT NULL
              AND username != ''
              AND COALESCE(vote_time_utc, detected_at) >= ?
            GROUP BY username
            HAVING active_days > 1
            ORDER BY active_days DESC, votes_seen DESC, last_seen DESC
            LIMIT 20
            """,
            (cutoff,),
        ).fetchall()
        return [
            {
                "username": row["username"],
                "active_days": int(row["active_days"] or 0),
                "votes_seen": int(row["votes_seen"] or 0),
                "last_seen": row["last_seen"],
                "last_seen_local": local_time_label(row["last_seen"]),
            }
            for row in rows
        ]

    def dashboard_snapshot(self) -> DashboardSnapshot:
        state = self.get_state()
        estimate = int(state["current_estimate"])
        party_size = int(self.config["vp_party_size"])
        remaining = votes_remaining(estimate, party_size)
        windows = self.velocity_windows(remaining)
        cycles = self.recent_cycles(3000)
        source_results = self.recent_source_results(12000)
        party_events = self.vote_party_events(50)
        stats = build_stats_payload(self, remaining, windows, cycles, source_results)
        dynamic_interval = poll_interval(self.config, estimate)
        override_interval = self.poll_interval_override_seconds()
        effective_interval = self.effective_poll_interval_seconds(estimate)
        next_poll_due = self.next_poll_due_at()
        next_poll_seconds = (
            max(0, int(math.ceil((next_poll_due - utc_now()).total_seconds())))
            if next_poll_due
            else None
        )
        sources = []
        for row in self.source_rows():
            sources.append(
                {
                    "name": row["name"],
                    "type": row["type"],
                    "parser": row["parser"],
                    "recent_parser": row["recent_parser"],
                    "fetcher": row["fetcher"] or "http",
                    "fallback_fetcher": row["fallback_fetcher"] or "",
                    "date_timezone": row["date_timezone"] or "local",
                    "enabled": bool(row["enabled"]),
                    "last_count": row["last_count"],
                    "last_checked_at": row["last_checked_at"],
                    "failure_count": int(row["failure_count"] or 0),
                    "next_allowed_at": row["next_allowed_at"],
                }
            )
        history = {
            "cycles": [row_to_dict(row) for row in reversed(cycles)],
            "source_results": [row_to_dict(row) for row in reversed(source_results)],
            "vote_party_events": [row_to_dict(row) for row in reversed(party_events)],
            "hourly_pattern": self.hourly_vote_pattern(),
            "latest_voters": self.latest_voters(),
            "source_delta_trace": self.source_delta_trace(),
            "source_issue_events": self.source_issue_events(),
            "calibration_mismatch_log": self.calibration_mismatch_log(),
        }
        return DashboardSnapshot(
            generated_at=iso(),
            state={
                "estimate": estimate,
                "party_size": party_size,
                "remaining": remaining,
                "confidence": state["confidence"],
                "last_calibrated_at": state["last_calibrated_at"],
                "last_updated_at": state["last_updated_at"],
                "total_observed_votes": int(state["total_observed_votes"] or 0),
                "calibration_source": state["calibration_source"],
                "dynamic_poll_interval_seconds": dynamic_interval,
                "poll_interval_seconds": effective_interval,
                "poll_interval_override_seconds": override_interval,
                "next_poll_due_at": iso(next_poll_due) if next_poll_due else None,
                "next_poll_seconds": next_poll_seconds,
                "auto_join_enabled": self.auto_join_enabled(),
            },
            stats=stats,
            history=history,
            sources=sources,
        )


class Notifier:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    def notify(self, title: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {title}: {message}", flush=True)
        if self.config["alerts"].get("desktop_notifications", False):
            self.desktop(title, message)
        webhook_url = self.config["alerts"].get("discord_webhook_url", "")
        if webhook_url:
            self.discord(webhook_url, title, message)

    def desktop(self, title: str, message: str) -> None:
        if platform.system() != "Darwin":
            return
        script = (
            'display notification '
            f'{json.dumps(message)} '
            'with title '
            f'{json.dumps(title)}'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

    def discord(self, webhook_url: str, title: str, message: str) -> None:
        payload = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
        request = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": APP_NAME},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except Exception as exc:
            print(f"Discord webhook failed: {exc}", file=sys.stderr, flush=True)


class MinecraftController:
    def __init__(self, config: dict[str, Any], store: Store, notifier: Notifier) -> None:
        self.config = config
        self.store = store
        self.notifier = notifier

    @property
    def mode(self) -> str:
        return str(self.config["minecraft"].get("mode", "notify"))

    def maybe_act(self, confidence: str) -> list[str]:
        messages: list[str] = []
        state = self.store.get_state()
        estimate = int(state["current_estimate"])
        party_size = int(self.config["vp_party_size"])
        remaining = votes_remaining(estimate, party_size)
        velocity = self.store.recent_vote_velocity_per_minute()
        mc = self.config["minecraft"]

        self._reset_cycle_flags_if_safe(estimate)

        if estimate >= int(mc["prelaunch_threshold"]):
            if self.store.get_runtime("prelaunch_active") != "1":
                msg = f"Prelaunch threshold reached at {estimate}/{party_size}."
                self.notifier.notify("Vote Party prelaunch", msg)
                messages.append(msg)
                self._run_or_report("prelaunch", mc.get("prelaunch_command", ""))
                self.store.set_runtime("prelaunch_active", "1")

        join_reason = self._join_reason(estimate, remaining, confidence, velocity)
        if join_reason:
            if not self.store.auto_join_enabled():
                msg = (
                    f"Auto-join is OFF at {estimate}/{party_size}; notify-only. "
                    f"Reason: {join_reason}"
                )
                self.notifier.notify("Vote Party close", msg)
                messages.append(msg)
                return messages
            if confidence == "low":
                msg = (
                    f"Low confidence at {estimate}/{party_size}; notify-only instead of joining. "
                    f"Reason: {join_reason}"
                )
                self.notifier.notify("Vote Party close", msg)
                messages.append(msg)
                return messages
            if self.store.get_runtime("joined_active") == "1":
                return messages

            command_ok = self._run_or_report("join", mc.get("join_command", ""))
            joined_successfully = self.mode == "commands" and command_ok
            join_event_id = self.store.insert_join_event(
                join_reason, estimate, confidence, joined_successfully
            )
            self.store.set_runtime("joined_active", "1")
            self.store.set_runtime("joined_at", iso())
            self.store.set_runtime("join_event_id", str(join_event_id))
            msg = f"Join triggered at {estimate}/{party_size}: {join_reason}"
            self.notifier.notify("Vote Party join", msg)
            messages.append(msg)

        return messages

    def monitor_online_limits(self) -> list[str]:
        messages: list[str] = []
        if self.store.get_runtime("joined_active") != "1":
            return messages

        joined_at = parse_iso(self.store.get_runtime("joined_at"))
        if not joined_at:
            return messages

        elapsed = (utc_now() - joined_at).total_seconds()
        hard_limit = int(self.config["minecraft"]["hard_disconnect_seconds"])
        max_target = int(self.config["minecraft"]["max_online_seconds"])
        if elapsed >= hard_limit:
            msg = f"Hard online limit reached after {int(elapsed)} seconds."
            self.disconnect(msg)
            messages.append(msg)
        elif elapsed >= max_target and self.store.get_runtime("target_limit_warned") != "1":
            msg = f"Target online window exceeded after {int(elapsed)} seconds."
            self.notifier.notify("Vote Party online window", msg)
            self.store.set_runtime("target_limit_warned", "1")
            messages.append(msg)

        reward_at = parse_iso(self.store.get_runtime("reward_detected_at"))
        delay = int(self.config["minecraft"]["reward_disconnect_delay_seconds"])
        if reward_at and (utc_now() - reward_at).total_seconds() >= delay:
            msg = f"Reward pattern detected; disconnecting after {delay} second delay."
            self.disconnect(msg)
            messages.append(msg)

        return messages

    def handle_chat_vp_count(self, count: int, party_size: int) -> str | None:
        configured_party_size = int(self.config["vp_party_size"])
        if party_size != configured_party_size:
            return (
                f"Ignored chat VP count {count}/{party_size}; configured party size is "
                f"{configured_party_size}."
            )

        self.store.calibrate(count, source="minecraft_chat")
        if self.store.get_runtime("joined_active") == "1":
            disconnect_floor = int(self.config["minecraft"]["disconnect_if_chat_vp_below"])
            if count < disconnect_floor:
                msg = (
                    f"Chat confirmed VP-count {count}/{party_size}, below "
                    f"{disconnect_floor}; disconnecting."
                )
                self.disconnect(msg)
                return msg
        return f"Chat calibrated VP-count to {count}/{party_size}."

    def handle_reward_detected(self, line: str) -> str | None:
        if self.store.get_runtime("joined_active") != "1":
            return None
        if self.store.get_runtime("reward_detected_at"):
            return None
        self.store.set_runtime("reward_detected_at", iso())
        join_event_id = self._join_event_id()
        if join_event_id:
            self.store.mark_join_reward(join_event_id)
        msg = f"Reward-like chat line detected: {line[:160]}"
        self.notifier.notify("Vote Party reward", msg)
        return msg

    def disconnect(self, reason: str) -> bool:
        command_ok = self._run_or_report("disconnect", self.config["minecraft"].get("disconnect_command", ""))
        join_event_id = self._join_event_id()
        if join_event_id:
            self.store.mark_join_disconnected(join_event_id)
        self.store.clear_runtime("joined_active")
        self.store.clear_runtime("joined_at")
        self.store.clear_runtime("join_event_id")
        self.store.clear_runtime("target_limit_warned")
        self.store.clear_runtime("reward_detected_at")
        self.notifier.notify("Vote Party disconnect", reason)
        return command_ok

    def _join_reason(
        self, estimate: int, remaining: int, confidence: str, velocity_per_minute: float
    ) -> str:
        mc = self.config["minecraft"]
        high_threshold = int(mc["join_threshold_high_confidence"])
        medium_threshold = int(mc["join_threshold_medium_confidence"])
        if confidence == "high" and estimate >= high_threshold:
            return f"high confidence threshold {high_threshold}/120 reached"
        if confidence == "medium" and estimate >= medium_threshold:
            return f"medium confidence threshold {medium_threshold}/120 reached"

        estimated_join_seconds = float(mc["estimated_join_seconds"])
        safety_buffer = float(mc["join_safety_buffer_votes"])
        expected_during_join = (velocity_per_minute / 60.0) * estimated_join_seconds
        if remaining <= math.ceil(expected_during_join + safety_buffer):
            return (
                "dynamic velocity threshold reached "
                f"(remaining={remaining}, velocity={velocity_per_minute:.2f}/min)"
            )
        return ""

    def _reset_cycle_flags_if_safe(self, estimate: int) -> None:
        near_threshold = int(self.config["polling"]["near_threshold"])
        if estimate >= near_threshold:
            return
        for key in (
            "prelaunch_active",
            "joined_active",
            "joined_at",
            "join_event_id",
            "target_limit_warned",
            "reward_detected_at",
        ):
            self.store.clear_runtime(key)

    def _run_or_report(self, action: str, command: str) -> bool:
        if self.mode != "commands":
            if command:
                self.notifier.notify(
                    f"Minecraft {action}",
                    f"Dry run: would execute {command!r}",
                )
            else:
                self.notifier.notify(
                    f"Minecraft {action}",
                    "Dry run: no command configured.",
                )
            return False

        if not command:
            self.notifier.notify(
                f"Minecraft {action}",
                "Command mode is enabled, but no command is configured.",
            )
            return False

        env = os.environ.copy()
        state = self.store.get_state()
        party_size = int(self.config["vp_party_size"])
        estimate = int(state["current_estimate"])
        env.update(
            {
                "VP_ESTIMATE": str(estimate),
                "VP_PARTY_SIZE": str(party_size),
                "VP_REMAINING": str(votes_remaining(estimate, party_size)),
                "VP_CONFIDENCE": str(state["confidence"]),
            }
        )
        try:
            completed = subprocess.run(
                command,
                shell=True,
                env=env,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                self.notifier.notify(
                    f"Minecraft {action}",
                    f"Command exited with {completed.returncode}: {command}",
                )
                return False
            return True
        except Exception as exc:
            self.notifier.notify(f"Minecraft {action}", f"Command failed: {exc}")
            return False

    def _join_event_id(self) -> int | None:
        raw = self.store.get_runtime("join_event_id")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None


class ChatLogTailer:
    def __init__(self, config: dict[str, Any], store: Store, controller: MinecraftController) -> None:
        self.config = config
        self.store = store
        self.controller = controller

    def poll(self) -> list[str]:
        path_raw = self.config["minecraft"].get("latest_log_path", "")
        if not path_raw:
            return []
        path = Path(os.path.expanduser(path_raw))
        if not path.exists():
            return []

        previous = int(self.store.get_runtime("log_offset", "0") or "0")
        size = path.stat().st_size
        if previous > size:
            previous = 0

        messages: list[str] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(previous)
            for line in fh:
                cleaned = strip_minecraft_log_line(line)
                chat_match = VP_CHAT_RE.search(cleaned)
                if chat_match:
                    count = int(chat_match.group(1))
                    party_size = int(chat_match.group(2))
                    message = self.controller.handle_chat_vp_count(count, party_size)
                    if message:
                        messages.append(message)
                reward_message = self._maybe_reward(cleaned)
                if reward_message:
                    messages.append(reward_message)
            new_offset = fh.tell()
        self.store.set_runtime("log_offset", str(new_offset))
        return messages

    def _maybe_reward(self, line: str) -> str | None:
        lowered = line.lower()
        for pattern in self.config["minecraft"].get("reward_detection_patterns", []):
            if str(pattern).lower() in lowered:
                return self.controller.handle_reward_detected(line)
        return None


def strip_minecraft_log_line(line: str) -> str:
    line = line.strip()
    # Example prefix: [20:31:33] [Render thread/INFO]: [CHAT] ...
    return re.sub(r"^\[[^\]]+\]\s+\[[^\]]+\]:\s*", "", line)


def poll_sources(store: Store, config: dict[str, Any]) -> PollCycleResult:
    started_at = utc_now()
    estimate_before = int(store.get_state()["current_estimate"])
    lock_ttl = int(config.get("estimation", {}).get("poll_lock_ttl_seconds", 120))
    if not store.acquire_poll_lock(lock_ttl):
        confidence = str(store.get_state()["confidence"])
        return PollCycleResult(
            started_at=started_at,
            ended_at=utc_now(),
            estimate_before=estimate_before,
            total_delta=0,
            successes=0,
            failures=0,
            count_successes=0,
            resets=0,
            large_jumps=0,
            confidence=confidence,
            estimate_after=estimate_before,
            vote_parties_crossed=0,
            source_results=[],
            log_events=["Skipped poll because another tracker process is already polling."],
        )
    results: list[PollSourceResult] = []
    successes = 0
    failures = 0
    count_successes = 0
    try:
        for source in store.get_sources():
            result = poll_single_source(store, config, source)
            results.append(result)
            if result.success:
                successes += 1
                if source["type"] == "count":
                    count_successes += 1
            elif result.skipped:
                pass
            else:
                failures += 1

        reconcile_inferred_source_deltas(store, config, results)
        apply_delta_safety(config, results)
        large_delta_warning = int(config["confidence"]["large_delta_warning"])
        resets = sum(1 for result in results if result.reset_detected)
        large_jumps = sum(1 for result in results if result.delta > large_delta_warning)
        total_delta = sum(result.delta for result in results)

        estimate_after = store.apply_vote_delta(total_delta)
        party_size = int(config["vp_party_size"])
        vote_parties_crossed = 0
        if total_delta > 0:
            vote_parties_crossed = (estimate_before + total_delta) // party_size
        confidence = compute_confidence(
            store=store,
            config=config,
            count_successes=count_successes,
            failures=failures,
            resets=resets,
            large_jumps=large_jumps,
        )
        store.set_confidence(confidence)
        ended_at = utc_now()

        cycle = PollCycleResult(
            started_at=started_at,
            ended_at=ended_at,
            estimate_before=estimate_before,
            total_delta=total_delta,
            successes=successes,
            failures=failures,
            count_successes=count_successes,
            resets=resets,
            large_jumps=large_jumps,
            confidence=confidence,
            estimate_after=estimate_after,
            vote_parties_crossed=vote_parties_crossed,
            source_results=results,
        )
        store.record_poll_cycle(cycle)
        return cycle
    finally:
        store.release_poll_lock()


def append_adjustment_note(result: PollSourceResult, note: str) -> None:
    if not note:
        return
    result.adjustment_note = (
        f"{result.adjustment_note}; {note}" if result.adjustment_note else note
    )


def reconcile_inferred_source_deltas(
    store: Store, config: dict[str, Any], results: list[PollSourceResult]
) -> None:
    estimation = config.get("estimation", {})
    if not estimation.get("stale_count_source_inference_enabled", True):
        return
    count_source_names = {
        str(row["name"])
        for row in store.source_rows()
        if row["type"] == "count" and bool(row["enabled"])
    }
    by_name = {
        result.name: result
        for result in results
        if result.name in count_source_names and result.success and result.count is not None
    }
    if len(by_name) < 2:
        return

    observed_since_anchor: dict[str, int] = {}
    for name, result in by_name.items():
        if result.raw_delta is None:
            result.raw_delta = result.delta
        credit = store.source_inference_credit(name)
        if result.delta > 0 and credit > 0:
            consumed = min(result.delta, credit)
            result.delta -= consumed
            credit -= consumed
            store.set_source_inference_credit(name, credit)
            append_adjustment_note(result, f"absorbed inferred credit {consumed}")
        anchor = store.source_inference_anchor(name)
        if anchor is None:
            store.set_runtime_pending(
                store.source_inference_key(name, "anchor_count"),
                str(int(result.count or 0)),
            )
            store.set_source_inference_credit(name, 0)
            continue
        observed_since_anchor[name] = max(0, int(result.count or 0) - anchor)

    min_peer_sources = int(estimation.get("stale_count_source_min_peer_sources", 3))
    min_peer_delta = int(estimation.get("stale_count_source_min_peer_delta", 2))
    max_inferred = int(estimation.get("stale_count_source_max_inferred_per_poll", 6))

    for name, result in by_name.items():
        if name not in observed_since_anchor:
            continue
        actual_seen = observed_since_anchor[name]
        current_credit = store.source_inference_credit(name)
        peer_values = [
            value
            for peer_name, value in observed_since_anchor.items()
            if peer_name != name and value > 0
        ]
        if len(peer_values) < min_peer_sources:
            continue
        target_seen = int(round(percentile([float(value) for value in peer_values], 0.5) or 0))
        if target_seen < min_peer_delta:
            continue
        already_counted = max(actual_seen, current_credit)
        inferred = min(max_inferred, max(0, target_seen - already_counted))
        if inferred <= 0:
            continue
        result.delta += inferred
        store.set_source_inference_credit(name, current_credit + inferred)
        append_adjustment_note(
            result,
            f"inferred stale count source +{inferred} from peer median {target_seen}",
        )


def apply_delta_safety(config: dict[str, Any], results: list[PollSourceResult]) -> None:
    for result in results:
        if result.raw_delta is None:
            result.raw_delta = result.delta
        result.suppressed_delta = 0

    positive = [result for result in results if result.success and result.delta > 0]
    if len(positive) != 1:
        return

    result = positive[0]
    estimation = config.get("estimation", {})
    default_limit = int(estimation.get("single_source_burst_limit", 3))
    reader_limit = int(estimation.get("reader_single_source_burst_limit", default_limit))
    limit = reader_limit if result.fetcher_used == "reader" else default_limit
    limit = max(0, limit)
    if result.delta <= limit:
        return

    raw_delta = result.delta
    result.delta = limit
    result.suppressed_delta = raw_delta - limit
    append_adjustment_note(result, f"capped single-source burst {raw_delta}->{limit}")


def poll_single_source(
    store: Store, config: dict[str, Any], source: sqlite3.Row
) -> PollSourceResult:
    name = str(source["name"])
    next_allowed = parse_iso(source["next_allowed_at"])
    if next_allowed and utc_now() < next_allowed:
        return PollSourceResult(
            name=name,
            success=False,
            skipped=True,
            error=f"backing off until {next_allowed.isoformat(timespec='seconds')}",
            fetcher_used=str(source_value(source, "fetcher", "http") or "http"),
        )

    try:
        html, fetcher_used = fetch_source_url(str(source["url"]), config, source)
        visible_text = html_to_visible_text(html)
        date_timezone = str(source_value(source, "date_timezone", "local") or "local")
        reference_dt = utc_now()
        if source["type"] == "count":
            parsed = parse_count_source(str(source["parser"]), visible_text, html)
            previous_count = source["last_count"]
            delta = 0
            reset_detected = False
            if previous_count is not None:
                previous = int(previous_count)
                if parsed.count is not None and parsed.count >= previous:
                    delta = parsed.count - previous
                elif parsed.count is not None:
                    reset_detected = True
                    delta = 0
            new_events = insert_recent_events_for_source(
                store,
                name,
                str(source["recent_parser"] or ""),
                visible_text,
                date_timezone,
                reference_dt,
            )
            store.insert_snapshot(name, parsed.count, parsed.raw_summary, True)
            store.update_source_success(name, parsed.count)
            store.conn.commit()
            return PollSourceResult(
                name=name,
                success=True,
                count=parsed.count,
                delta=delta,
                new_events=new_events,
                reset_detected=reset_detected,
                raw_summary=parsed.raw_summary,
                fetcher_used=fetcher_used,
            )

        parsed = parse_recent_source(
            name, str(source["parser"]), visible_text, date_timezone, reference_dt
        )
        is_baseline = source["last_checked_at"] is None
        new_events = 0
        for event in parsed.events:
            if store.insert_event(name, event):
                new_events += 1
        delta = 0 if is_baseline else new_events
        store.insert_snapshot(name, None, parsed.raw_summary, True)
        store.update_source_success(name, None)
        store.conn.commit()
        return PollSourceResult(
            name=name,
            success=True,
            delta=delta,
            new_events=new_events,
            raw_summary=parsed.raw_summary,
            fetcher_used=fetcher_used,
        )
    except Exception as exc:
        error = friendly_error(exc)
        store.insert_snapshot(name, None, "", False, error)
        store.update_source_failure(source)
        store.conn.commit()
        return PollSourceResult(
            name=name,
            success=False,
            error=error,
            fetcher_used=str(source_value(source, "fetcher", "http") or "http"),
        )


def insert_recent_events_for_source(
    store: Store,
    source_name: str,
    parser_name: str,
    visible_text: str,
    date_timezone: str = "local",
    reference_dt: datetime | None = None,
) -> int:
    if not parser_name:
        return 0
    parsed = parse_recent_source(
        source_name, parser_name, visible_text, date_timezone, reference_dt
    )
    new_events = 0
    for event in parsed.events:
        if store.insert_event(source_name, event):
            new_events += 1
    return new_events


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"URL error: {exc.reason}"
    return str(exc)


def compute_confidence(
    store: Store,
    config: dict[str, Any],
    count_successes: int,
    failures: int,
    resets: int,
    large_jumps: int,
) -> str:
    state = store.get_state()
    calibrated_at = parse_iso(state["last_calibrated_at"])
    if not calibrated_at:
        return "low"

    max_calibration_minutes = int(config["confidence"]["max_minutes_since_calibration"])
    age_minutes = (utc_now() - calibrated_at).total_seconds() / 60.0
    if age_minutes > max_calibration_minutes * 2:
        return "low"

    if resets or large_jumps:
        return "low" if failures else "medium"

    required = int(config["confidence"]["require_count_sources"])
    if (
        count_successes >= required
        and failures == 0
        and age_minutes <= max_calibration_minutes
    ):
        return "high"
    if count_successes >= max(2, required - 1) and age_minutes <= max_calibration_minutes * 2:
        return "medium"
    return "low"


def votes_remaining(estimate: int, party_size: int) -> int:
    return party_size - estimate if estimate > 0 else party_size


def circular_signed_error(predicted: int, actual: int, party_size: int) -> int:
    if party_size <= 0:
        return actual - predicted
    error = (actual - predicted) % party_size
    if error > party_size / 2:
        error -= party_size
    return int(error)


def calibration_error_severity(absolute_error: int, party_size: int) -> str:
    if absolute_error <= 0:
        return "exact"
    scaled_major = max(8, int(round(max(1, party_size) * 0.07)))
    scaled_critical = max(15, int(round(max(1, party_size) * 0.13)))
    if absolute_error == 1:
        return "trace"
    if absolute_error <= 3:
        return "minor"
    if absolute_error < scaled_major:
        return "moderate"
    if absolute_error < scaled_critical:
        return "major"
    return "critical"


def calibration_error_message(
    previous: int,
    actual: int,
    signed_error: int,
    severity: str,
    party_size: int,
) -> str:
    if signed_error == 0:
        return f"Exact calibration at {actual}/{party_size}."
    direction = "underestimated" if signed_error > 0 else "overestimated"
    return (
        f"{severity.title()} mismatch: estimate {previous}/{party_size} "
        f"{direction} by {abs(signed_error)}; actual was {actual}/{party_size}."
    )


def eta_from_velocity(remaining: int, velocity_per_minute: float) -> int | None:
    if remaining <= 0:
        return 0
    if velocity_per_minute <= 0:
        return None
    return int(math.ceil((remaining / velocity_per_minute) * 60.0))


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_numbers(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "p90": None,
            "stdev": None,
        }
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "median": percentile(values, 0.5),
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.9),
        "stdev": math.sqrt(variance),
    }


def summarize_seconds(values: list[float]) -> dict[str, Any]:
    summary = summarize_numbers(values)
    return {
        **summary,
        "mean_minutes": summary["mean"] / 60.0 if summary["mean"] is not None else None,
        "median_minutes": summary["median"] / 60.0
        if summary["median"] is not None
        else None,
    }


def current_no_vote_streak(cycles_desc: list[sqlite3.Row]) -> int:
    streak = 0
    for row in cycles_desc:
        if int(row["total_delta"] or 0) > 0:
            break
        streak += 1
    return streak


def source_concentration(source_stats: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(int(row["votes"]) for row in source_stats)
    if total <= 0:
        return {"hhi": 0.0, "top_source": None, "top_share": 0.0}
    top = max(source_stats, key=lambda row: int(row["votes"]))
    shares = [int(row["votes"]) / total for row in source_stats]
    return {
        "hhi": sum(share * share for share in shares),
        "top_source": top["source"],
        "top_share": int(top["votes"]) / total,
    }


def burst_stats(cycles_chrono: list[sqlite3.Row]) -> dict[str, Any]:
    burst_threshold = 3
    bursts = [
        row
        for row in cycles_chrono
        if int(row["total_delta"] or 0) >= burst_threshold
    ]
    if not cycles_chrono:
        return {
            "threshold_votes": burst_threshold,
            "count_loaded": 0,
            "max_burst_size": 0,
            "average_burst_size": None,
            "frequency_per_day": 0.0,
            "recent_bursts": [],
        }
    started = parse_iso(cycles_chrono[0]["ended_at"])
    ended = parse_iso(cycles_chrono[-1]["ended_at"])
    days = max(((ended - started).total_seconds() / 86400.0) if started and ended else 1.0, 1.0)
    sizes = [float(row["total_delta"] or 0) for row in bursts]
    return {
        "threshold_votes": burst_threshold,
        "count_loaded": len(bursts),
        "max_burst_size": int(max(sizes)) if sizes else 0,
        "average_burst_size": sum(sizes) / len(sizes) if sizes else None,
        "frequency_per_day": len(bursts) / days,
        "recent_bursts": [
            {
                "ended_at": row["ended_at"],
                "ended_at_local": local_time_label(row["ended_at"]),
                "votes": int(row["total_delta"] or 0),
                "estimate_after": int(row["estimate_after"] or 0),
            }
            for row in bursts[-20:]
        ],
    }


def hourly_extremes(hourly_pattern: list[dict[str, Any]]) -> dict[str, Any]:
    peak = sorted(hourly_pattern, key=lambda row: row["votes"], reverse=True)[:5]
    quiet_candidates = [row for row in hourly_pattern if row["polls"] > 0]
    quiet = sorted(quiet_candidates, key=lambda row: row["votes"])[:5]
    return {"peak_hours_utc": peak, "quiet_hours_utc": quiet}


def hourly_party_probability(party_events: list[sqlite3.Row]) -> list[dict[str, Any]]:
    counts = {hour: 0 for hour in range(24)}
    total = 0
    for row in party_events:
        event_time = parse_iso(row["estimated_at"])
        if not event_time:
            continue
        hour = event_time.astimezone(timezone.utc).hour
        counts[hour] += 1
        total += 1
    return [
        {
            "hour_utc": hour,
            "events": counts[hour],
            "probability": counts[hour] / total if total else 0.0,
        }
        for hour in range(24)
    ]


def trusted_source_ranking(
    source_stats: list[dict[str, Any]],
    source_delays: list[dict[str, Any]],
    sources: list[sqlite3.Row],
) -> list[dict[str, Any]]:
    delay_by_source = {row["source"]: row for row in source_delays}
    source_meta = {row["name"]: row for row in sources}
    ranked = []
    now = utc_now()
    for row in source_stats:
        source = row["source"]
        meta = source_meta.get(source)
        last_checked = parse_iso(meta["last_checked_at"] if meta else None)
        freshness_seconds = (now - last_checked).total_seconds() if last_checked else None
        freshness_score = 1.0 if freshness_seconds is not None and freshness_seconds < 300 else 0.5
        delay = delay_by_source.get(source, {})
        median_delay = delay.get("median_delay_seconds")
        delay_score = 1.0
        if median_delay is not None:
            delay_score = max(0.0, 1.0 - min(float(median_delay), 3600.0) / 3600.0)
        score = (
            float(row["success_rate"]) * 0.55
            + freshness_score * 0.25
            + delay_score * 0.20
        )
        ranked.append(
            {
                "source": source,
                "score": score,
                "success_rate": row["success_rate"],
                "freshness_seconds": freshness_seconds,
                "median_delay_seconds": median_delay,
                "votes": row["votes"],
            }
        )
    return sorted(ranked, key=lambda row: row["score"], reverse=True)


def readiness_phase(config: dict[str, Any], estimate: int, confidence: str) -> dict[str, Any]:
    polling = config["polling"]
    minecraft = config["minecraft"]
    party_size = int(config["vp_party_size"])
    if estimate >= int(minecraft["join_threshold_high_confidence"]) and confidence == "high":
        phase = "join-zone"
        action = "join eligible"
    elif estimate >= int(minecraft["join_threshold_medium_confidence"]) and confidence == "medium":
        phase = "join-zone"
        action = "join eligible"
    elif estimate >= int(polling["trigger_threshold"]):
        phase = "trigger-watch"
        action = "poll tightly"
    elif estimate >= int(minecraft["prelaunch_threshold"]):
        phase = "prelaunch"
        action = "prepare client"
    elif estimate >= int(polling["near_threshold"]):
        phase = "armed"
        action = "increase polling"
    else:
        phase = "idle"
        action = "observe"
    return {
        "phase": phase,
        "action": action,
        "progress": estimate / party_size if party_size else 0.0,
    }


def data_quality_score(
    state: sqlite3.Row,
    source_stats: list[dict[str, Any]],
    failure_rate: float,
) -> dict[str, Any]:
    score = 100.0
    reasons = []
    if state["confidence"] == "medium":
        score -= 18
        reasons.append("medium confidence")
    elif state["confidence"] == "low":
        score -= 38
        reasons.append("low confidence")
    calibrated_at = parse_iso(state["last_calibrated_at"])
    if not calibrated_at:
        score -= 24
        reasons.append("not calibrated")
    else:
        age_minutes = (utc_now() - calibrated_at).total_seconds() / 60.0
        if age_minutes > 180:
            score -= 18
            reasons.append("stale calibration")
        elif age_minutes > 60:
            score -= 8
            reasons.append("aging calibration")
    if failure_rate > 0.25:
        score -= 20
        reasons.append("poll failures")
    elif failure_rate > 0.05:
        score -= 8
        reasons.append("some poll failures")
    active_sources = sum(1 for row in source_stats if int(row["ok_polls"]) > 0)
    if active_sources < 2:
        score -= 20
        reasons.append("few active sources")
    elif active_sources < 4:
        score -= 8
        reasons.append("limited active sources")
    score = max(0.0, min(100.0, score))
    return {"score": score, "reasons": reasons}


def build_stats_payload(
    store: Store,
    remaining: int,
    windows: list[VelocityWindow],
    cycles_desc: list[sqlite3.Row],
    source_results_desc: list[sqlite3.Row],
) -> dict[str, Any]:
    cycles_chrono = list(reversed(cycles_desc))
    deltas = [float(row["total_delta"] or 0) for row in cycles_chrono]
    positive_deltas = [value for value in deltas if value > 0]
    eta_values = [
        float(window.eta_seconds)
        for window in windows
        if window.eta_seconds is not None and window.velocity_per_minute > 0
    ]
    source_24h = store.source_stats(1440)
    source_7d = store.source_stats(10080)
    source_debug_24h = store.source_call_diagnostics(1440)
    source_debug_7d = store.source_call_diagnostics(10080)
    voters_24h = store.voter_stats(1440)
    voters_7d = store.voter_stats(10080)
    source_delays = store.source_delay_stats(10080)
    party_events = store.vote_party_events(500)
    hourly_pattern = store.hourly_vote_pattern()
    state = store.get_state()
    estimate = int(state["current_estimate"])
    party_size = int(store.config["vp_party_size"])
    total_polls = len(cycles_desc)
    total_votes = sum(int(row["total_delta"] or 0) for row in cycles_desc)
    failures = sum(int(row["failures"] or 0) for row in cycles_desc)
    successes = sum(int(row["successes"] or 0) for row in cycles_desc)
    poll_spacings: list[float] = []
    last_end: datetime | None = None
    for row in cycles_chrono:
        ended = parse_iso(row["ended_at"])
        if ended and last_end:
            poll_spacings.append((ended - last_end).total_seconds())
        if ended:
            last_end = ended
    window_payload = [
        {
            "minutes": window.minutes,
            "votes": window.votes,
            "velocity_per_minute": window.velocity_per_minute,
            "eta_seconds": window.eta_seconds,
        }
        for window in windows
    ]
    eta_summary = summarize_seconds(eta_values)
    delta_summary = summarize_numbers(deltas)
    positive_delta_summary = summarize_numbers(positive_deltas)
    poll_spacing_summary = summarize_seconds(poll_spacings)
    burstiness = None
    if delta_summary["mean"] and delta_summary["mean"] > 0:
        burstiness = delta_summary["stdev"] / delta_summary["mean"]
    by_window = {window.minutes: window for window in windows}
    v15 = by_window.get(15).velocity_per_minute if by_window.get(15) else 0.0
    v60 = by_window.get(60).velocity_per_minute if by_window.get(60) else 0.0
    acceleration = (v15 - v60) / max(v60, 0.01)
    hours = hourly_extremes(hourly_pattern)
    best_hours = sorted(hourly_pattern, key=lambda row: row["votes"], reverse=True)[:5]
    failure_rate = failures / (successes + failures) if successes + failures else 0.0
    votes_24h = store.votes_in_window(1440)
    votes_7d = store.votes_in_window(10080)

    return {
        "velocity_windows": window_payload,
        "eta": {
            "consensus_seconds": eta_summary["median"],
            "fast_seconds": eta_summary["p25"],
            "slow_seconds": eta_summary["p75"],
            "sample_count": eta_summary["count"],
        },
        "votes": {
            "last_5m": store.votes_in_window(5),
            "last_15m": store.votes_in_window(15),
            "last_30m": store.votes_in_window(30),
            "last_1h": store.votes_in_window(60),
            "last_6h": store.votes_in_window(360),
            "last_12h": store.votes_in_window(720),
            "last_24h": votes_24h,
            "last_7d": votes_7d,
            "recent_loaded_votes": total_votes,
        },
        "polling": {
            "cycles_loaded": total_polls,
            "successes_loaded": successes,
            "failures_loaded": failures,
            "failure_rate_loaded": failure_rate,
            "no_vote_cycle_streak": current_no_vote_streak(cycles_desc),
            "spacing_seconds": poll_spacing_summary,
        },
        "delta_distribution": {
            "all": delta_summary,
            "positive_only": positive_delta_summary,
            "burstiness": burstiness,
        },
        "source_mix": {
            "last_24h": source_24h,
            "last_7d": source_7d,
            "concentration_24h": source_concentration(source_24h),
            "trusted_ranking": trusted_source_ranking(
                source_24h,
                source_delays,
                store.source_rows(),
            ),
            "update_delay": source_delays,
            "active_sources_loaded": len(
                {row["source_name"] for row in source_results_desc if int(row["success"] or 0)}
            ),
        },
        "source_debug": {
            "last_24h": source_debug_24h,
            "last_7d": source_debug_7d,
            "issues_7d": store.source_issue_events(10080),
            "issues_7d_count": store.source_issue_count(10080),
            "delta_trace_7d": store.source_delta_trace(10080),
        },
        "voters": {
            "top_24h": voters_24h,
            "top_7d": voters_7d,
            "unique_24h": len(voters_24h),
            "unique_7d": len(voters_7d),
            "overlap": store.voter_overlap_stats(10080),
            "streaks": store.voter_streaks(),
        },
        "bursts": burst_stats(cycles_chrono),
        "hours": {
            **hours,
            "party_probability_by_hour_utc": hourly_party_probability(party_events),
        },
        "estimate_error": store.estimate_error_stats(),
        "downtime": store.downtime_stats(int(store.config["polling"]["normal_interval_seconds"])),
        "forecast": {
            "readiness": readiness_phase(store.config, estimate, str(state["confidence"])),
            "acceleration_15m_vs_60m": acceleration,
            "projected_parties_24h": votes_24h / party_size if party_size else 0.0,
            "projected_parties_7d": votes_7d / party_size if party_size else 0.0,
            "best_hours_utc": best_hours,
            "data_quality": data_quality_score(state, source_24h, failure_rate),
        },
        "party_intervals": store.party_interval_stats(),
    }


def poll_interval(config: dict[str, Any], estimate: int) -> int:
    polling = config["polling"]
    if estimate >= int(polling["trigger_threshold"]):
        return int(polling["trigger_interval_seconds"])
    if estimate >= int(polling["near_threshold"]):
        return int(polling["near_interval_seconds"])
    return int(polling["normal_interval_seconds"])


def latest_poll_schedule_base(
    store: Store, fallback_at: datetime, fallback_estimate: int
) -> tuple[datetime, int]:
    try:
        state_estimate = int(store.get_state()["current_estimate"])
    except Exception:
        state_estimate = fallback_estimate
    row = store.conn.execute(
        """
        SELECT ended_at, estimate_after
        FROM poll_cycles
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return fallback_at, state_estimate
    ended_at = parse_iso(row["ended_at"]) or fallback_at
    return ended_at, state_estimate


def publish_next_poll_due(
    store: Store, fallback_at: datetime, fallback_estimate: int
) -> datetime:
    base_at, estimate = latest_poll_schedule_base(
        store, fallback_at, fallback_estimate
    )
    interval = store.effective_poll_interval_seconds(estimate)
    due_at = base_at + timedelta(seconds=max(1, interval))
    store.set_next_poll_due_at(due_at)
    return due_at


def sleep_until_next_poll(
    store: Store, fallback_at: datetime, fallback_estimate: int
) -> None:
    last_due_iso = ""
    while True:
        base_at, estimate = latest_poll_schedule_base(
            store, fallback_at, fallback_estimate
        )
        interval = store.effective_poll_interval_seconds(estimate)
        due_at = base_at + timedelta(seconds=max(1, interval))
        due_iso = iso(due_at)
        if due_iso != last_due_iso:
            store.set_next_poll_due_at(due_at)
            last_due_iso = due_iso
        remaining = (due_at - utc_now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(1.0, max(0.1, remaining)))


def describe_cycle(cycle: PollCycleResult, store: Store, config: dict[str, Any]) -> str:
    party_size = int(config["vp_party_size"])
    remaining = votes_remaining(cycle.estimate_after, party_size)
    velocity = store.recent_vote_velocity_per_minute()
    parts = [
        f"delta={cycle.total_delta}",
        f"estimate={cycle.estimate_after}/{party_size}",
        f"remaining={remaining}",
        f"confidence={cycle.confidence}",
        f"velocity={velocity:.2f}/min",
        f"ok={cycle.successes}",
        f"fail={cycle.failures}",
    ]
    return ", ".join(parts)


def print_status(store: Store, config: dict[str, Any]) -> None:
    state = store.get_state()
    estimate = int(state["current_estimate"])
    party_size = int(config["vp_party_size"])
    remaining = votes_remaining(estimate, party_size)
    velocity = store.recent_vote_velocity_per_minute()
    interval = store.effective_poll_interval_seconds(estimate)
    override = store.poll_interval_override_seconds()
    next_poll_due = store.next_poll_due_at()
    next_poll_remaining = (
        max(0, int(math.ceil((next_poll_due - utc_now()).total_seconds())))
        if next_poll_due
        else None
    )
    calibrated_at = state["last_calibrated_at"] or "never"
    print(f"VP estimate: {estimate}/{party_size} ({remaining} remaining)")
    print(f"Confidence: {state['confidence']}")
    print(f"Last calibrated: {calibrated_at} ({state['calibration_source'] or 'none'})")
    print(f"Observed votes since calibration/db start: {state['total_observed_votes']}")
    print(f"Recent velocity: {velocity:.2f} votes/min")
    source = "manual override" if override is not None else "dynamic"
    print(f"Poll interval: {interval}s ({source})")
    print(f"Next poll in: {format_duration(next_poll_remaining)}")
    print("")
    print("Recent snapshots:")
    for row in store.latest_snapshots(12):
        status = "ok" if row["success"] else "fail"
        count = "" if row["count"] is None else f" count={row['count']}"
        detail = row["raw_summary"] or row["error"] or ""
        print(f"- {row['checked_at']} {row['source_name']} {status}{count} {detail}")


def healthcheck_payload(store: Store, config: dict[str, Any]) -> dict[str, Any]:
    service = config.get("service", {})
    max_age = int(service.get("healthcheck_max_poll_age_seconds", 180))
    min_sources = int(service.get("minimum_successful_sources", 3))
    state = store.get_state()
    estimate = int(state["current_estimate"])
    party_size = int(config["vp_party_size"])
    recent_cycles = store.recent_cycles(1)
    latest_cycle = row_to_dict(recent_cycles[0]) if recent_cycles else None
    latest_poll_at = parse_iso(latest_cycle["ended_at"]) if latest_cycle else None
    latest_poll_age = (
        int(max(0, (utc_now() - latest_poll_at).total_seconds()))
        if latest_poll_at
        else None
    )

    active_sources = [row for row in store.source_rows() if bool(row["enabled"])]
    latest_results: dict[str, sqlite3.Row] = {}
    for row in store.recent_source_results(500):
        latest_results.setdefault(str(row["source_name"]), row)

    fresh_successes: list[str] = []
    failing_sources: list[dict[str, Any]] = []
    stale_sources: list[str] = []
    for source in active_sources:
        name = str(source["name"])
        result = latest_results.get(name)
        checked_at = parse_iso(result["checked_at"]) if result else None
        age = (
            int(max(0, (utc_now() - checked_at).total_seconds()))
            if checked_at
            else None
        )
        if result and bool(result["success"]) and age is not None and age <= max_age:
            fresh_successes.append(name)
        elif result and not bool(result["success"]):
            failing_sources.append(
                {
                    "source": name,
                    "age_seconds": age,
                    "skipped": bool(result["skipped"]),
                    "error": result["error"] or "",
                }
            )
        if age is None or age > max_age:
            stale_sources.append(name)

    latest_poll_fresh = latest_poll_age is not None and latest_poll_age <= max_age
    enough_sources = len(fresh_successes) >= min_sources
    ok = latest_poll_fresh and enough_sources
    return {
        "ok": ok,
        "generated_at": iso(),
        "estimate": {
            "current": estimate,
            "party_size": party_size,
            "remaining": votes_remaining(estimate, party_size),
            "confidence": state["confidence"],
            "last_calibrated_at": state["last_calibrated_at"],
            "last_updated_at": state["last_updated_at"],
        },
        "polling": {
            "latest_cycle": latest_cycle,
            "latest_poll_age_seconds": latest_poll_age,
            "max_poll_age_seconds": max_age,
            "next_poll_due_at": iso(store.next_poll_due_at()) if store.next_poll_due_at() else None,
            "poll_interval_seconds": store.effective_poll_interval_seconds(estimate),
            "poll_interval_override_seconds": store.poll_interval_override_seconds(),
        },
        "sources": {
            "active": len(active_sources),
            "minimum_successful": min_sources,
            "fresh_successful": len(fresh_successes),
            "fresh_successful_names": fresh_successes,
            "stale": len(stale_sources),
            "stale_names": stale_sources,
            "failing": failing_sources[:12],
        },
        "checks": {
            "latest_poll_fresh": latest_poll_fresh,
            "enough_sources": enough_sources,
        },
    }


def run_healthcheck(store: Store, config: dict[str, Any]) -> int:
    payload = healthcheck_payload(store, config)
    print(json.dumps(payload, indent=2, default=str))
    return 0 if payload["ok"] else 1


def run_daemon(store: Store, config: dict[str, Any]) -> None:
    notifier = Notifier(config)
    controller = MinecraftController(config, store, notifier)
    chat = ChatLogTailer(config, store, controller)
    notifier.notify("Vote Party tracker", "Daemon started.")
    while True:
        with store.lock:
            cycle = poll_sources(store, config)
            cycle.log_events.extend(chat.poll())
            cycle.action_messages.extend(controller.maybe_act(cycle.confidence))
            cycle.action_messages.extend(controller.monitor_online_limits())
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {describe_cycle(cycle, store, config)}")
        for source_result in cycle.source_results:
            if source_result.success:
                detail = f"delta={source_result.delta}"
                if source_result.suppressed_delta:
                    detail += f", raw_delta={source_result.raw_delta}, suppressed={source_result.suppressed_delta}"
                if source_result.count is not None:
                    detail += f", count={source_result.count}"
                if source_result.new_events:
                    detail += f", new_events={source_result.new_events}"
                if source_result.fetcher_used and source_result.fetcher_used != "http":
                    detail += f", via={source_result.fetcher_used}"
                if source_result.adjustment_note:
                    detail += f", {source_result.adjustment_note}"
                print(f"  {source_result.name}: ok ({detail})")
            elif source_result.skipped:
                print(f"  {source_result.name}: skipped ({source_result.error})")
            else:
                print(f"  {source_result.name}: failed ({source_result.error})")
        for message in cycle.log_events + cycle.action_messages:
            print(f"  note: {message}")
        sys.stdout.flush()
        sleep_until_next_poll(store, cycle.ended_at, cycle.estimate_after)


def run_once(store: Store, config: dict[str, Any]) -> None:
    notifier = Notifier(config)
    controller = MinecraftController(config, store, notifier)
    chat = ChatLogTailer(config, store, controller)
    cycle = poll_sources(store, config)
    cycle.log_events.extend(chat.poll())
    cycle.action_messages.extend(controller.maybe_act(cycle.confidence))
    cycle.action_messages.extend(controller.monitor_online_limits())
    publish_next_poll_due(store, cycle.ended_at, cycle.estimate_after)
    print(describe_cycle(cycle, store, config))
    for source_result in cycle.source_results:
        if source_result.success:
            pieces = [f"{source_result.name}: ok"]
            if source_result.count is not None:
                pieces.append(f"count={source_result.count}")
            if source_result.delta:
                pieces.append(f"delta={source_result.delta}")
            if source_result.suppressed_delta:
                pieces.append(f"raw_delta={source_result.raw_delta}")
                pieces.append(f"suppressed={source_result.suppressed_delta}")
            if source_result.new_events:
                pieces.append(f"new_events={source_result.new_events}")
            if source_result.fetcher_used and source_result.fetcher_used != "http":
                pieces.append(f"via={source_result.fetcher_used}")
            if source_result.adjustment_note:
                pieces.append(source_result.adjustment_note)
            if source_result.raw_summary:
                pieces.append(f"summary={source_result.raw_summary!r}")
            print("  " + ", ".join(pieces))
        elif source_result.skipped:
            print(f"  {source_result.name}: skipped, {source_result.error}")
        else:
            print(f"  {source_result.name}: failed, {source_result.error}")
    for message in cycle.log_events + cycle.action_messages:
        print(f"  note: {message}")


def dashboard_to_dict(snapshot: DashboardSnapshot) -> dict[str, Any]:
    return {
        "generated_at": snapshot.generated_at,
        "state": snapshot.state,
        "stats": snapshot.stats,
        "history": snapshot.history,
        "sources": snapshot.sources,
    }


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def text_response(
    handler: BaseHTTPRequestHandler, status: int, body: str, content_type: str
) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


def make_dashboard_handler(
    store: Store, config: dict[str, Any], token: str
) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "VotePartyDashboard/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if not self.authorized(parsed):
                json_response(self, 403, {"error": "forbidden"})
                return
            if parsed.path == "/":
                text_response(
                    self,
                    200,
                    dashboard_html(token, int(config["gui"]["refresh_seconds"])),
                    "text/html; charset=utf-8",
                )
                return
            if parsed.path == "/api/dashboard":
                with store.lock:
                    payload = dashboard_to_dict(store.dashboard_snapshot())
                json_response(self, 200, payload)
                return
            if parsed.path == "/api/health":
                json_response(self, 200, {"ok": True, "generated_at": iso()})
                return
            json_response(self, 404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if not self.authorized(parsed):
                json_response(self, 403, {"error": "forbidden"})
                return
            payload = self.read_json()
            if parsed.path == "/api/calibrate":
                count = int(payload.get("count", -1))
                if count < 0:
                    json_response(self, 400, {"error": "count must be non-negative"})
                    return
                with store.lock:
                    store.calibrate(count, source="dashboard")
                    payload = dashboard_to_dict(store.dashboard_snapshot())
                json_response(self, 200, payload)
                return
            if parsed.path == "/api/autojoin":
                enabled = bool(payload.get("enabled", False))
                with store.lock:
                    store.set_auto_join_enabled(enabled)
                    payload = dashboard_to_dict(store.dashboard_snapshot())
                json_response(self, 200, payload)
                return
            if parsed.path == "/api/poll":
                with store.lock:
                    cycle = poll_sources(store, config)
                    controller = MinecraftController(config, store, Notifier(config))
                    ChatLogTailer(config, store, controller).poll()
                    controller.maybe_act(cycle.confidence)
                    controller.monitor_online_limits()
                    payload = dashboard_to_dict(store.dashboard_snapshot())
                json_response(
                    self,
                    200,
                    {
                        "cycle": {
                            "delta": cycle.total_delta,
                            "estimate_after": cycle.estimate_after,
                            "confidence": cycle.confidence,
                        },
                        "dashboard": payload,
                    },
                )
                return
            json_response(self, 404, {"error": "not found"})

        def authorized(self, parsed: urllib.parse.ParseResult) -> bool:
            if self.client_address[0] not in {"127.0.0.1", "::1"}:
                return False
            query = urllib.parse.parse_qs(parsed.query)
            supplied = query.get("token", [""])[0]
            return secrets.compare_digest(supplied, token)

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}
            return loaded if isinstance(loaded, dict) else {}

    return DashboardHandler


def dashboard_html(token: str, refresh_seconds: int) -> str:
    safe_token = json.dumps(token)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vote Party Tracker</title>
<style>
:root {{
  color-scheme: dark;
  --bg: #090a0b;
  --panel: #111316;
  --panel-2: #171a1d;
  --line: #2a2f33;
  --text: #eef4ef;
  --muted: #8f9b95;
  --mint: #72f0ba;
  --amber: #f5c66a;
  --red: #ff6b6b;
  --steel: #aeb8b2;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
button, input {{ font: inherit; }}
.shell {{ max-width: 1480px; margin: 0 auto; padding: 20px; }}
.topbar {{
  display: flex; justify-content: space-between; gap: 16px; align-items: center;
  padding: 14px 0 18px; border-bottom: 1px solid var(--line);
}}
.brand {{ display: flex; gap: 12px; align-items: baseline; }}
.brand h1 {{ margin: 0; font-size: 18px; letter-spacing: .08em; text-transform: uppercase; }}
.brand span {{ color: var(--muted); font-size: 12px; }}
.controls {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
button {{
  border: 1px solid var(--line); color: var(--text); background: var(--panel-2);
  border-radius: 6px; padding: 8px 10px; cursor: pointer;
}}
button:hover {{ border-color: var(--mint); }}
input {{
  width: 92px; border: 1px solid var(--line); color: var(--text);
  background: #0c0e10; border-radius: 6px; padding: 8px 10px;
}}
.grid {{ display: grid; gap: 14px; margin-top: 16px; }}
.metrics {{ grid-template-columns: repeat(6, minmax(140px, 1fr)); }}
.main {{ grid-template-columns: minmax(320px, 1.25fr) minmax(320px, .75fr); }}
.wide {{ grid-template-columns: 1fr 1fr; }}
.card {{
  background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 14px; min-width: 0;
}}
.label {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .09em; }}
.value {{ font-size: 28px; font-weight: 720; margin-top: 4px; white-space: nowrap; }}
.sub {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
.ok {{ color: var(--mint); }}
.warn {{ color: var(--amber); }}
.bad {{ color: var(--red); }}
.progress {{
  height: 10px; border-radius: 5px; overflow: hidden; background: #070808;
  border: 1px solid var(--line); margin-top: 12px;
}}
.progress div {{ height: 100%; width: 0; background: linear-gradient(90deg, var(--mint), var(--amber)); }}
.section-title {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
.section-title h2 {{ margin: 0; font-size: 13px; text-transform: uppercase; letter-spacing: .1em; }}
canvas {{ width: 100%; height: 260px; display: block; }}
.heat {{ display: grid; grid-template-columns: repeat(24, 1fr); gap: 4px; margin-top: 12px; }}
.heat div {{
  min-height: 72px; border-radius: 5px; border: 1px solid var(--line);
  display: flex; align-items: end; justify-content: center; padding: 4px;
  color: var(--steel); font-size: 10px; background: #0d0f10;
}}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }}
th {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }}
td {{ color: var(--text); }}
.scroll {{ overflow: auto; max-height: 360px; }}
.split {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }}
.statline {{ display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--line); }}
.statline span:first-child {{ color: var(--muted); }}
.mono {{ font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
@media (max-width: 1100px) {{
  .metrics {{ grid-template-columns: repeat(3, minmax(140px, 1fr)); }}
  .main, .wide {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 680px) {{
  .shell {{ padding: 12px; }}
  .topbar {{ align-items: flex-start; flex-direction: column; }}
  .metrics {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
  .value {{ font-size: 22px; }}
  th, td {{ white-space: normal; }}
}}
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div class="brand">
      <h1>Vote Party Tracker</h1>
      <span id="updated">booting</span>
    </div>
    <div class="controls">
      <input id="calibrateInput" type="number" min="0" placeholder="VP">
      <button id="calibrateButton">Calibrate</button>
      <button id="pollButton">Poll Now</button>
      <button id="autoJoinButton">Auto-Join OFF</button>
      <button id="pauseButton">Pause UI</button>
    </div>
  </header>

  <section class="grid metrics">
    <div class="card"><div class="label">Estimate</div><div class="value mono" id="estimate">--</div><div class="progress"><div id="progressBar"></div></div><div class="sub" id="remaining">--</div></div>
    <div class="card"><div class="label">ETA Consensus</div><div class="value mono" id="eta">--</div><div class="sub" id="etaBand">--</div></div>
    <div class="card"><div class="label">Velocity 30m</div><div class="value mono" id="velocity30">--</div><div class="sub" id="velocitySpread">--</div></div>
    <div class="card"><div class="label">Confidence</div><div class="value" id="confidence">--</div><div class="sub" id="calibration">--</div></div>
    <div class="card"><div class="label">Votes 24h</div><div class="value mono" id="votes24">--</div><div class="sub" id="votes7d">--</div></div>
    <div class="card"><div class="label">Poll Health</div><div class="value mono" id="health">--</div><div class="sub" id="streak">--</div></div>
  </section>

  <section class="grid main">
    <div class="card">
      <div class="section-title"><h2>Vote Flow</h2><span class="sub">delta bars + estimate line</span></div>
      <canvas id="flowCanvas" width="900" height="320"></canvas>
    </div>
    <div class="card">
      <div class="section-title"><h2>ETA Models</h2><span class="sub">rolling windows</span></div>
      <div class="scroll"><table><thead><tr><th>Window</th><th>Votes</th><th>Rate</th><th>ETA</th></tr></thead><tbody id="velocityRows"></tbody></table></div>
    </div>
  </section>

  <section class="grid wide">
    <div class="card">
      <div class="section-title"><h2>Source Mix</h2><span class="sub">independent websites</span></div>
      <div class="scroll"><table><thead><tr><th>Source</th><th>Votes</th><th>OK</th><th>Fail</th><th>Skip</th><th>Reader</th><th>Reliability</th></tr></thead><tbody id="sourceRows"></tbody></table></div>
    </div>
    <div class="card">
      <div class="section-title"><h2>Statistics</h2><span class="sub">loaded history</span></div>
      <div class="split">
        <div id="statLeft"></div>
        <div id="statRight"></div>
      </div>
    </div>
  </section>

  <section class="grid wide">
    <div class="card">
      <div class="section-title"><h2>Last Voters</h2><span class="sub">local time</span></div>
      <div class="scroll"><table><thead><tr><th>Username</th><th>Source</th><th>Vote Time</th><th>Detected</th><th>Raw</th></tr></thead><tbody id="lastVoterRows"></tbody></table></div>
    </div>
    <div class="card">
      <div class="section-title"><h2>Username Stats</h2><span class="sub">observed recent voters</span></div>
      <div class="scroll"><table><thead><tr><th>Username</th><th>Votes</th><th>Sources</th><th>Last Seen</th></tr></thead><tbody id="voterStatRows"></tbody></table></div>
    </div>
  </section>

  <section class="grid">
    <div class="card">
      <div class="section-title"><h2>Hour Pattern</h2><span class="sub">UTC vote density</span></div>
      <div class="heat" id="hourHeat"></div>
    </div>
  </section>

  <section class="grid wide">
    <div class="card">
      <div class="section-title"><h2>Estimated Parties</h2><span class="sub">crossing history</span></div>
      <div class="scroll"><table><thead><tr><th>Time</th><th>Before</th><th>After</th><th>Delta</th><th>Confidence</th></tr></thead><tbody id="partyRows"></tbody></table></div>
    </div>
    <div class="card">
      <div class="section-title"><h2>Recent Polls</h2><span class="sub">latest cycles</span></div>
      <div class="scroll"><table><thead><tr><th>Time</th><th>Delta</th><th>Estimate</th><th>OK</th><th>Fail</th></tr></thead><tbody id="cycleRows"></tbody></table></div>
    </div>
  </section>
</div>

<script>
const TOKEN = {safe_token};
const REFRESH_MS = {max(1, refresh_seconds) * 1000};
let paused = false;
let latest = null;

const $ = (id) => document.getElementById(id);
function api(path) {{ return `${{path}}?token=${{encodeURIComponent(TOKEN)}}`; }}
function fmtNumber(n, digits = 0) {{ return n === null || n === undefined || Number.isNaN(n) ? "--" : Number(n).toFixed(digits); }}
function fmtRate(n) {{ return `${{fmtNumber(n, 2)}}/m`; }}
function fmtEta(seconds) {{
  if (seconds === null || seconds === undefined) return "--";
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${{h}}h ${{m}}m`;
  if (m > 0) return `${{m}}m ${{s}}s`;
  return `${{s}}s`;
}}
function shortTime(iso) {{
  if (!iso) return "--";
  return new Date(iso).toLocaleTimeString([], {{hour: "2-digit", minute: "2-digit", second: "2-digit"}});
}}
function classForConfidence(value) {{
  if (value === "high") return "ok";
  if (value === "medium") return "warn";
  return "bad";
}}
async function loadDashboard() {{
  if (paused) return;
  const res = await fetch(api("/api/dashboard"));
  if (!res.ok) throw new Error(`dashboard ${{res.status}}`);
  latest = await res.json();
  render(latest);
}}
function render(data) {{
  latest = data;
  const state = data.state;
  const stats = data.stats;
  const progress = state.party_size ? (state.estimate / state.party_size) * 100 : 0;
  $("updated").textContent = `updated ${{shortTime(data.generated_at)}}`;
  $("estimate").textContent = `${{state.estimate}}/${{state.party_size}}`;
  $("remaining").textContent = `${{state.remaining}} remaining · next poll ${{state.poll_interval_seconds}}s`;
  $("progressBar").style.width = `${{Math.max(0, Math.min(100, progress))}}%`;
  $("eta").textContent = fmtEta(stats.eta.consensus_seconds);
  $("etaBand").textContent = `fast ${{fmtEta(stats.eta.fast_seconds)}} · slow ${{fmtEta(stats.eta.slow_seconds)}}`;
  const v30 = stats.velocity_windows.find(w => w.minutes === 30) || {{}};
  const v5 = stats.velocity_windows.find(w => w.minutes === 5) || {{}};
  const v60 = stats.velocity_windows.find(w => w.minutes === 60) || {{}};
  $("velocity30").textContent = fmtRate(v30.velocity_per_minute);
  $("velocitySpread").textContent = `5m ${{fmtRate(v5.velocity_per_minute)}} · 60m ${{fmtRate(v60.velocity_per_minute)}}`;
  $("confidence").textContent = state.confidence;
  $("confidence").className = `value ${{classForConfidence(state.confidence)}}`;
  $("calibration").textContent = state.last_calibrated_at ? `calibrated ${{shortTime(state.last_calibrated_at)}}` : "not calibrated";
  $("votes24").textContent = stats.votes.last_24h;
  $("votes7d").textContent = `${{stats.votes.last_7d}} in 7d`;
  $("health").textContent = `${{Math.round((1 - stats.polling.failure_rate_loaded) * 100)}}%`;
  $("streak").textContent = `${{stats.forecast.readiness.phase}} · quality ${{Math.round(stats.forecast.data_quality.score)}}%`;
  $("autoJoinButton").textContent = state.auto_join_enabled ? "Auto-Join ON" : "Auto-Join OFF";
  $("autoJoinButton").className = state.auto_join_enabled ? "warn" : "";
  renderVelocity(stats.velocity_windows);
  renderSources(stats.source_mix.last_24h);
  renderLastVoters(data.history.latest_voters);
  renderVoterStats(stats.voters.top_24h);
  renderStats(stats);
  renderHeat(data.history.hourly_pattern);
  renderParties(data.history.vote_party_events.slice(-12).reverse());
  renderCycles(data.history.cycles.slice(-24).reverse());
  drawFlow(data.history.cycles.slice(-120), state.party_size);
}}
function renderVelocity(rows) {{
  $("velocityRows").innerHTML = rows.map(row => `<tr><td>${{row.minutes}}m</td><td class="mono">${{row.votes}}</td><td class="mono">${{fmtRate(row.velocity_per_minute)}}</td><td class="mono">${{fmtEta(row.eta_seconds)}}</td></tr>`).join("");
}}
function renderSources(rows) {{
  $("sourceRows").innerHTML = rows.map(row => `<tr><td>${{escapeHtml(row.source)}}</td><td class="mono">${{row.votes}}</td><td class="mono">${{row.ok_polls}}</td><td class="mono">${{row.failed_polls}}</td><td class="mono">${{row.skipped_polls}}</td><td class="mono">${{row.reader_polls}}</td><td class="mono">${{Math.round(row.success_rate * 100)}}%</td></tr>`).join("");
}}
function renderLastVoters(rows) {{
  $("lastVoterRows").innerHTML = rows.map(row => `<tr><td>${{escapeHtml(row.username)}}</td><td>${{escapeHtml(row.source)}}</td><td class="mono">${{escapeHtml(row.vote_time_local || "--")}}</td><td class="mono">${{escapeHtml(row.detected_at_local || "--")}}</td><td>${{escapeHtml(row.vote_time_text || "")}}</td></tr>`).join("");
}}
function renderVoterStats(rows) {{
  $("voterStatRows").innerHTML = rows.map(row => `<tr><td>${{escapeHtml(row.username)}}</td><td class="mono">${{row.votes_seen}}</td><td class="mono">${{row.sources_seen}}</td><td class="mono">${{escapeHtml(row.last_vote_time_local || "--")}}</td></tr>`).join("");
}}
function renderStats(stats) {{
  const left = [
    ["Readiness", stats.forecast.readiness.phase],
    ["Action", stats.forecast.readiness.action],
    ["Quality", `${{fmtNumber(stats.forecast.data_quality.score, 0)}}%`],
    ["Burstiness", fmtNumber(stats.delta_distribution.burstiness, 2)],
    ["Acceleration", fmtNumber(stats.forecast.acceleration_15m_vs_60m, 2)],
    ["Avg delta", fmtNumber(stats.delta_distribution.all.mean, 2)],
    ["Max delta", fmtNumber(stats.delta_distribution.all.max, 0)],
    ["Poll spacing", fmtEta(stats.polling.spacing_seconds.median)],
  ];
  const conc = stats.source_mix.concentration_24h;
  const right = [
    ["Top source", conc.top_source || "--"],
    ["Top share", `${{fmtNumber((conc.top_share || 0) * 100, 0)}}%`],
    ["HHI", fmtNumber(conc.hhi, 2)],
    ["Active sources", stats.source_mix.active_sources_loaded],
    ["ETA models", stats.eta.sample_count],
    ["Parties 24h", fmtNumber(stats.forecast.projected_parties_24h, 2)],
    ["Parties 7d", fmtNumber(stats.forecast.projected_parties_7d, 2)],
    ["Party median", fmtEta(stats.party_intervals.median)],
  ];
  $("statLeft").innerHTML = left.map(statline).join("");
  $("statRight").innerHTML = right.map(statline).join("");
}}
function statline(row) {{ return `<div class="statline"><span>${{row[0]}}</span><span class="mono">${{row[1]}}</span></div>`; }}
function renderHeat(rows) {{
  const maxVotes = Math.max(1, ...rows.map(r => r.votes));
  $("hourHeat").innerHTML = rows.map(row => {{
    const a = row.votes / maxVotes;
    const bg = `rgba(114, 240, 186, ${{0.08 + a * 0.72}})`;
    return `<div style="background:${{bg}}" title="${{row.votes}} votes">${{String(row.hour_utc).padStart(2, "0")}}</div>`;
  }}).join("");
}}
function renderParties(rows) {{
  $("partyRows").innerHTML = rows.map(row => `<tr><td>${{shortTime(row.estimated_at)}}</td><td class="mono">${{row.estimate_before}}</td><td class="mono">${{row.estimate_after}}</td><td class="mono">${{row.delta}}</td><td>${{row.confidence}}</td></tr>`).join("");
}}
function renderCycles(rows) {{
  $("cycleRows").innerHTML = rows.map(row => `<tr><td>${{shortTime(row.ended_at)}}</td><td class="mono">${{row.total_delta}}</td><td class="mono">${{row.estimate_after}}</td><td class="mono">${{row.successes}}</td><td class="mono">${{row.failures}}</td></tr>`).join("");
}}
function drawFlow(cycles, partySize) {{
  const canvas = $("flowCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b0d0f";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#252a2e";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {{
    const y = 20 + i * ((h - 45) / 4);
    ctx.beginPath(); ctx.moveTo(35, y); ctx.lineTo(w - 12, y); ctx.stroke();
  }}
  if (!cycles.length) return;
  const maxDelta = Math.max(1, ...cycles.map(c => c.total_delta || 0));
  const step = (w - 55) / Math.max(1, cycles.length);
  cycles.forEach((c, i) => {{
    const x = 38 + i * step;
    const barH = ((c.total_delta || 0) / maxDelta) * (h - 60);
    ctx.fillStyle = "rgba(245,198,106,.76)";
    ctx.fillRect(x, h - 25 - barH, Math.max(2, step * .55), barH);
  }});
  ctx.strokeStyle = "#72f0ba";
  ctx.lineWidth = 2;
  ctx.beginPath();
  cycles.forEach((c, i) => {{
    const x = 38 + i * step + step * .28;
    const y = 20 + (1 - ((c.estimate_after || 0) / partySize)) * (h - 55);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();
}}
function escapeHtml(value) {{
  return String(value).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
$("pauseButton").onclick = () => {{
  paused = !paused;
  $("pauseButton").textContent = paused ? "Resume UI" : "Pause UI";
}};
$("pollButton").onclick = async () => {{
  $("pollButton").textContent = "Polling...";
  try {{
    const res = await fetch(api("/api/poll"), {{method: "POST", body: "{{}}"}});
    const payload = await res.json();
    render(payload.dashboard);
  }} finally {{
    $("pollButton").textContent = "Poll Now";
  }}
}};
$("autoJoinButton").onclick = async () => {{
  const enabled = !(latest && latest.state && latest.state.auto_join_enabled);
  const res = await fetch(api("/api/autojoin"), {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{enabled}})
  }});
  render(await res.json());
}};
$("calibrateButton").onclick = async () => {{
  const count = Number($("calibrateInput").value);
  if (!Number.isFinite(count) || count < 0) return;
  const res = await fetch(api("/api/calibrate"), {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{count}})
  }});
  render(await res.json());
}};
loadDashboard().catch(console.error);
setInterval(() => loadDashboard().catch(console.error), REFRESH_MS);
</script>
</body>
</html>"""


def run_web_gui(store: Store, config: dict[str, Any], block: bool = True) -> ThreadingHTTPServer:
    gui_config = config.get("gui", {})
    host = str(gui_config.get("host", "127.0.0.1"))
    port = int(gui_config.get("port", 8765))
    token = secrets.token_urlsafe(18)
    handler = make_dashboard_handler(store, config, token)
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/?token={urllib.parse.quote(token)}"
    print(f"Dashboard: {url}", flush=True)
    if gui_config.get("open_browser", True):
        webbrowser.open(url)
    if block:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server


class DesktopDashboard:
    def __init__(self, store: Store, config: dict[str, Any]) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.store = store
        self.config = config
        self.refresh_ms = max(1, int(config.get("gui", {}).get("refresh_seconds", 5))) * 1000
        self.colors = {
            "bg": "#0b1014",
            "panel": "#0f151b",
            "card": "#141c23",
            "card_soft": "#17212a",
            "line": "#26323a",
            "text": "#e8f0eb",
            "muted": "#9aa8a1",
            "accent": "#7dd3ae",
            "accent_dim": "#24463a",
            "warning": "#f5c66a",
            "blue": "#8fb3ff",
            "graph_bg": "#0c1217",
            "danger": "#f08d8d",
        }
        self.root = tk.Tk()
        self.root.title("Vote Party Tracker")
        self.root.geometry("1520x1000")
        self.root.minsize(1180, 760)
        self.root.configure(bg=self.colors["bg"])
        self.metric_vars: dict[str, Any] = {}
        self.tables: dict[str, Any] = {}
        self.table_rows: dict[str, list[tuple[Any, ...]]] = {}
        self.table_columns: dict[str, tuple[str, ...]] = {}
        self.table_titles: dict[str, str] = {}
        self.popout_tables: dict[str, Any] = {}
        self.canvas = None
        self.flow_popout_canvas = None
        self.toast_window = None
        self.last_seen_cycle_id: int | None = None
        self.latest_payload: dict[str, Any] | None = None
        self.next_poll_due_at: datetime | None = None
        self.calibrate_var = tk.StringVar()
        self.poll_interval_var = tk.StringVar()
        self.flow_window_var = tk.StringVar(value="24h")
        self.status_var = tk.StringVar(value="Starting")
        self.poll_interval_entry = None
        self._build_style()
        self._build_layout()
        self.root.after(1000, self.tick_poll_countdown)

    def _build_style(self) -> None:
        ttk = self.ttk
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        colors = self.colors
        style.configure(
            ".",
            background=colors["bg"],
            foreground=colors["text"],
            fieldbackground=colors["card"],
        )
        style.configure("TFrame", background=colors["bg"])
        style.configure("Card.TFrame", background=colors["card"], relief="flat")
        style.configure("Panel.TFrame", background=colors["panel"], relief="flat")
        style.configure("TLabel", background=colors["bg"], foreground=colors["text"])
        style.configure("Muted.TLabel", background=colors["bg"], foreground=colors["muted"])
        style.configure("Card.TLabel", background=colors["card"], foreground=colors["text"])
        style.configure(
            "MutedCard.TLabel", background=colors["card"], foreground=colors["muted"]
        )
        style.configure(
            "Metric.TLabel",
            background=colors["card"],
            foreground=colors["text"],
            font=("Menlo", 24, "bold"),
        )
        style.configure(
            "TButton",
            background=colors["card_soft"],
            foreground=colors["text"],
            bordercolor=colors["line"],
            padding=(10, 5),
        )
        style.configure(
            "Accent.TButton",
            background=colors["accent_dim"],
            foreground=colors["text"],
            bordercolor=colors["accent"],
        )
        style.map(
            "TButton",
            background=[("active", colors["line"])],
            foreground=[("disabled", colors["muted"])],
        )
        style.configure(
            "TEntry",
            fieldbackground=colors["panel"],
            foreground=colors["text"],
            bordercolor=colors["line"],
            insertcolor=colors["text"],
        )
        style.configure(
            "TCombobox",
            fieldbackground=colors["panel"],
            background=colors["card_soft"],
            foreground=colors["text"],
            bordercolor=colors["line"],
            arrowcolor=colors["muted"],
        )
        style.configure("Dashboard.TNotebook", background=colors["bg"], borderwidth=0)
        style.configure(
            "Dashboard.TNotebook.Tab",
            background=colors["panel"],
            foreground=colors["muted"],
            padding=(18, 9),
            font=("Helvetica", 11, "bold"),
        )
        style.map(
            "Dashboard.TNotebook.Tab",
            background=[("selected", colors["card"])],
            foreground=[("selected", colors["text"])],
        )
        style.configure(
            "Treeview",
            background=colors["panel"],
            fieldbackground=colors["panel"],
            foreground=colors["text"],
            bordercolor=colors["line"],
            rowheight=27,
        )
        style.configure(
            "Treeview.Heading",
            background=colors["card_soft"],
            foreground=colors["muted"],
            relief="flat",
            font=("Helvetica", 10, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", colors["accent_dim"])],
            foreground=[("selected", colors["text"])],
        )

    def _build_layout(self) -> None:
        tk = self.tk
        ttk = self.ttk
        root = self.root

        shell = ttk.Frame(root, padding=(16, 12))
        shell.pack(fill="both", expand=True)

        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 10))
        title = ttk.Frame(header)
        title.pack(side="left")
        ttk.Label(title, text="Vote Party Tracker", font=("Helvetica", 19, "bold")).pack(anchor="w")
        ttk.Label(title, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        controls = ttk.Frame(header)
        controls.pack(side="right")
        ttk.Entry(controls, textvariable=self.calibrate_var, width=8).pack(side="left", padx=4)
        ttk.Button(controls, text="Calibrate", command=self.calibrate).pack(side="left", padx=4)
        ttk.Button(controls, text="Poll Now", command=self.poll_now, style="Accent.TButton").pack(side="left", padx=4)
        ttk.Label(controls, text="Poll every", style="Muted.TLabel").pack(side="left", padx=(14, 4))
        self.poll_interval_entry = ttk.Entry(controls, textvariable=self.poll_interval_var, width=6)
        self.poll_interval_entry.pack(side="left", padx=2)
        ttk.Label(controls, text="s", style="Muted.TLabel").pack(side="left")
        ttk.Button(controls, text="Apply", command=self.apply_poll_interval).pack(side="left", padx=(6, 2))
        ttk.Button(controls, text="Auto", command=self.clear_poll_interval).pack(side="left", padx=2)
        self.auto_button = ttk.Button(controls, text="Auto-Join OFF", command=self.toggle_auto_join)
        self.auto_button.pack(side="left", padx=4)

        metrics = ttk.Frame(shell)
        metrics.pack(fill="x", pady=(0, 10))
        for idx in range(7):
            metrics.columnconfigure(idx, weight=1, uniform="metric")
        for idx, key in enumerate(("estimate", "poll", "eta", "velocity", "confidence", "votes", "health")):
            self._metric_card(metrics, key, idx)

        notebook = ttk.Notebook(shell, style="Dashboard.TNotebook")
        notebook.pack(fill="both", expand=True)
        overview = self._tab(notebook, "Overview")
        sources_tab = self._tab(notebook, "Sources")
        voters_tab = self._tab(notebook, "Voters")
        forecast_tab = self._tab(notebook, "Forecast")
        audit_tab = self._tab(notebook, "Audit")

        self._configure_grid(overview, (3, 2), 2)
        flow = self._card(overview, "Vote Flow", "long-range trend", "flow")
        flow.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=(0, 8), pady=(10, 8))
        flow_controls = ttk.Frame(flow, style="Card.TFrame")
        flow_controls.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(flow_controls, text="Window", style="MutedCard.TLabel").pack(side="left")
        flow_picker = ttk.Combobox(
            flow_controls,
            textvariable=self.flow_window_var,
            values=("2h", "6h", "24h", "7d", "all"),
            width=6,
            state="readonly",
        )
        flow_picker.pack(side="left", padx=(8, 0))
        flow_picker.bind("<<ComboboxSelected>>", lambda _event: self.redraw_flow())
        self.canvas = tk.Canvas(
            flow, bg=self.colors["graph_bg"], highlightthickness=0, height=240
        )
        self.canvas.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        velocity = self._card(overview, "ETA Models", "rolling windows", "velocity")
        velocity.grid(row=0, column=2, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.tables["velocity"] = self._table(velocity, ("Window", "Votes", "Rate", "ETA"), "velocity", "ETA Models")

        cycles = self._card(overview, "Recent Polls", "latest cycles", "cycles")
        cycles.grid(row=1, column=2, sticky="nsew", padx=(8, 0), pady=(8, 10))
        self.tables["cycles"] = self._table(cycles, ("Time", "Delta", "Estimate", "OK", "Fail"), "cycles", "Recent Polls")

        advanced = self._card(overview, "Control Summary", "readiness, drift, downtime", "advanced")
        advanced.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=(0, 8), pady=(8, 10))
        self.tables["advanced"] = self._table(advanced, ("Metric", "Value"), "advanced", "Control Summary")

        self._configure_grid(sources_tab, (2, 2), 2)
        sources = self._card(sources_tab, "Source Mix", "independent websites", "sources")
        sources.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(10, 8))
        self.tables["sources"] = self._table(
            sources,
            ("Source", "Votes", "OK", "Fail", "Skip", "Reader", "Reliability"),
            "sources",
            "Source Mix",
        )

        diagnostics = self._card(sources_tab, "Source Diagnostics", "7d call health", "diagnostics")
        diagnostics.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.tables["diagnostics"] = self._table(
            diagnostics,
            ("Source", "OK%", "Fail", "Skip", "Solo", "Catchup", "Suppressed", "Stale", "Score"),
            "diagnostics",
            "Source Diagnostics",
        )

        trace = self._card(sources_tab, "Delta Trace", "recent vote attribution", "delta_trace")
        trace.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 10))
        self.tables["delta_trace"] = self._table(
            trace,
            ("Time", "Delta", "Sources", "Note"),
            "delta_trace",
            "Delta Trace",
        )

        trusted_sources = self._card(sources_tab, "Trusted Ranking", "freshness and consistency", "trusted_sources")
        trusted_sources.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 10))
        self.tables["trusted_sources"] = self._table(
            trusted_sources,
            ("Source", "Score", "OK%", "Fresh", "Delay", "Votes"),
            "trusted_sources",
            "Trusted Ranking",
        )

        self._configure_grid(voters_tab, (2, 2), 2)
        voters = self._card(voters_tab, "Last Voters", "local time", "last_voters")
        voters.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(10, 8))
        self.tables["last_voters"] = self._table(
            voters,
            ("Username", "Source", "Vote Time", "Detected"),
            "last_voters",
            "Last Voters",
        )

        voter_stats = self._card(voters_tab, "Username Stats", "observed recent voters", "voter_stats")
        voter_stats.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.tables["voter_stats"] = self._table(
            voter_stats,
            ("Username", "Votes", "Sources", "Last Seen"),
            "voter_stats",
            "Username Stats",
        )

        overlap = self._card(voters_tab, "Source Overlap", "multi-site voters", "voter_overlap")
        overlap.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 10))
        self.tables["voter_overlap"] = self._table(
            overlap,
            ("Username", "Votes", "Sources", "Share", "Last Seen"),
            "voter_overlap",
            "Source Overlap",
        )

        streaks = self._card(voters_tab, "Voting Streaks", "visible repeat activity", "voter_streaks")
        streaks.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 10))
        self.tables["voter_streaks"] = self._table(
            streaks,
            ("Username", "Days", "Votes", "Last Seen"),
            "voter_streaks",
            "Voting Streaks",
        )

        self._configure_grid(forecast_tab, (2, 2), 2)
        hours = self._card(forecast_tab, "Hour Forecast", "peak, quiet, probability", "hours")
        hours.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(10, 8))
        self.tables["hours"] = self._table(hours, ("Hour UTC", "Votes", "Party Chance"), "hours", "Hour Forecast")

        burst_table = self._card(forecast_tab, "Vote Bursts", "high-flow windows", "bursts")
        burst_table.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.tables["bursts"] = self._table(
            burst_table,
            ("Time", "Votes", "Estimate"),
            "bursts",
            "Vote Bursts",
        )

        party_history = self._card(forecast_tab, "Estimated Parties", "crossing history", "party_history")
        party_history.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 10))
        self.tables["party_history"] = self._table(
            party_history,
            ("Time", "Before", "After", "Delta", "Confidence"),
            "party_history",
            "Estimated Parties",
        )

        intervals = self._card(forecast_tab, "Interval Stats", "party cadence", "intervals")
        intervals.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 10))
        self.tables["intervals"] = self._table(
            intervals,
            ("Metric", "Value"),
            "intervals",
            "Interval Stats",
        )

        self._configure_grid(audit_tab, (2, 2), 2)
        calibration_log = self._card(audit_tab, "Calibration Log", "estimate mismatches", "calibration_log")
        calibration_log.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=(10, 8))
        self.tables["calibration_log"] = self._table(
            calibration_log,
            ("Time", "Previous", "Actual", "Error", "Severity"),
            "calibration_log",
            "Calibration Log",
        )

        source_issues = self._card(audit_tab, "Source Issues", "latest failures and skips", "source_issues")
        source_issues.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=(10, 8))
        self.tables["source_issues"] = self._table(
            source_issues,
            ("Time", "Source", "Type", "Error"),
            "source_issues",
            "Source Issues",
        )

        error_stats = self._card(audit_tab, "Estimate Error", "drift and severity summary", "error_stats")
        error_stats.grid(row=1, column=0, sticky="nsew", padx=(0, 8), pady=(8, 10))
        self.tables["error_stats"] = self._table(
            error_stats,
            ("Metric", "Value"),
            "error_stats",
            "Estimate Error",
        )

        downtime = self._card(audit_tab, "Downtime Impact", "missed votes and gaps", "downtime")
        downtime.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(8, 10))
        self.tables["downtime"] = self._table(
            downtime,
            ("Metric", "Value"),
            "downtime",
            "Downtime Impact",
        )

    def _tab(self, notebook: Any, title: str) -> Any:
        frame = self.ttk.Frame(notebook, style="Panel.TFrame", padding=(10, 0, 10, 10))
        notebook.add(frame, text=title)
        return frame

    def _configure_grid(self, frame: Any, columns: tuple[int, ...], rows: int) -> None:
        for idx, weight in enumerate(columns):
            frame.columnconfigure(idx, weight=weight, uniform=f"col-{len(columns)}")
        for idx in range(rows):
            frame.rowconfigure(idx, weight=1, uniform=f"row-{rows}")

    def _metric_card(self, parent: Any, key: str, column: int) -> None:
        ttk = self.ttk
        frame = ttk.Frame(parent, style="Card.TFrame", padding=12)
        frame.grid(row=0, column=column, sticky="nsew", padx=5, pady=8)
        label = key.replace("_", " ").upper()
        ttk.Label(frame, text=label, style="MutedCard.TLabel", font=("Helvetica", 10, "bold")).pack(anchor="w")
        value = self.tk.StringVar(value="--")
        sub = self.tk.StringVar(value="--")
        self.metric_vars[key] = (value, sub)
        ttk.Label(frame, textvariable=value, style="Metric.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(frame, textvariable=sub, style="MutedCard.TLabel").pack(anchor="w")

    def _card(self, parent: Any, title: str, subtitle: str, popout_key: str | None = None) -> Any:
        ttk = self.ttk
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(10, 10))
        top = ttk.Frame(frame, style="Card.TFrame")
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text=title, style="Card.TLabel", font=("Helvetica", 11, "bold")).pack(side="left")
        if popout_key:
            command = self.open_flow_popout if popout_key == "flow" else lambda key=popout_key, label=title: self.open_table_popout(key, label)
            ttk.Button(top, text="Pop Out", command=command).pack(side="right")
        ttk.Label(top, text=subtitle, style="MutedCard.TLabel").pack(side="right", padx=(0, 8))
        return frame

    def _table(
        self,
        parent: Any,
        columns: tuple[str, ...],
        table_name: str | None = None,
        title: str = "",
    ) -> Any:
        tree = self._create_tree(parent, columns, height=7)
        if table_name:
            self.table_columns[table_name] = columns
            self.table_titles[table_name] = title or table_name
        return tree

    def _create_tree(self, parent: Any, columns: tuple[str, ...], height: int = 7) -> Any:
        ttk = self.ttk
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=height)
        for column in columns:
            tree.heading(column, text=column)
            tree.column(column, width=110, anchor="w", stretch=True)
        tree.tag_configure("even", background=self.colors["panel"])
        tree.tag_configure("odd", background="#111a21")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=yscroll.set)
        tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        return tree

    def run(self) -> None:
        self.refresh()
        self.root.mainloop()

    def refresh(self) -> None:
        try:
            with self.store.lock:
                payload = dashboard_to_dict(self.store.dashboard_snapshot())
            self.render(payload)
        except Exception as exc:
            self.status_var.set(f"Error: {exc}")
        self.root.after(self.refresh_ms, self.refresh)

    def render(self, payload: dict[str, Any]) -> None:
        self.notify_vote_found(payload)
        self.latest_payload = payload
        state = payload["state"]
        stats = payload["stats"]
        history = payload["history"]
        self.next_poll_due_at = parse_iso(state.get("next_poll_due_at"))
        self.status_var.set(
            f"Updated {datetime.now().strftime('%H:%M:%S')} | next poll {format_duration(state.get('next_poll_seconds'))}"
        )
        self.metric_vars["estimate"][0].set(f"{state['estimate']}/{state['party_size']}")
        self.metric_vars["estimate"][1].set(f"{state['remaining']} remaining")
        self.update_poll_metric(state)
        self.metric_vars["eta"][0].set(format_duration(stats["eta"]["consensus_seconds"]))
        self.metric_vars["eta"][1].set(
            f"fast {format_duration(stats['eta']['fast_seconds'])} | slow {format_duration(stats['eta']['slow_seconds'])}"
        )
        velocity_30 = next((w for w in stats["velocity_windows"] if w["minutes"] == 30), None)
        self.metric_vars["velocity"][0].set(format_rate((velocity_30 or {}).get("velocity_per_minute")))
        self.metric_vars["velocity"][1].set("30m rolling")
        self.metric_vars["confidence"][0].set(str(state["confidence"]))
        self.metric_vars["confidence"][1].set("auto-join ON" if state["auto_join_enabled"] else "auto-join OFF")
        self.metric_vars["votes"][0].set(str(stats["votes"]["last_24h"]))
        self.metric_vars["votes"][1].set(f"{stats['votes']['last_7d']} in 7d")
        self.metric_vars["health"][0].set(f"{round((1 - stats['polling']['failure_rate_loaded']) * 100)}%")
        self.metric_vars["health"][1].set(
            f"{stats['forecast']['readiness']['phase']} | quality {round(stats['forecast']['data_quality']['score'])}%"
        )
        self.sync_poll_interval_entry(state)
        self.auto_button.configure(text="Auto-Join ON" if state["auto_join_enabled"] else "Auto-Join OFF")
        self._set_rows(
            "velocity",
            [
                (f"{row['minutes']}m", row["votes"], format_rate(row["velocity_per_minute"]), format_duration(row["eta_seconds"]))
                for row in stats["velocity_windows"]
            ],
        )
        self._set_rows(
            "sources",
            [
                (
                    row["source"],
                    row["votes"],
                    row["ok_polls"],
                    row["failed_polls"],
                    row["skipped_polls"],
                    row["reader_polls"],
                    f"{round(row['success_rate'] * 100)}%",
                )
                for row in stats["source_mix"]["last_24h"]
            ],
        )
        self._set_rows(
            "trusted_sources",
            [
                (
                    row["source"],
                    f"{row['score'] * 100:.0f}%",
                    f"{row['success_rate'] * 100:.0f}%",
                    format_duration(row["freshness_seconds"]),
                    format_duration(row["median_delay_seconds"]),
                    row["votes"],
                )
                for row in stats["source_mix"]["trusted_ranking"]
            ],
        )
        self._set_rows(
            "last_voters",
            [
                (
                    row["username"],
                    row["source"],
                    row.get("vote_time_local") or "--",
                    row.get("detected_at_local") or "--",
                )
                for row in history["latest_voters"][:30]
            ],
        )
        self._set_rows(
            "voter_stats",
            [
                (row["username"], row["votes_seen"], row["sources_seen"], row.get("last_vote_time_local") or "--")
                for row in stats["voters"]["top_24h"]
            ],
        )
        overlap = stats["voters"]["overlap"]
        self._set_rows(
            "voter_overlap",
            [
                (
                    row["username"],
                    row["votes_seen"],
                    row["sources_seen"],
                    f"{row['source_share'] * 100:.0f}%",
                    row.get("last_seen_local") or "--",
                )
                for row in overlap["overlap_users"]
            ],
        )
        self._set_rows(
            "voter_streaks",
            [
                (
                    row["username"],
                    row["active_days"],
                    row["votes_seen"],
                    row.get("last_seen_local") or "--",
                )
                for row in stats["voters"]["streaks"]
            ],
        )
        self._set_rows(
            "cycles",
            [
                (local_time_short(row["ended_at"]), row["total_delta"], row["estimate_after"], row["successes"], row["failures"])
                for row in history["cycles"][-24:][::-1]
            ],
        )
        trusted = stats["source_mix"]["trusted_ranking"][0] if stats["source_mix"]["trusted_ranking"] else {}
        diagnostics = stats["source_debug"]["last_7d"]
        worst_diag = min(diagnostics, key=lambda row: row["health_score"]) if diagnostics else {}
        solo_votes = sum(int(row["solo_votes"]) for row in diagnostics)
        catchup_votes = sum(int(row["catchup_votes"]) for row in diagnostics)
        advanced_rows = [
            ("Auto-join", "ON" if state["auto_join_enabled"] else "OFF"),
            ("Next poll", format_duration(state.get("next_poll_seconds"))),
            (
                "Poll interval",
                f"{format_duration(state.get('poll_interval_seconds'))} "
                f"({'manual' if state.get('poll_interval_override_seconds') is not None else 'dynamic'})",
            ),
            ("Peak hour", hour_label(stats["hours"]["peak_hours_utc"][0]) if stats["hours"]["peak_hours_utc"] else "--"),
            ("Quiet hour", hour_label(stats["hours"]["quiet_hours_utc"][0]) if stats["hours"]["quiet_hours_utc"] else "--"),
            ("Vote bursts", stats["bursts"]["count_loaded"]),
            ("Max burst size", stats["bursts"]["max_burst_size"]),
            ("Burst freq/day", f"{stats['bursts']['frequency_per_day']:.2f}"),
            ("Avg estimate error", format_optional(stats["estimate_error"]["average_error"])),
            ("Worst estimate error", format_optional(stats["estimate_error"]["worst_error"])),
            ("Drift/hour", format_optional(stats["estimate_error"]["drift_per_hour"])),
            ("Downtime gaps", stats["downtime"]["gap_count"]),
            ("Missed votes est.", f"{stats['downtime']['missed_vote_estimate']:.1f}"),
            ("Most trusted", trusted.get("source", "--")),
            ("Likely full-site voters", len(overlap["likely_full_site_voters"])),
            ("Worst source", worst_diag.get("source", "--")),
            ("Worst source score", f"{round(worst_diag.get('health_score', 0))}%" if worst_diag else "--"),
            ("Solo-source votes", solo_votes),
            ("Catchup votes", catchup_votes),
            ("Source issues 7d", stats["source_debug"]["issues_7d_count"]),
        ]
        self._set_rows("advanced", advanced_rows)
        hour_rows = []
        probability = {row["hour_utc"]: row for row in stats["hours"]["party_probability_by_hour_utc"]}
        for row in stats["hours"]["peak_hours_utc"][:3] + stats["hours"]["quiet_hours_utc"][:3]:
            prob = probability.get(row["hour_utc"], {}).get("probability", 0.0)
            hour_rows.append((hour_label(row), row["votes"], f"{prob * 100:.1f}%"))
        self._set_rows("hours", hour_rows)
        self._set_rows(
            "diagnostics",
            [
                (
                    row["source"],
                    f"{round(row['success_rate'] * 100)}%",
                    row["failed_polls"],
                    row["skipped_polls"],
                    row["solo_votes"],
                    row["catchup_votes"],
                    row["suppressed_votes"],
                    format_duration(row["stale_seconds"]),
                    f"{round(row['health_score'])}%",
                )
                for row in diagnostics
            ],
        )
        self._set_rows(
            "delta_trace",
            [
                (
                    local_time_short(row["ended_at"]),
                    row["total_delta"],
                    row["positive_detail"] or "--",
                    row["note"],
                )
                for row in history["source_delta_trace"][:40]
            ],
        )
        self._set_rows(
            "source_issues",
            [
                (
                    local_time_short(row["checked_at"]),
                    row["source"],
                    "skip" if row["skipped"] else "fail",
                    row["error"],
                )
                for row in history["source_issue_events"][:40]
            ],
        )
        self._set_rows(
            "bursts",
            [
                (
                    local_time_short(row["ended_at"]),
                    row["votes"],
                    row["estimate_after"],
                )
                for row in stats["bursts"]["recent_bursts"][::-1]
            ],
        )
        self._set_rows(
            "party_history",
            [
                (
                    local_time_short(row["estimated_at"]),
                    row["estimate_before"],
                    row["estimate_after"],
                    row["delta"],
                    row["confidence"],
                )
                for row in history["vote_party_events"][-40:][::-1]
            ],
        )
        interval_stats = stats["party_intervals"]
        self._set_rows(
            "intervals",
            [
                ("Observed intervals", interval_stats["count"]),
                ("Average interval", format_duration(interval_stats["mean"])),
                ("Median interval", format_duration(interval_stats["median"])),
                ("Fastest interval", format_duration(interval_stats["min"])),
                ("Slowest interval", format_duration(interval_stats["max"])),
                ("P90 interval", format_duration(interval_stats["p90"])),
            ],
        )
        self._set_rows(
            "calibration_log",
            [
                (
                    row.get("calibrated_at_local") or local_time_label(row["calibrated_at"]) or "--",
                    row["previous_estimate"],
                    row["actual_estimate"],
                    f"{int(row['signed_error']):+d}",
                    row["severity"],
                )
                for row in history["calibration_mismatch_log"][:80]
            ],
        )
        latest_error = stats["estimate_error"].get("latest") or {}
        self._set_rows(
            "error_stats",
            [
                ("Latest severity", latest_error.get("severity", "--")),
                ("Latest signed error", latest_error.get("signed_error", "--")),
                ("Latest message", latest_error.get("message", "--")),
                ("Average error", format_optional(stats["estimate_error"]["average_error"])),
                ("Worst error", format_optional(stats["estimate_error"]["worst_error"])),
                ("Drift/hour", format_optional(stats["estimate_error"]["drift_per_hour"])),
                ("Samples", stats["estimate_error"]["absolute"]["count"]),
            ],
        )
        self._set_rows(
            "downtime",
            [
                ("Gap count", stats["downtime"]["gap_count"]),
                ("Longest gap", format_duration(stats["downtime"]["longest_gap_seconds"])),
                ("Total gap", format_duration(stats["downtime"]["total_gap_seconds"])),
                ("Missed votes est.", f"{stats['downtime']['missed_vote_estimate']:.1f}"),
                ("No-vote streak", stats["polling"]["no_vote_cycle_streak"]),
                ("Poll spacing median", format_duration(stats["polling"]["spacing_seconds"]["median"])),
                ("Poll spacing p90", format_duration(stats["polling"]["spacing_seconds"]["p90"])),
            ],
        )
        self.redraw_flow()

    def update_poll_metric(self, state: dict[str, Any]) -> None:
        if self.next_poll_due_at:
            remaining = max(
                0,
                int(math.ceil((self.next_poll_due_at - utc_now()).total_seconds())),
            )
        else:
            remaining = state.get("next_poll_seconds")
        override = state.get("poll_interval_override_seconds")
        interval = state.get("poll_interval_seconds")
        dynamic_interval = state.get("dynamic_poll_interval_seconds")
        mode = "manual" if override is not None else "dynamic"
        if override is not None and dynamic_interval != interval:
            detail = f"every {format_duration(interval)} | {mode}"
        else:
            detail = f"every {format_duration(interval)} | {mode}"
        self.metric_vars["poll"][0].set(format_duration(remaining))
        self.metric_vars["poll"][1].set(detail)

    def sync_poll_interval_entry(self, state: dict[str, Any]) -> None:
        focused = self.root.focus_get()
        if focused is self.poll_interval_entry:
            return
        interval = state.get("poll_interval_override_seconds") or state.get("poll_interval_seconds")
        self.poll_interval_var.set("" if interval is None else str(int(interval)))

    def tick_poll_countdown(self) -> None:
        if self.latest_payload and "poll" in self.metric_vars:
            self.update_poll_metric(self.latest_payload["state"])
        self.root.after(1000, self.tick_poll_countdown)

    def notify_vote_found(self, payload: dict[str, Any]) -> None:
        history = payload.get("history", {})
        cycles = history.get("cycles", [])
        cycle_ids = [
            int(cycle["id"])
            for cycle in cycles
            if cycle.get("id") is not None
        ]
        if not cycle_ids:
            return
        max_cycle_id = max(cycle_ids)
        if self.last_seen_cycle_id is None:
            self.last_seen_cycle_id = max_cycle_id
            return
        new_cycles = [
            cycle
            for cycle in cycles
            if int(cycle.get("id") or 0) > self.last_seen_cycle_id
            and int(cycle.get("total_delta") or 0) > 0
        ]
        self.last_seen_cycle_id = max(max_cycle_id, self.last_seen_cycle_id)
        if not new_cycles or not self.config.get("gui", {}).get("vote_notifications", True):
            return
        amount = sum(int(cycle.get("total_delta") or 0) for cycle in new_cycles)
        latest = max(new_cycles, key=lambda cycle: int(cycle.get("id") or 0))
        detail = self.vote_notification_detail(new_cycles, payload)
        self.show_vote_notification(amount, detail, latest.get("estimate_after"))
        self.play_vote_sound()

    def vote_notification_detail(
        self, cycles: list[dict[str, Any]], payload: dict[str, Any]
    ) -> str:
        trace_by_id = {
            int(row["cycle_id"]): row
            for row in payload.get("history", {}).get("source_delta_trace", [])
            if row.get("cycle_id") is not None
        }
        details = []
        for cycle in cycles[-3:]:
            trace = trace_by_id.get(int(cycle.get("id") or 0))
            if trace and trace.get("positive_detail"):
                details.append(str(trace["positive_detail"]))
        if details:
            return " | ".join(details)
        if len(cycles) == 1:
            return f"1 poll at {local_time_short(cycles[0].get('ended_at'))}"
        return f"{len(cycles)} polls since last refresh"

    def show_vote_notification(
        self, amount: int, detail: str, estimate_after: Any = None
    ) -> None:
        try:
            if self.toast_window:
                self.toast_window.destroy()
        except Exception:
            pass
        window = self.tk.Toplevel(self.root)
        self.toast_window = window
        window.overrideredirect(True)
        window.configure(bg=self.colors["card"])
        try:
            window.attributes("-topmost", True)
            window.attributes("-alpha", 0.0)
        except Exception:
            pass
        frame = self.tk.Frame(window, bg=self.colors["card"], padx=16, pady=14)
        frame.pack(fill="both", expand=True)
        title = "VOTE FOUND" if amount == 1 else "VOTES FOUND"
        self.tk.Label(
            frame,
            text=f"{title}  +{amount}",
            bg=self.colors["card"],
            fg=self.colors["accent"],
            font=("Helvetica", 14, "bold"),
        ).pack(anchor="w")
        subtext = detail or "New vote activity detected"
        if estimate_after is not None:
            subtext = f"{subtext} | estimate {estimate_after}"
        self.tk.Label(
            frame,
            text=subtext[:120],
            bg=self.colors["card"],
            fg=self.colors["text"],
            font=("Helvetica", 11),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(4, 8))
        bar = self.tk.Canvas(frame, height=4, bg=self.colors["line"], highlightthickness=0)
        bar.pack(fill="x")
        rect = bar.create_rectangle(0, 0, 1, 4, fill=self.colors["accent"], outline="")
        window.update_idletasks()
        width = max(window.winfo_width(), 360)
        height = max(window.winfo_height(), 92)
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(self.root.winfo_width(), 900)
        x = root_x + root_w - width - 28
        y = root_y + 92
        window.geometry(f"{width}x{height}+{x}+{y}")

        total_steps = 90

        def animate(step: int = 0) -> None:
            if not window.winfo_exists():
                return
            progress = min(1.0, step / total_steps)
            alpha = 1.0
            if progress < 0.12:
                alpha = progress / 0.12
            elif progress > 0.82:
                alpha = max(0.0, (1.0 - progress) / 0.18)
            try:
                window.attributes("-alpha", alpha)
            except Exception:
                pass
            current_width = max(1, int(width * (1.0 - progress)))
            bar.coords(rect, 0, 0, current_width, 4)
            if step < total_steps:
                self.root.after(45, lambda: animate(step + 1))
            else:
                try:
                    window.destroy()
                except Exception:
                    pass
                if self.toast_window is window:
                    self.toast_window = None

        animate()

    def play_vote_sound(self) -> None:
        if not self.config.get("gui", {}).get("vote_sound", True):
            return
        if sys.platform == "darwin":
            sound_path = "/System/Library/Sounds/Glass.aiff"

            def worker() -> None:
                try:
                    subprocess.run(
                        ["afplay", sound_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=4,
                        check=False,
                    )
                except Exception:
                    self.root.after(0, self.root.bell)

            threading.Thread(target=worker, daemon=True).start()
            return
        self.root.bell()

    def _set_rows(self, table_name: str, rows: list[tuple[Any, ...]]) -> None:
        self.table_rows[table_name] = rows
        tree = self.tables[table_name]
        self._replace_rows(tree, rows)
        popout_tree = self.popout_tables.get(table_name)
        if popout_tree:
            self._replace_rows(popout_tree, rows)

    def _replace_rows(self, tree: Any, rows: list[tuple[Any, ...]]) -> None:
        for item in tree.get_children():
            tree.delete(item)
        for index, row in enumerate(rows):
            tree.insert(
                "",
                "end",
                values=tuple("" if value is None else value for value in row),
                tags=("odd" if index % 2 else "even",),
            )

    def open_table_popout(self, table_name: str, title: str) -> None:
        existing = self.popout_tables.get(table_name)
        if existing:
            try:
                existing.winfo_toplevel().lift()
                return
            except Exception:
                self.popout_tables.pop(table_name, None)
        window = self.tk.Toplevel(self.root)
        window.title(title)
        window.geometry("900x520")
        window.configure(bg=self.colors["bg"])
        frame = self.ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        tree = self._create_tree(frame, self.table_columns.get(table_name, ()), height=18)
        self.popout_tables[table_name] = tree
        self._replace_rows(tree, self.table_rows.get(table_name, []))
        window.protocol("WM_DELETE_WINDOW", lambda: self.close_table_popout(table_name, window))

    def close_table_popout(self, table_name: str, window: Any) -> None:
        self.popout_tables.pop(table_name, None)
        window.destroy()

    def open_flow_popout(self) -> None:
        if self.flow_popout_canvas:
            try:
                self.flow_popout_canvas.winfo_toplevel().lift()
                return
            except Exception:
                self.flow_popout_canvas = None
        window = self.tk.Toplevel(self.root)
        window.title("Vote Flow")
        window.geometry("1100x620")
        window.configure(bg=self.colors["bg"])
        canvas = self.tk.Canvas(window, bg=self.colors["graph_bg"], highlightthickness=0)
        canvas.pack(fill="both", expand=True, padx=12, pady=12)
        self.flow_popout_canvas = canvas
        window.protocol("WM_DELETE_WINDOW", lambda: self.close_flow_popout(window))
        self.redraw_flow()

    def close_flow_popout(self, window: Any) -> None:
        self.flow_popout_canvas = None
        window.destroy()

    def redraw_flow(self) -> None:
        if not self.latest_payload:
            return
        history = self.latest_payload["history"]
        state = self.latest_payload["state"]
        cycles = self.filtered_flow_cycles(history["cycles"])
        self.draw_flow_on_canvas(self.canvas, cycles, state["party_size"])
        if self.flow_popout_canvas:
            self.draw_flow_on_canvas(self.flow_popout_canvas, cycles, state["party_size"])

    def filtered_flow_cycles(self, cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        value = self.flow_window_var.get()
        hours_by_value = {"2h": 2, "6h": 6, "24h": 24, "7d": 168}
        hours = hours_by_value.get(value)
        if not hours:
            return cycles
        cutoff = utc_now() - timedelta(hours=hours)
        filtered = [
            cycle
            for cycle in cycles
            if (parse_iso(cycle.get("ended_at")) or utc_now()) >= cutoff
        ]
        return filtered or cycles[-1:]

    def draw_flow_on_canvas(
        self, canvas: Any, cycles: list[dict[str, Any]], party_size: int
    ) -> None:
        if canvas is None:
            return
        colors = self.colors
        canvas.delete("all")
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 180)
        canvas.create_rectangle(0, 0, width, height, fill=colors["graph_bg"], outline="")
        left = 42
        right = width - 16
        top = 20
        bottom = height - 42
        for idx in range(5):
            y = top + idx * ((bottom - top) / 4)
            canvas.create_line(left, y, right, y, fill=colors["line"])
        if not cycles:
            return
        max_delta = max(1, *(int(cycle.get("total_delta") or 0) for cycle in cycles))
        step = (right - left) / max(1, len(cycles))
        for idx, cycle in enumerate(cycles):
            delta = int(cycle.get("total_delta") or 0)
            x = left + idx * step
            bar_h = (delta / max_delta) * (bottom - top)
            canvas.create_rectangle(
                x,
                bottom - bar_h,
                x + max(1, min(8, step * 0.55)),
                bottom,
                fill=colors["warning"],
                outline="",
            )
        points: list[float] = []
        for idx, cycle in enumerate(cycles):
            x = left + idx * step + step * 0.3
            y = top + (1 - (int(cycle.get("estimate_after") or 0) / max(1, party_size))) * (bottom - top)
            points.extend([x, y])
        if len(points) >= 4:
            canvas.create_line(*points, fill=colors["accent"], width=2)
        cumulative = 0
        cumulative_points: list[float] = []
        total_votes = max(1, sum(int(cycle.get("total_delta") or 0) for cycle in cycles))
        for idx, cycle in enumerate(cycles):
            cumulative += int(cycle.get("total_delta") or 0)
            x = left + idx * step + step * 0.3
            y = bottom - (cumulative / total_votes) * (bottom - top)
            cumulative_points.extend([x, y])
        if len(cumulative_points) >= 4:
            canvas.create_line(*cumulative_points, fill=colors["blue"], width=2)
        canvas.create_text(left, 10, text=f"max delta {max_delta}", fill=colors["muted"], anchor="w")
        canvas.create_text(
            right,
            10,
            text=f"{len(cycles)} polls | {total_votes} votes",
            fill=colors["muted"],
            anchor="e",
        )
        canvas.create_text(left, bottom + 14, text="bars=delta", fill=colors["warning"], anchor="w")
        canvas.create_text(left + 92, bottom + 14, text="green=VP", fill=colors["accent"], anchor="w")
        canvas.create_text(left + 170, bottom + 14, text="blue=trend", fill=colors["blue"], anchor="w")
        label_indexes = sorted(
            {
                0,
                max(0, len(cycles) // 4),
                max(0, len(cycles) // 2),
                max(0, (len(cycles) * 3) // 4),
                len(cycles) - 1,
            }
        )
        for idx in label_indexes:
            cycle = cycles[idx]
            x = left + idx * step
            canvas.create_line(x, bottom, x, bottom + 4, fill=colors["line"])
            canvas.create_text(
                x,
                bottom + 28,
                text=local_time_axis_label(cycle.get("ended_at")),
                fill=colors["muted"],
                anchor="center",
                font=("Menlo", 9),
            )

    def calibrate(self) -> None:
        try:
            count = int(self.calibrate_var.get())
        except ValueError:
            self.status_var.set("Calibration must be a number")
            return
        with self.store.lock:
            self.store.calibrate(count, source="desktop")
            publish_next_poll_due(self.store, utc_now(), count)
        self.refresh()

    def poll_now(self) -> None:
        def worker() -> None:
            try:
                with self.store.lock:
                    cycle = poll_sources(self.store, self.config)
                    publish_next_poll_due(
                        self.store, cycle.ended_at, cycle.estimate_after
                    )
                self.root.after(0, self.refresh)
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f"Poll failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def apply_poll_interval(self) -> None:
        raw = self.poll_interval_var.get().strip()
        try:
            seconds = int(raw)
        except ValueError:
            self.status_var.set("Poll interval must be seconds")
            return
        if seconds < 5:
            self.status_var.set("Poll interval minimum is 5 seconds")
            return
        if seconds > 86400:
            self.status_var.set("Poll interval maximum is 86400 seconds")
            return
        with self.store.lock:
            state = self.store.get_state()
            self.store.set_poll_interval_override_seconds(seconds)
            publish_next_poll_due(
                self.store,
                utc_now(),
                int(state["current_estimate"]),
            )
        self.status_var.set(f"Manual poll interval set to {seconds}s")
        self.refresh()

    def clear_poll_interval(self) -> None:
        with self.store.lock:
            state = self.store.get_state()
            self.store.clear_poll_interval_override_seconds()
            publish_next_poll_due(
                self.store,
                utc_now(),
                int(state["current_estimate"]),
            )
        self.status_var.set("Poll interval returned to dynamic mode")
        self.refresh()

    def toggle_auto_join(self) -> None:
        with self.store.lock:
            self.store.set_auto_join_enabled(not self.store.auto_join_enabled())
        self.refresh()


def format_duration(seconds: Any) -> str:
    if seconds is None:
        return "--"
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_rate(value: Any) -> str:
    if value is None:
        return "--"
    return f"{float(value):.2f}/m"


def format_optional(value: Any, digits: int = 2) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def hour_label(row: dict[str, Any]) -> str:
    return f"{int(row['hour_utc']):02d}:00"


def local_time_short(value: str | None) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return "--"
    return parsed.astimezone().strftime("%H:%M:%S")


def local_time_axis_label(value: str | None) -> str:
    parsed = parse_iso(value)
    if not parsed:
        return "--"
    local = parsed.astimezone()
    return local.strftime("%m-%d %H:%M")


def run_gui(store: Store, config: dict[str, Any]) -> None:
    DesktopDashboard(store, config).run()


def reset_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Track vote pages and time short Minecraft joins."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to config JSON (default: config.json)",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Write a default config file and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite config with --init-config.",
    )
    parser.add_argument(
        "--calibrate",
        type=int,
        help="Set current in-game VP count, e.g. --calibrate 73.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Poll all sources once, update state, and exit.",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously using dynamic polling intervals.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current tracker state and recent snapshots.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print the dashboard statistics payload as JSON.",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Print service health as JSON and exit non-zero if stale/unhealthy.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Run the native desktop dashboard.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Run the local browser dashboard instead of the native desktop dashboard.",
    )
    parser.add_argument(
        "--gui-host",
        help="Web dashboard bind host override (default from config: 127.0.0.1).",
    )
    parser.add_argument(
        "--gui-port",
        type=int,
        help="Web dashboard port override (default from config: 8765).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the web dashboard URL in a browser.",
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Delete the SQLite database before doing anything else.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config_path = Path(args.config)

    if args.init_config:
        write_default_config(config_path, force=args.force)
        print(f"Wrote {config_path}")
        return 0

    config = load_config(config_path)
    if args.gui_host:
        config.setdefault("gui", {})["host"] = args.gui_host
    if args.gui_port:
        config.setdefault("gui", {})["port"] = args.gui_port
    if args.no_open:
        config.setdefault("gui", {})["open_browser"] = False
    db_path = Path(config.get("database_path", DEFAULT_DB_PATH))
    if args.reset_db:
        reset_db(db_path)
        print(f"Reset {db_path}")

    store = Store(db_path, config)
    try:
        if args.calibrate is not None:
            store.calibrate(args.calibrate)
            print(f"Calibrated VP estimate to {args.calibrate % int(config['vp_party_size'])}/120")

        if args.status:
            print_status(store, config)

        if args.stats:
            print(json.dumps(dashboard_to_dict(store.dashboard_snapshot()), indent=2))

        if args.healthcheck:
            return run_healthcheck(store, config)

        if args.once:
            run_once(store, config)

        web_server: ThreadingHTTPServer | None = None
        if args.web and args.daemon:
            web_server = run_web_gui(store, config, block=False)

        daemon_thread: threading.Thread | None = None
        if args.gui and args.daemon:
            daemon_thread = threading.Thread(
                target=run_daemon,
                args=(store, config),
                daemon=True,
            )
            daemon_thread.start()
            run_gui(store, config)
            return 0

        if args.daemon:
            try:
                run_daemon(store, config)
            finally:
                if web_server:
                    web_server.shutdown()

        if args.web and not args.daemon:
            run_web_gui(store, config, block=True)

        if args.gui and not args.daemon:
            run_gui(store, config)

        if not any(
            [
                args.calibrate is not None,
                args.status,
                args.stats,
                args.healthcheck,
                args.once,
                args.daemon,
                args.gui,
                args.web,
            ]
        ):
            build_arg_parser().print_help()
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
