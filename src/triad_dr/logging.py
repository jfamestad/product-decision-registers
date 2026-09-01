"""Structured logging configuration for triad_dr.

**stdout is the MCP protocol channel.** Under the stdio transport, anything
written to stdout that is not a JSON-RPC frame corrupts the stream and the
client disconnects. structlog's output destination depends on its default
logger factory, which is not part of its stable API — relying on it happening
to be stderr is a latent protocol bug. This module pins it explicitly.

Import for side effects, or call `configure_logging()` directly. It is
idempotent and safe to call from any entry point.
"""

from __future__ import annotations

import logging
import sys

import structlog

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog to emit to stderr only.

    Idempotent: repeated calls after the first are no-ops, so every entry point
    (server, CLI, tests) can call it without fighting over configuration.

    Args:
        level: Minimum log level to emit. Defaults to ``logging.INFO``.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # The load-bearing line: stderr, never stdout.
        logger_factory=structlog.WriteLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True
