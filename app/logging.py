"""structlog configuration for the whole application."""

import logging
import sys

import structlog


def configure_logging(*, json_logs: bool = False, level: int = logging.INFO) -> None:
    """Configure structlog once at startup.

    Args:
        json_logs: Emit JSON lines (production) instead of pretty console output.
        level: Root stdlib logging level; APScheduler/uvicorn logs flow through it.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
