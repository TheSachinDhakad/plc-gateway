"""Structured logging: context variables, JSON/console formatters, secret redaction.

Context (plant_id, device_id, driver, tag_id, operation) is carried in ``contextvars`` so
every log line emitted inside :func:`log_context` is tagged automatically, including lines
from asyncio tasks spawned within it.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.config
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

CONTEXT_FIELDS = ("plant_id", "device_id", "driver", "tag_id", "operation")
_ctx: dict[str, contextvars.ContextVar[str | None]] = {
    name: contextvars.ContextVar(f"log_{name}", default=None) for name in CONTEXT_FIELDS
}
EXTRA_FIELDS = ("error_code", "duration_ms", "request_id", "principal")


@contextlib.contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    tokens = []
    for key, value in values.items():
        if key not in _ctx:
            raise KeyError(f"unknown log context field {key!r}")
        tokens.append((_ctx[key], _ctx[key].set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def bind_context(**values: str | None) -> None:
    """Set context for the remainder of the current task (used at task start)."""
    for key, value in values.items():
        _ctx[key].set(value)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _ctx.items():
            if not hasattr(record, name):
                setattr(record, name, var.get())
        return True


_SECRET_KEYS = re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization)")
_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s,;'\"]+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)?\s*)(\S+)"),
    re.compile(r"(?i)(x-api-key\s*[:=]\s*)(\S+)"),
    re.compile(r"(mysql(?:\+\w+)?://[^:/\s]+:)([^@\s]+)(@)"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 3:
            text = pattern.sub(r"\1***\3", text)
        else:
            text = pattern.sub(r"\1***", text)
    return text


class RedactingFilter(logging.Filter):
    """Redacts secrets from the message, args and extra fields."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args: keep raw message
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        for key in list(record.__dict__):
            if _SECRET_KEYS.search(key) and key not in ("msg",):
                record.__dict__[key] = "***"
        return True


def _base_fields(record: logging.LogRecord) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "logger": record.name,
    }
    for name in (*CONTEXT_FIELDS, *EXTRA_FIELDS):
        value = getattr(record, name, None)
        if value is not None:
            data[name] = value
    data["message"] = record.getMessage()
    return data


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = _base_fields(record)
        if record.exc_info:
            data["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(data, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = _base_fields(record)
        ctx = " ".join(
            f"{k}={data[k]}" for k in (*CONTEXT_FIELDS, *EXTRA_FIELDS) if k in data
        )
        line = f"{data['ts']} {data['level']:<7} {data['logger']}: {data['message']}"
        if ctx:
            line += f" [{ctx}]"
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def setup_logging(logging_yaml: Path | None, log_dir: Path, *, json_console: bool = False) -> None:
    """Configure logging from ``logging.yaml`` (with ``{log_dir}`` substitution)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    if logging_yaml is not None and logging_yaml.exists():
        text = logging_yaml.read_text(encoding="utf-8").replace("{log_dir}", log_dir.as_posix())
        cfg = yaml.safe_load(text)
        if json_console:
            cfg["handlers"]["console"]["formatter"] = "json"
        logging.config.dictConfig(cfg)
        return
    handler = logging.StreamHandler()
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactingFilter())
    handler.setFormatter(JsonFormatter() if json_console else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
