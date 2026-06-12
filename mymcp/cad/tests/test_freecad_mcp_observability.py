from __future__ import annotations

import json

from freecad_mcp_observability import classify_failure, observe_mcp_tool


def test_classify_failure_common_buckets() -> None:
    assert classify_failure("file_not_found:CAD file not found") == "path_or_upload_error"
    assert classify_failure("Failed to connect to FreeCAD") == "freecad_connection_error"
    assert classify_failure("Invalid project file") == "cad_import_error"
    assert classify_failure("AttributeError: object has no attribute Shape") == "freecad_api_error"


def test_observe_mcp_tool_writes_jsonl(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CAD_MCP_RUN_LOG_DIR", str(tmp_path))

    @observe_mcp_tool("sample_tool")
    def sample_tool() -> str:
        return json.dumps({"success": False, "error": "document_not_found:Document not found"})

    assert json.loads(sample_tool())["success"] is False

    logs = list(tmp_path.glob("mcp_runs_*.jsonl"))
    assert len(logs) == 1
    event = json.loads(logs[0].read_text(encoding="utf-8").strip())
    assert event["tool"] == "sample_tool"
    assert event["success"] is False
    assert event["failure_class"] == "document_state_error"
