"""Small, dependency-free helpers shared by the application."""

from __future__ import annotations

import ipaddress
import uuid
from datetime import datetime
from typing import Any

DATETIME_FMT_NORM = "%Y-%m-%d %H:%M:%S"


def generate_id() -> str:
    """Return a random identifier suitable for database primary keys."""
    return uuid.uuid4().hex


def paginate_list(data_list: list[Any], page: int, page_size: int) -> tuple[list[Any], int]:
    """Return a one-based page and the unfiltered item count."""
    total = len(data_list)
    start = (page - 1) * page_size
    return data_list[start : start + page_size], total


def parse_config_content(config_str: str) -> dict[str, str]:
    """Parse the key/value subset of an OCI CLI configuration file."""
    result: dict[str, str] = {}
    for raw_line in config_str.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def get_client_ip(request: Any) -> str:
    """Return the peer IP, optionally honoring explicitly trusted proxy headers."""
    from config import settings

    if settings.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            candidate = forwarded.split(",", maxsplit=1)[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass

        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            try:
                return str(ipaddress.ip_address(real_ip.strip()))
            except ValueError:
                pass

    return request.client.host if request.client else "unknown"


def get_time_difference(start_time: datetime) -> str:
    """Format a human-readable duration from ``start_time`` until now."""
    diff = datetime.now() - start_time
    parts = []
    if diff.days:
        parts.append(f"{diff.days}天")
    hours = diff.seconds // 3600
    if hours:
        parts.append(f"{hours}小时")
    minutes = (diff.seconds % 3600) // 60
    if minutes:
        parts.append(f"{minutes}分钟")
    return "".join(parts) or "刚刚"
