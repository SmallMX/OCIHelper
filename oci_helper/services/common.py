"""Shared execution boundary for synchronous OCI SDK calls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

from loguru import logger

from exceptions import OciException

T = TypeVar("T")


async def run_oci(operation: Callable[[], T], message: str) -> T:
    """Run an OCI call off the event loop and return a sanitized business error."""
    try:
        return await asyncio.to_thread(operation)
    except OciException:
        raise
    except Exception as exc:
        logger.warning("{}: {}", message, exc)
        raise OciException(-1, f"{message}: {safe_error(exc)}") from exc


def safe_error(value: object, max_length: int = 500) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()[:max_length]
