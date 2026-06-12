"""Structured observability helpers for FreeCAD MCP tools.

The goal is to make CAD/CAM failures replayable without changing existing tool
contracts. Tool functions still return their original JSON strings; this module
only appends compact JSONL events for later diagnosis and controlled patches.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, TypeVar

logger = logging.getLogger("FreeCADMCPObservability")

DEFAULT_RUN_LOG_DIR = "/root/GSK/cache/agentCAD/runs"
MAX_FIELD_CHARS = 4000
MAX_ARG_CHARS = 800

F = TypeVar("F", bound=Callable[..., Any])


def classify_failure(message: str) -> str:
    """Classify common FreeCAD/MCP failure messages into maintenance buckets."""
    lowered = (message or "").lower()
    if not lowered:
        return "unknown"
    if any(token in lowered for token in ("file not found", "file_not_found", "no such file")):
        return "path_or_upload_error"
    if any(token in lowered for token in ("failed to connect", "connection refused", "timed out", "timeout", "ping")):
        return "freecad_connection_error"
    if any(token in lowered for token in ("invalid project file", "import.open", "import failed", "cannot import")):
        return "cad_import_error"
    if any(token in lowered for token in ("document_not_found", "document not found", "no active document")):
        return "document_state_error"
    if any(token in lowered for token in ("object_not_found", "object not found", "has no shape", "empty shape")):
        return "object_state_error"
    if any(token in lowered for token in ("no exportable objects", "mesh.export", "import.export", "failed to export")):
        return "export_error"
    if any(token in lowered for token in ("pathscripts", "postprocess", "post_process", "no cam job", "no job")):
        return "cam_runtime_error"
    if any(token in lowered for token in ("attributeerror", "has no attribute", "typeerror", "valueerror")):
        return "freecad_api_error"
    return "unclassified_error"


def current_log_dir() -> Path:
    return Path(os.getenv("CAD_MCP_RUN_LOG_DIR", DEFAULT_RUN_LOG_DIR))


def current_log_path() -> Path:
    day = datetime.now().strftime("%Y%m%d")
    return current_log_dir() / f"mcp_runs_{day}.jsonl"


def _truncate(value: Any, limit: int = MAX_FIELD_CHARS) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "...<truncated>"
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value[:50]]
    if isinstance(value, tuple):
        return [_truncate(item, limit) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _truncate(item, limit) for key, item in list(value.items())[:80]}
    return value


def summarize_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "args": _truncate(args, MAX_ARG_CHARS),
        "kwargs": _truncate(kwargs, MAX_ARG_CHARS),
    }


def parse_tool_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        try:
            payload = json.loads(result)
            return payload if isinstance(payload, dict) else {"raw_result": payload}
        except json.JSONDecodeError:
            return {"raw_result": _truncate(result)}
    if isinstance(result, dict):
        return result
    return {"raw_result": _truncate(str(result))}


def _extract_error_message(payload: dict[str, Any]) -> str:
    for key in ("error", "message", "error_detail", "error_code"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


def build_event(
    tool_name: str,
    call: dict[str, Any],
    payload: dict[str, Any],
    duration_ms: int,
    exception: BaseException | None = None,
) -> dict[str, Any]:
    success = bool(payload.get("success", payload.get("ok", False)))
    error_message = "" if success else _extract_error_message(payload)
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "success": success,
        "duration_ms": duration_ms,
        "call": call,
        "result": _truncate(payload),
    }
    if not success:
        event["failure_class"] = classify_failure(error_message)
        event["error_message"] = _truncate(error_message)
    if exception is not None:
        event["failure_class"] = classify_failure(str(exception))
        event["exception_type"] = type(exception).__name__
        event["traceback"] = _truncate(traceback.format_exc())
    return event


def record_event(event: dict[str, Any]) -> None:
    """Append one JSONL event; logging failures must not break MCP tools."""
    try:
        path = current_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning("Failed to write CAD MCP run event: %s", exc)


def observe_mcp_tool(tool_name: str) -> Callable[[F], F]:
    """Decorate an MCP tool while preserving its external behavior."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            call = summarize_call(args, kwargs)
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                duration_ms = int((perf_counter() - started) * 1000)
                record_event(build_event(tool_name, call, {}, duration_ms, exc))
                raise

            duration_ms = int((perf_counter() - started) * 1000)
            payload = parse_tool_payload(result)
            record_event(build_event(tool_name, call, payload, duration_ms))
            return result

        return wrapper  # type: ignore[return-value]

    return decorator
