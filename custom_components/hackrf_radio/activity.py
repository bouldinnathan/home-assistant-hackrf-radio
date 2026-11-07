"""Bounded, durable, redacted activity history. Never gate USB on log storage."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
LEVELS = ("debug", "info", "warning", "error")
SECRET_NAME = re.compile(r"token|password|secret|api.?key|authorization|cookie", re.I)
CURRENT_OPERATION = ContextVar("hackrf_activity_operation", default=None)


def diagnostic_output(tail: bytearray, truncated: bool) -> str:
    if truncated:
        # Discard the incomplete first line: it could start halfway through a
        # credential and evade value/header redaction.
        if b"\n" not in tail:
            return "Tool output exceeded the diagnostic line limit; inspect host logs"
        tail = tail[tail.index(b"\n") + 1:]
    return tail.decode("utf-8", errors="replace") or "Tool reported an unsuccessful exit"


def redact(value: object, secrets=()) -> str:
    text = str(value)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = re.sub(r"https?://[^\s<>\"']+", "[url redacted]", text, flags=re.I)
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key)|authorization|cookie)[\"']?\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"\b[0-9a-fA-F]{32}\b", "[identifier redacted]", text)
    return "".join(c for c in text if c == "\t" or not unicodedata.category(c).startswith("C"))[:1000]


class ActivityLog:
    """Each transaction uses its own connection, safe across event loops/threads.

    FULL synchronous rollback journals make a committed record restart-safe.
    Retention is enforced in the same transaction. A fixed SQLite page limit
    bounds disk usage even with hostile child output. Storage failures degrade
    history, never interrupt a flash, and produce one ordinary logger warning.
    """

    def __init__(self, path: Path | None, source: str = "usb_host", secrets=(), retention: int = 2000):
        if type(retention) is not int or not 1 <= retention <= 2000:
            raise ValueError("Activity retention must be 1–2000 records")
        self.path = Path(path) if path is not None else None
        self.source = source
        self.secrets = tuple(secrets) + tuple(v for k, v in os.environ.items() if SECRET_NAME.search(k) and v)
        self.retention = retention
        self.important_retention = max(1, retention // 4)
        self._lock = threading.Lock()
        self._error = None
        self._recent = {}

    def _connect(self):
        if self.path is None:
            raise OSError("Configure persistent application or firmware storage for USB-host logs")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not follow a substituted database symlink.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        connection = sqlite3.connect(self.path, timeout=3)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA max_page_count=8192")
            connection.execute("CREATE TABLE IF NOT EXISTS activity (seq INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS important (seq INTEGER PRIMARY KEY, record TEXT NOT NULL)")
        except BaseException:
            connection.close()
            raise
        return connection

    def _failed(self):
        if self._error is None:
            _LOGGER.warning("HackRF activity storage unavailable; inspect disk space, permissions and database health")
        self._error = "Activity storage unavailable; inspect disk space, permissions and database health. Device safeguards remain active."

    async def record(self, event: str, message: str, *, level: str = "info", operation_id: str | None = None,
                     details: dict | None = None, deduplicate: bool = False) -> dict | None:
        if level not in LEVELS or not re.fullmatch(r"[a-z0-9_.-]{1,80}", event):
            raise ValueError("Invalid activity event or level")
        safe = redact(message, self.secrets).encode("utf-8")[:2048].decode("utf-8", errors="ignore")
        now = time.monotonic()
        key = (event, safe)
        if deduplicate and now - self._recent.get(key, -1000) < 60:
            return None
        self._recent[key] = now
        if len(self._recent) > 256:
            self._recent = dict(list(self._recent.items())[-256:])
        record = {"id": uuid4().hex, "timestamp": datetime.now(UTC).isoformat(), "source": self.source,
                  "level": level, "event": event, "message": safe}
        operation_id = operation_id or CURRENT_OPERATION.get() or uuid4().hex
        if operation_id and re.fullmatch(r"[0-9a-f]{32}", operation_id):
            record["operation_id"] = operation_id
        if details:
            record["details"] = {redact(k, self.secrets)[:40]: "[redacted]" if SECRET_NAME.search(str(k)) else redact(v, self.secrets)[:120]
                                 for k, v in list(details.items())[:12]}
            while len(json.dumps(record["details"], ensure_ascii=False).encode("utf-8")) > 2048:
                record["details"].popitem()

        def write():
            with self._lock:
                try:
                    connection = self._connect()
                    try:
                        with connection:
                            connection.execute("DELETE FROM activity WHERE seq <= (SELECT COALESCE(MAX(seq),0)-? FROM activity)", (self.retention - 1,))
                            raw = json.dumps(record, ensure_ascii=False)
                            cursor = connection.execute("INSERT INTO activity(record) VALUES (?)", (raw,))
                            if level in {"warning", "error"}:
                                connection.execute("INSERT INTO important(seq,record) VALUES (?,?)", (cursor.lastrowid, raw))
                                connection.execute("DELETE FROM important WHERE seq NOT IN (SELECT seq FROM important ORDER BY seq DESC LIMIT ?)", (self.important_retention,))
                        self._error = None
                    finally:
                        connection.close()
                except (OSError, sqlite3.Error):
                    self._failed()
        await asyncio.to_thread(write)
        # Routine sweeps/output stay in the activity view, without flooding HA's
        # global log. Failures have a stable redacted message in both places.
        if level in {"warning", "error"}:
            _LOGGER.log(logging.ERROR if level == "error" else logging.WARNING, "%s: %s", event, safe)
        return record

    async def read(self, *, level: str = "all", search: str = "", limit: int = 200, before: int = 0) -> dict:
        if level not in (*LEVELS, "all") or type(limit) is not int or not 1 <= limit <= 500 or type(before) is not int or before < 0:
            raise ValueError("Invalid log filter")
        if not isinstance(search, str) or len(search) > 120:
            raise ValueError("Log search is too long")

        def read():
            with self._lock:
                records = []
                try:
                    connection = self._connect()
                    try:
                        # Fixed retention bounds the work; filtering never treats
                        # user input as SQL or a regular expression.
                        rows = connection.execute("SELECT seq,record FROM (SELECT seq,record FROM activity UNION SELECT seq,record FROM important) "
                                                  "WHERE (?=0 OR seq<?) ORDER BY seq DESC LIMIT ?",
                                                  (before, before, self.retention + self.important_retention)).fetchall()
                        size = 0
                        for sequence, raw in rows:
                            record = json.loads(raw)
                            if (level == "all" or record["level"] == level) and search.casefold() in raw.casefold():
                                size += len(json.dumps(record).encode("utf-8")) + 64
                                if records and size > 1_500_000:
                                    break
                                records.append(record | {"sequence": sequence})
                                if len(records) == limit:
                                    break
                    finally:
                        connection.close()
                except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
                    self._failed()
                return {"records": records, "next_before": records[-1]["sequence"] if records else 0,
                        "storage_error": self._error, "retention": self.retention + self.important_retention,
                        "recent_retention": self.retention, "important_retention": self.important_retention}
        return await asyncio.to_thread(read)
