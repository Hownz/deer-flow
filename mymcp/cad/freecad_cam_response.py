"""Shared response helpers for CAM MCP tools."""

from __future__ import annotations

import json
from typing import Any


def success_response(message: str, data: dict[str, Any] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "message": message,
        "data": data or {},
        "warnings": warnings or [],
    }


def error_response(
    error_code: str,
    message: str,
    detail: str | None = None,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "message": message,
        "error_code": error_code,
        "error_detail": detail or "",
        "data": data or {},
        "warnings": warnings or [],
    }


def dump_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)

