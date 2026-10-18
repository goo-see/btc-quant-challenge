"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import logging
import queue
import re
import sys
from collections.abc import MutableMapping
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from typing import Any

import structlog

_SECRET_KEYS = re.compile(r"(secret|access_key|authorization|token|password|jwt)", re.IGNORECASE)
# JWTs and long base64/hex-ish blobs that look like keys
_SECRET_VALUE = re.compile(r"(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _SECRET_KEYS.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    if isinstance(value, str):
        return _SECRET_VALUE.sub("***JWT***", value)
    return value


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {k: redact(v) for k, v in event_dict.items()}


_listener: QueueListener | None = None


def configure_logging(
    level: str = "INFO",
    log_dir: Path | None = None,
    json_output: bool = True,
    *,
    log_file: str = "trader.jsonl",
) -> None:
    """Non-blocking logging: records go to a queue; a background thread does the I/O.

    A blocked stdout pipe (or slow disk) must never stall the asyncio trading loop — observed on
    2026-09-24 when a backgrounded process froze for 15 minutes on synchronous stdout writes.
    """
    global _listener
    sinks: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        sinks.append(logging.FileHandler(log_dir / log_file, encoding="utf-8"))
    for h in sinks:
        h.setFormatter(logging.Formatter("%(message)s"))
    if _listener is not None:
        _listener.stop()
    q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=10000)
    _listener = QueueListener(q, *sinks, respect_handler_level=True)
    _listener.start()
    qh = QueueHandler(q)
    qh.setFormatter(logging.Formatter("%(message)s"))  # QueueHandler pre-formats; keep raw
    logging.basicConfig(level=level, handlers=[qh], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is too noisy
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def shutdown_logging() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
