"""Structured logging: console renderer locally, JSON lines in CI."""

from __future__ import annotations

import logging
import os
import sys

import structlog


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    # Quiet noisy libraries; our own events are what matter in the run log.
    for noisy in ("httpx", "httpcore", "googleapiclient", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if in_ci else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
