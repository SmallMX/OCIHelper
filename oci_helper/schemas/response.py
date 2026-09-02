"""Factories for the API's stable response envelope."""

from __future__ import annotations

from typing import Any


class ResponseData:
    """Build JSON-serializable success and failure response dictionaries."""

    @staticmethod
    def success(data: Any = None, message: str = "请求成功") -> dict[str, Any]:
        return {
            "success": True,
            "code": 200,
            "data": data,
            "msg": message,
        }

    @staticmethod
    def fail(message: str = "请求失败", code: int = -1) -> dict[str, Any]:
        return {
            "success": False,
            "code": code,
            "data": None,
            "msg": message,
        }
