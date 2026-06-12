"""
FreeCAD SSE MCP Server

这是一个使用 SSE 传输方式的 FreeCAD MCP 服务器。
基于原有的 stdio 版本 server.py 转换而来。
"""

import http.client
import json
import logging
import os
import xmlrpc.client
import base64
import yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Union, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent
from freecad_cam_tools import register_cam_tools
from freecad_mcp_observability import classify_failure, observe_mcp_tool, record_event

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("FreeCADSSEMCPserver")


# FreeCAD 连接配置
# 192.168.0.88
freecad_addr = os.getenv("FREECAD_RPC_HOST", "127.0.0.1") 
freecad_port = int(os.getenv("FREECAD_RPC_PORT", "9875"))
_freecad_connection = None

# SSE 服务器配置
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8088
REMOTE_CAD_UPLOAD_DIR = os.getenv(
    "FREECAD_REMOTE_CAD_UPLOAD_DIR",
    os.getenv("FREECAD_REMOTE_FCSTD_UPLOAD_DIR", "/root/GSK/cache/agentCAD/uploads"),
)
SUPPORTED_CAD_OPEN_EXTENSIONS = {".fcstd", ".step", ".stp", ".igs", ".iges", ".dwg"}


class TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport that enforces socket timeout on both connect and read.

    The standard xmlrpc.client.Transport passes timeout to HTTPConnection,
    which uses it for TCP connect (via socket.create_connection). However,
    on reused connections the socket timeout may not be re-applied, and
    some platform/FreeCAD combinations can cause the read to hang forever
    even when a connect timeout is configured.

    This transport guarantees that self.timeout is always applied to the
    underlying socket before each request, covering both new and reused
    HTTP connections.
    """

    def make_connection(self, host):
        if self._connection and host == self._connection[0]:
            # Reused connection: re-assert the timeout on the existing socket
            conn = self._connection[1]
            if conn.sock:
                conn.sock.settimeout(self.timeout)
            return conn
        chost, self._extra_headers, x509 = self.get_host_info(host)
        conn = http.client.HTTPConnection(chost, timeout=self.timeout)
        self._connection = host, conn
        return conn


class FreeCADConnection:
    def __init__(self, host: str = freecad_addr, port: int = freecad_port):
        transport = TimeoutTransport()
        transport.timeout = 30

        self.server = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}",
            allow_none=True,
            transport=transport
        )

    def ping(self) -> bool:
        return self.server.ping()

    def create_document(self, name: str) -> dict[str, Any]:
        return self.server.create_document(name)

    def create_object(self, doc_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.create_object(doc_name, obj_data)

    def edit_object(self, doc_name: str, obj_name: str, obj_data: dict[str, Any]) -> dict[str, Any]:
        return self.server.edit_object(doc_name, obj_name, obj_data)

    def delete_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.delete_object(doc_name, obj_name)

    def insert_part_from_library(self, relative_path: str) -> dict[str, Any]:
        return self.server.insert_part_from_library(relative_path)

    def execute_code(self, code: str) -> dict[str, Any]:
        return self.server.execute_code(code)

    def get_active_screenshot(self, view_name: str = "Isometric") -> str | None:
        try:
            # Check if we're in a view that supports screenshots
            result = self.server.execute_code("""
import FreeCAD
import FreeCADGui

if FreeCAD.Gui.ActiveDocument and FreeCAD.Gui.ActiveDocument.ActiveView:
    view_type = type(FreeCAD.Gui.ActiveDocument.ActiveView).__name__
    
    # These view types don't support screenshots
    unsupported_views = ['SpreadsheetGui::SheetView', 'DrawingGui::DrawingView', 'TechDrawGui::MDIViewPage']
    
    if view_type in unsupported_views or not hasattr(FreeCAD.Gui.ActiveDocument.ActiveView, 'saveImage'):
        print("Current view does not support screenshots")
        False
    else:
        print(f"Current view supports screenshots: {view_type}")
        True
else:
    print("No active view")
    False
""")

            # If the view doesn't support screenshots, return None
            if not result.get("success", False) or "Current view does not support screenshots" in result.get("message", ""):
                logger.info("Screenshot unavailable in current view (likely Spreadsheet or TechDraw view)")
                return None

            # Otherwise, try to get the screenshot
            return self.server.get_active_screenshot(view_name)
        except Exception as e:
            # Log the error but return None instead of raising an exception
            logger.error(f"Error getting screenshot: {e}")
            return None

    def get_objects(self, doc_name: str) -> list[dict[str, Any]]:
        try:
            return self.server.get_objects(doc_name)
        except xmlrpc.client.ProtocolError as e:
            logger.error(f"XML-RPC protocol error getting objects: {e}")
            raise
        except Exception as e:
            logger.error(f"Error getting objects: {e}")
            raise

    def get_object(self, doc_name: str, obj_name: str) -> dict[str, Any]:
        return self.server.get_object(doc_name, obj_name)

    def get_parts_list(self) -> list[str]:
        return self.server.get_parts_list()

    def disconnect(self):
        self.server.disconnect()


def get_freecad_connection():
    """Get or create a persistent FreeCAD connection"""
    global _freecad_connection
    if _freecad_connection is None:
        _freecad_connection = FreeCADConnection(host=freecad_addr, port=freecad_port)
        if not _freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            _freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
    return _freecad_connection


def _find_agent_cad_root() -> str:
    """Best-effort discovery of the DeerFlow/AgentCAD project root."""
    global _AGENT_CAD_ROOT
    try:
        if _AGENT_CAD_ROOT is not None:
            return _AGENT_CAD_ROOT
    except NameError:
        _AGENT_CAD_ROOT = None

    script_dir = Path(__file__).resolve().parent
    candidates: list[Path] = []

    for env_name in ("DEER_FLOW_PROJECT_ROOT", "DEER_FLOW_HOME"):
        env_value = os.getenv(env_name)
        if env_value:
            candidates.append(Path(env_value))

    candidates.extend(
        [
            Path.cwd(),
            script_dir,
            script_dir.parent,
            script_dir.parent.parent,
            script_dir.parent.parent.parent,
        ]
    )

    for candidate in candidates:
        try:
            probe = candidate.resolve()
        except OSError:
            continue
        while True:
            is_deerflow_root = (
                (probe / "config.yaml").is_file()
                and (probe / "backend" / "packages" / "harness" / "deerflow" / "config" / "paths.py").is_file()
            )
            has_cad_experience = (probe / "skills" / "custom" / "cad-experience").is_dir()
            if is_deerflow_root or has_cad_experience:
                _AGENT_CAD_ROOT = str(probe)
                return _AGENT_CAD_ROOT
            parent = probe.parent
            if parent == probe:
                break
            probe = parent

    # freecad_sse_server.py lives under <project>/mymcp/cad in this deployment.
    _AGENT_CAD_ROOT = str(script_dir.parent.parent)
    return _AGENT_CAD_ROOT


def _resolve_local_cad_path(file_path: str) -> str | None:
    """Resolve Linux/backend upload paths that the Windows FreeCAD host cannot see."""
    normalized_path = file_path.replace("\\", "/")
    if len(normalized_path) >= 3 and normalized_path[1] == ":" and normalized_path[2] == "/":
        return None
    if normalized_path.startswith("//"):
        return None

    if os.path.isfile(normalized_path):
        return os.path.abspath(normalized_path)

    if not os.path.isabs(normalized_path):
        candidate = os.path.abspath(normalized_path)
        if os.path.isfile(candidate):
            return candidate

    agent_cad_root = _find_agent_cad_root()
    backend_root = os.path.join(agent_cad_root, "backend")
    local_candidates = [
        os.path.join(agent_cad_root, normalized_path.lstrip("/")),
        os.path.join(backend_root, normalized_path.lstrip("/")),
    ]
    for candidate in local_candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)

    search_roots = []
    if env_home := os.getenv("DEER_FLOW_HOME"):
        search_roots.append(os.path.join(env_home, "threads"))
    search_roots.extend([
        os.path.join(backend_root, ".agent-cad", "threads"),
        os.path.join(os.path.expanduser("~"), ".agent-cad", "threads"),
    ])

    relative = ""
    virtual_prefix = "/mnt/user-data/"
    if normalized_path.startswith(virtual_prefix):
        relative = normalized_path[len(virtual_prefix):]
    elif "/user-data/" in normalized_path:
        relative = normalized_path.split("/user-data/", 1)[1]

    matches = []
    for root in search_roots:
        if not os.path.isdir(root):
            continue
        if relative:
            for thread_id in os.listdir(root):
                candidate = os.path.join(root, thread_id, "user-data", relative)
                if os.path.isfile(candidate):
                    matches.append(candidate)
        basename = os.path.basename(normalized_path)
        if basename:
            for dirpath, _, filenames in os.walk(root):
                if basename in filenames:
                    candidate = os.path.join(dirpath, basename)
                    if os.path.isfile(candidate):
                        matches.append(candidate)

    if not matches:
        return None
    return os.path.abspath(max(matches, key=os.path.getmtime))


def _copy_local_file_to_freecad_host(freecad: FreeCADConnection, local_path: str) -> str:
    """Copy a backend-local CAD file to the FreeCAD upload directory."""
    safe_name = "".join(
        char if char.isalnum() or char in {".", "_", "-"} else "_"
        for char in os.path.basename(local_path)
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    remote_path = f"{REMOTE_CAD_UPLOAD_DIR.rstrip('/')}/{timestamp}_{safe_name}"
    remote_literal = json.dumps(remote_path)

    init_script = f"""
import os
target_path = {remote_literal}
os.makedirs(os.path.dirname(target_path), exist_ok=True)
with open(target_path, "wb") as handle:
    pass
print(f"CAD_REMOTE_TARGET:{{target_path}}")
"""
    init_res = freecad.execute_code(init_script)
    if not init_res.get("success", False):
        raise Exception(f"Failed to initialize remote CAD file copy: {init_res.get('error', init_res.get('message', 'Unknown error'))}")

    chunk_size = 512 * 1024
    total_size = os.path.getsize(local_path)
    copied = 0
    with open(local_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            copied += len(chunk)
            encoded = base64.b64encode(chunk).decode("ascii")
            chunk_literal = json.dumps(encoded)
            chunk_script = f"""
import base64
target_path = {remote_literal}
with open(target_path, "ab") as handle:
    handle.write(base64.b64decode({chunk_literal}))
print("CAD_UPLOAD_PROGRESS:{copied}/{total_size}")
"""
            chunk_res = freecad.execute_code(chunk_script)
            if not chunk_res.get("success", False):
                raise Exception(f"Failed to copy CAD file chunk to FreeCAD host: {chunk_res.get('error', chunk_res.get('message', 'Unknown error'))}")

    return remote_path


# ── 经验库辅助函数 ────────────────────────────────────────────────

# `_find_agent_cad_root` is defined above and reused by upload resolution and
# the CAD experience helpers below.


def _experience_base_dir() -> Path:
    return Path(_find_agent_cad_root()) / "skills" / "custom" / "cad-experience"


def _slugify(text: str) -> str:
    """Turn an error_signature into a safe filename fragment."""
    safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in text)
    safe = safe.strip("_").lower()
    return safe[:50] if safe else "untitled"


def _parse_frontmatter(file_path: Path) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a .md file. Returns (frontmatter_dict, body_text)."""
    raw = file_path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    return dict(fm) if isinstance(fm, dict) else {}, parts[2].strip()


def _extract_sections(body: str) -> dict[str, str]:
    """Extract ## 症状, ## 根因, ## 解决 sections from experience body."""
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = line[3:].strip()
            current_lines = []
        elif current_key:
            current_lines.append(line)
    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _build_experience_body(symptom: str, root_cause: str, solution: str, update_record: str = "") -> str:
    """Build the markdown body for an experience file."""
    title = f"# {symptom[:60]}" if symptom else "# 未命名经验"
    parts = [
        title,
        "",
        "## 症状",
        symptom or "(待补充)",
        "",
        "## 根因",
        root_cause or "(待补充)",
        "",
        "## 解决",
        solution or "(待补充)",
    ]
    if update_record:
        parts.extend(["", "## 解法更新记录", update_record])
    return "\n".join(parts) + "\n"


def _compute_heat(
    hit_count: int = 0,
    failed_attempts: int = 0,
    status: str = "draft",
    resolved_by: str = "agent",
    updated_by: str = "agent",
) -> int:
    """Compute experience heat score from the formula in README.md."""
    base = (hit_count - failed_attempts * 2) * 10
    verified_bonus = 20 if status == "verified" else 0
    manual_bonus = 5 if (resolved_by != "agent" or updated_by != "agent") else 0
    return base + verified_bonus + manual_bonus


def _normalize_sig(sig: str) -> set[str]:
    """Normalize an error_signature to a set of tokens for fuzzy matching."""
    import re
    norm = str(sig).lower()
    norm = re.sub(r'[-_ ]+', ' ', norm)
    norm = re.sub(r'[^a-z0-9 ]', '', norm)
    return set(norm.split())


_CANONICAL_EXPERIENCE_SIGNATURES: dict[str, tuple[str, ...]] = {
    "face_skip_headless_flat_top": (
        "face_skip_headless_flat_top",
        "face empty toolpath",
        "face headless empty",
        "skip face",
        "skip facing",
    ),
    "profile_empty_toolpath": (
        "profile_empty_toolpath",
        "profile empty toolpath",
        "profile non cutting",
        "profile command_count 0",
        "profile command count 0",
    ),
    "profile_all_side_faces": (
        "profile_all_side_faces",
        "all side faces",
        "all_side_faces",
        "triangular prism profile",
        "non rectangular profile",
    ),
    "drilling_headless_fallback": (
        "drilling_headless_fallback",
        "drilling headless fallback",
        "drilling cam api unavailable",
        "cam_api_unavailable",
    ),
    "drilling_locations_uppercase_xy": (
        "drilling_locations_uppercase_xy",
        "uppercase x/y",
        "uppercase x y",
        "requires x and y values",
        "requires lowercase x and y",
    ),
    "finaldepth_above_startdepth": (
        "finaldepth_above_startdepth",
        "final depth above start depth",
        "finaldepth above startdepth",
        "finaldepth higher than startdepth",
        "finaldepth must be lower than startdepth",
        "final depth must be lower than start depth",
    ),
    "final_depth_out_of_bounds": (
        "final_depth_out_of_bounds",
        "final depth out of bounds",
        "finaldepth out of bounds",
        "depth out of bounds",
    ),
    "safe_height_error": (
        "safe_height_error",
        "safe height error",
        "safe_height_low",
        "invalid_height",
    ),
    "residual_tc_spindle_zero": (
        "residual_tc_spindle_zero",
        "residual tool controller spindle zero",
        "spindle speed feed values",
        "spindle=0",
        "spindle 0",
    ),
    "empty_toolpath_after_recompute_headless": (
        "empty_toolpath_after_recompute_headless",
        "empty toolpath after recompute",
        "recompute command_count 0",
        "recompute command count 0",
        "all operations command_count 0",
    ),
    "non_cutting_operation": (
        "non_cutting_operation",
        "non_cutting",
        "non cutting",
        "zombie operation",
    ),
    "unsupported_stock_mode": (
        "unsupported_stock_mode",
        "unsupported stock mode",
        "from_relative",
    ),
    "tool_diameter_too_large": (
        "tool_diameter_too_large",
        "tool diameter too large",
        "diameter too large",
        "feature not reachable",
    ),
    "tool_preset_not_found": (
        "tool_preset_not_found",
        "tool preset not found",
        "no matching tool preset",
        "preset not found",
    ),
    "feed_rate_unit_mismatch": (
        "feed_rate_unit_mismatch",
        "feed_rate",
        "feed rate unit mismatch",
        "feedrate unit mismatch",
        "60 times",
        "60x",
    ),
    "invalid_project_file": (
        "invalid_project_file",
        "invalid project file",
        "step fallback",
        "iges fallback",
    ),
    "freecad_timed_out": (
        "freecad_timed_out",
        "timed out",
        "timeout",
        "rpc timeout",
        "freecad timeout",
    ),
    "wcs_z_origin_at_stock_bottom_not_top": (
        "wcs_z_origin_at_stock_bottom_not_top",
        "wcs z origin at stock bottom",
        "stock bottom not top",
        "model_center_top",
    ),
}


def _canonical_sig_text(text: str) -> str:
    """Return lowercase text with separators collapsed for signature comparisons."""
    import re
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")


def _canonical_experience_signature(text: str) -> str:
    """Map known CAM experience aliases and error prose to the canonical signature."""
    canonical_text = _canonical_sig_text(text)
    if not canonical_text:
        return ""

    for canonical, aliases in _CANONICAL_EXPERIENCE_SIGNATURES.items():
        if canonical_text == canonical:
            return canonical
        for alias in aliases:
            alias_text = _canonical_sig_text(alias)
            if alias_text and (canonical_text == alias_text or alias_text in canonical_text):
                return canonical

    return canonical_text


def _signature_matches_message(error_signature: str, error_message: str) -> bool:
    """Match both legacy/raw and canonicalized signatures against a tool error."""
    sig = str(error_signature)
    message = str(error_message)
    if sig and sig in message:
        return True
    canonical_sig = _canonical_experience_signature(sig)
    canonical_message = _canonical_experience_signature(message)
    if canonical_sig and canonical_sig == canonical_message:
        return True
    normalized_message = _canonical_sig_text(message)
    return bool(canonical_sig and canonical_sig in normalized_message)


def _fuzzy_match_existing(dir_path: Path, error_signature: str, tool_name: str) -> Path | None:
    """Fuzzy-match an error_signature against existing entries with the same tool.

    Returns the best-matching file path if Jaccard similarity >= 0.4, else None.
    """
    new_tokens = _normalize_sig(_canonical_experience_signature(error_signature))
    if not new_tokens:
        return None

    best_match: Path | None = None
    best_score = 0.0

    for md_file in dir_path.glob("*.md"):
        fm, _ = _parse_frontmatter(md_file)
        if fm.get("tool") != tool_name:
            continue
        existing_tokens = _normalize_sig(_canonical_experience_signature(str(fm.get("error_signature", ""))))
        if not existing_tokens:
            continue

        intersection = new_tokens & existing_tokens
        union = new_tokens | existing_tokens
        jaccard = len(intersection) / len(union) if union else 0.0

        if jaccard >= 0.4 and jaccard > best_score:
            best_score = jaccard
            best_match = md_file

    return best_match


# 创建 MCP 服务器实例
mcp = FastMCP("freecad-sse-server", host=DEFAULT_HOST, port=DEFAULT_PORT)
register_cam_tools(mcp, get_freecad_connection)

@mcp.tool()
@observe_mcp_tool("create_document")
def create_document(name: str) -> str:
    """Create a new FreeCAD document (CAD design file).
    Use this tool to create a new CAD document for 3D modeling.
    For creating file system directories/folders, use the create_directory tool instead.

    Args:
        name: The name of the FreeCAD document to create (e.g., "MyDesign", "Part1").

    Returns:
        A message indicating the success or failure of the document creation.

    Examples:
        If you want to create a document named "MyDocument", you can use the following data.
        ```json
        {
            "name": "MyDocument"
        }
        ```
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.create_document(name)
        if res["success"]:
            return json.dumps({
                "success": True,
                "message": f"Document '{res['document_name']}' created successfully",
                "document_name": res['document_name']
            }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({
                "success": False,
                "error": f"Failed to create document: {res['error']}"
            }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to create document: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to create document: {str(e)}"
        }, ensure_ascii=False, indent=2)

def _open_cad_file_impl(file_path: str, activate: bool = True) -> str:
    freecad = get_freecad_connection()
    try:
        normalized_path = file_path.replace("\\", "/")
        extension = os.path.splitext(normalized_path)[1].lower()
        if extension not in SUPPORTED_CAD_OPEN_EXTENSIONS:
            supported = ", ".join(sorted(SUPPORTED_CAD_OPEN_EXTENSIONS))
            return json.dumps({
                "success": False,
                "error": f"Expected a supported CAD file path ({supported}), got: {file_path}"
            }, ensure_ascii=False, indent=2)

        local_path = _resolve_local_cad_path(normalized_path)
        opened_path = normalized_path
        transferred_to_freecad_host = False
        if local_path:
            opened_path = _copy_local_file_to_freecad_host(freecad, local_path)
            transferred_to_freecad_host = True

        path_literal = json.dumps(opened_path)
        activate_literal = "True" if activate else "False"
        extension_literal = json.dumps(extension)
        script = f"""
import os
import FreeCAD as App

file_path = {path_literal}
extension = {extension_literal}
if not os.path.isfile(file_path):
    raise Exception(f"file_not_found:CAD file not found: {{file_path}}")

open_method = "document"
if extension == ".fcstd":
    doc = App.openDocument(file_path)
else:
    import Import
    open_method = "import"
    Import.open(file_path)
    doc = App.ActiveDocument

if doc is None:
    raise Exception(f"open_failed:Failed to open CAD file: {{file_path}}")

if {activate_literal}:
    App.setActiveDocument(doc.Name)

shape_object_names = []
for obj in getattr(doc, "Objects", []):
    shape = getattr(obj, "Shape", None)
    if shape is None:
        continue
    try:
        if shape.isNull():
            continue
    except Exception:
        pass
    shape_object_names.append(obj.Name)

# Identify final object: the boolean result NOT consumed by any other boolean op.
# Collect names of objects used as Base or Tool of boolean operations (Part::Cut/Fuse/Common).
consumed = set()
boolean_objects = {{}}  # name -> TypeId for boolean results
for obj in getattr(doc, "Objects", []):
    type_id = getattr(obj, "TypeId", "")
    if "Part::" in type_id and any(k in type_id for k in ("Cut", "Fuse", "Common")):
        boolean_objects[obj.Name] = type_id
        base = getattr(obj, "Base", None)
        tool = getattr(obj, "Tool", None)
        for ref in (base, tool):
            if ref is None:
                continue
            items = ref if isinstance(ref, (list, tuple)) else [ref]
            for item in items:
                consumed.add(item.Name if hasattr(item, "Name") else str(item))

# Only boolean results NOT consumed are valid final candidates
boolean_final = [n for n in shape_object_names if n in boolean_objects and n not in consumed]
if boolean_final:
    final_model_name = boolean_final[-1]
else:
    # Fallback: last shape object not consumed (any type)
    final_candidates = [n for n in shape_object_names if n not in consumed]
    final_model_name = final_candidates[-1] if final_candidates else (shape_object_names[-1] if shape_object_names else "")

print(f"CAD_OPEN_SUCCESS:{{doc.Name}}")
print(f"CAD_OPEN_LABEL:{{doc.Label}}")
print(f"CAD_OBJECT_COUNT:{{len(doc.Objects)}}")
print(f"CAD_OPEN_METHOD:{{open_method}}")
print("CAD_MODEL_NAMES:" + ",".join(shape_object_names))
print("CAD_FINAL_MODEL:" + final_model_name)
"""
        res = freecad.execute_code(script)
        if not res.get("success", False):
            error_msg = res.get("error", res.get("message", "Unknown error"))
            return json.dumps({
                "success": False,
                "error": f"Failed to open CAD file: {error_msg}"
            }, ensure_ascii=False, indent=2)

        output = res.get("message", "")
        document_name = ""
        document_label = ""
        object_count = 0
        open_method = ""
        shape_object_names = []
        final_model_name = ""
        for line in output.split("\n"):
            if line.startswith("CAD_OPEN_SUCCESS:"):
                document_name = line.split(":", 1)[1].strip()
            elif line.startswith("CAD_OPEN_LABEL:"):
                document_label = line.split(":", 1)[1].strip()
            elif line.startswith("CAD_OBJECT_COUNT:"):
                try:
                    object_count = int(line.split(":", 1)[1].strip())
                except ValueError:
                    object_count = 0
            elif line.startswith("CAD_OPEN_METHOD:"):
                open_method = line.split(":", 1)[1].strip()
            elif line.startswith("CAD_MODEL_NAMES:"):
                raw_names = line.split(":", 1)[1].strip()
                shape_object_names = [name for name in raw_names.split(",") if name]
            elif line.startswith("CAD_FINAL_MODEL:"):
                final_model_name = line.split(":", 1)[1].strip()

        # Use detected final model if available, otherwise fall back to last shape object
        if final_model_name:
            model_name = final_model_name
        elif shape_object_names:
            model_name = shape_object_names[-1]  # last created shape object, not first
        else:
            model_name = ""

        # Fallback: if document_name is empty but document opened successfully,
        # fetch the active document name via a quick execute_code call.
        # This saves ~8 agent rounds of trial-and-error trying to discover the doc name.
        if not document_name:
            fallback_res = freecad.execute_code("""
import FreeCAD as App
doc = App.ActiveDocument
if doc:
    print("FALLBACK_DOC_NAME:" + doc.Name)
""")
            if fallback_res.get("success"):
                for line in fallback_res.get("message", "").split("\n"):
                    if line.startswith("FALLBACK_DOC_NAME:"):
                        document_name = line.split(":", 1)[1].strip()
                        break

        screenshot = freecad.get_active_screenshot()
        return json.dumps({
            "success": True,
            "message": "CAD file opened successfully",
            "document_name": document_name,
            "document_label": document_label,
            "object_count": object_count,
            "open_method": open_method,
            "model_name": model_name,
            "shape_object_names": shape_object_names,
            "source_path": normalized_path,
            "opened_path": opened_path,
            "transferred_to_freecad_host": transferred_to_freecad_host,
            "has_screenshot": screenshot is not None
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to open CAD file: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to open CAD file: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("open_cad_file")
def open_cad_file(file_path: str, activate: bool = True) -> str:
    """Open an existing CAD file in FreeCAD using the host's standard open flow.

    Args:
        file_path: The CAD file path. Backend upload paths are copied to the
            configured FreeCAD upload directory before opening.
        activate: Whether to make the opened document the active document.

    Returns:
        JSON with success status, opened document name, label, object count, and screenshot availability.
    """
    return _open_cad_file_impl(file_path, activate)


@mcp.tool()
@observe_mcp_tool("open_fcstd_file")
def open_fcstd_file(file_path: str, activate: bool = True) -> str:
    """Deprecated compatibility alias for open_cad_file."""
    return _open_cad_file_impl(file_path, activate)


logger.info("Registered tools: open_cad_file, open_fcstd_file")


@mcp.tool()
@observe_mcp_tool("cad_record_failure")
def cad_record_failure(
    stage: str,
    message: str,
    skill_name: str = "",
    tool_name: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    """Record a higher-level CAD/CAM failure sample for controlled maintenance.

    Use this when a skill fails before or between MCP tool calls, or when the
    observed failure is a workflow/spec problem rather than a single FreeCAD API
    call. This tool only records diagnostics; it does not modify skills or MCP
    code.
    """
    event = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "tool": "cad_record_failure",
        "success": False,
        "stage": stage,
        "skill_name": skill_name,
        "related_tool": tool_name,
        "failure_class": classify_failure(message),
        "error_message": message,
        "context": context or {},
    }
    record_event(event)
    return json.dumps({
        "success": True,
        "message": "CAD/CAM failure sample recorded",
        "failure_class": event["failure_class"],
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("cad_query_experience")
def cad_query_experience(
    error_message: str,
    tool_name: str = "",
    failure_class: str = "",
) -> str:
    """Search the CAD/CAM experience library for known fixes to a tool failure.

    Must be called BEFORE any self-reasoning or repair loop when a CAD/CAM tool
    returns ``success=false``.  The tool scans all experience entries, performs
    triple matching (error_signature substring × tool name × failure class),
    and returns ranked solutions ordered by heat score.

    Args:
        error_message: The full error message returned by the failing tool.
        tool_name: The name of the tool that failed (optional but strongly
                   recommended for precise matching).
        failure_class: Failure class from ``classify_failure()`` (optional).
    Returns:
        JSON with ``matches`` list (sorted by heat, most relevant first), each
        containing ``file``, ``error_signature``, ``tool``, ``failure_class``,
        ``heat``, ``symptom``, ``root_cause``, ``solution``.
    """
    base = _experience_base_dir()
    if not base.is_dir():
        return json.dumps({
            "success": True,
            "matches": [],
            "count": 0,
            "message": f"Experience library not found at {base}",
        }, ensure_ascii=False, indent=2)

    matches: list[dict[str, Any]] = []
    for subdir in ("cam", "modeling", "export"):
        dir_path = base / subdir
        if not dir_path.is_dir():
            continue
        for md_file in sorted(dir_path.glob("*.md")):
            fm, body = _parse_frontmatter(md_file)
            if not fm.get("error_signature"):
                continue

            heat = _compute_heat(
                hit_count=int(fm.get("hit_count", 0)),
                failed_attempts=int(fm.get("failed_attempts", 0)),
                status=str(fm.get("status", "draft")),
                resolved_by=str(fm.get("resolved_by", "agent")),
                updated_by=str(fm.get("updated_by", "agent")),
            )
            if heat < 0:
                continue

            sig = str(fm.get("error_signature", ""))
            tool = str(fm.get("tool", ""))
            fclass = str(fm.get("failure_class", ""))

            canonical_sig = _canonical_experience_signature(sig)
            sig_match = _signature_matches_message(sig, error_message)
            tool_match = bool(tool_name) and tool == tool_name
            class_match = bool(failure_class) and fclass == failure_class

            if sig_match and (tool_match or class_match):
                sections = _extract_sections(body)
                matches.append({
                    "file": str(md_file),
                    "error_signature": sig,
                    "canonical_error_signature": canonical_sig,
                    "tool": tool,
                    "failure_class": fclass,
                    "heat": heat,
                    "status": str(fm.get("status", "draft")),
                    "hit_count": int(fm.get("hit_count", 0)),
                    "symptom": sections.get("症状", ""),
                    "root_cause": sections.get("根因", ""),
                    "solution": sections.get("解决", ""),
                })

    matches.sort(key=lambda m: m["heat"], reverse=True)
    return json.dumps({
        "success": True,
        "matches": matches,
        "count": len(matches),
        "message": f"Found {len(matches)} matching experience(s)" if matches else "No matching experience found",
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("cad_write_experience")
def cad_write_experience(
    error_signature: str,
    tool_name: str,
    failure_class: str,
    symptom: str = "",
    root_cause: str = "",
    solution: str = "",
    subdirectory: str = "cam",
    tags: list[str] | None = None,
    increment_hit: bool = False,
    increment_failed: bool = False,
    update_note: str = "",
) -> str:
    """Create or update a CAD/CAM experience entry in the experience library.

    Call this after successfully solving a new problem or when an existing
    experience solution has been applied and needs its hit/failed counter
    updated.  If an entry with the same ``error_signature`` and ``tool_name``
    already exists it will be updated; otherwise a new entry is created.

    Args:
        error_signature: Core error keyword for matching (e.g. "final_depth_out_of_bounds").
        tool_name: The MCP tool that failed.
        failure_class: Failure class from ``classify_failure()``.
        symptom: Human-readable symptom description (for new entries).
        root_cause: Root cause analysis (for new entries).
        solution: The fix steps that resolved the problem.
        subdirectory: One of "cam", "modeling", "export".  Defaults to "cam".
        tags: Optional tags list.
        increment_hit: Set True when an experience solution was applied successfully.
        increment_failed: Set True when an experience solution was tried but failed.
        update_note: When updating, a note explaining what changed and why.
    Returns:
        JSON with ``action`` ("created" or "updated"), ``file`` path,
        and the new ``hit_count`` / ``failed_attempts``.
    """
    requested_error_signature = error_signature
    error_signature = _canonical_experience_signature(error_signature)
    base = _experience_base_dir()
    dir_path = base / subdirectory
    dir_path.mkdir(parents=True, exist_ok=True)

    existing_file: Path | None = None
    existing_fm: dict[str, Any] = {}
    fuzzy_match = False
    match_type = "exact"  # exact | sig_only | fuzzy

    # Level 1: exact match on error_signature + tool
    for md_file in dir_path.glob("*.md"):
        fm, _ = _parse_frontmatter(md_file)
        fm_sig = _canonical_experience_signature(str(fm.get("error_signature", "")))
        if fm_sig == error_signature and fm.get("tool") == tool_name:
            existing_file = md_file
            existing_fm = fm
            break

    # Level 2: exact match on error_signature only (tool may differ)
    if existing_file is None:
        for md_file in dir_path.glob("*.md"):
            fm, _ = _parse_frontmatter(md_file)
            fm_sig = _canonical_experience_signature(str(fm.get("error_signature", "")))
            if fm_sig == error_signature:
                existing_file = md_file
                existing_fm = fm
                fuzzy_match = True
                match_type = "sig_only"
                break

    # Level 3: fuzzy match — same tool + similar error_signature tokens
    if existing_file is None:
        fuzzy_file = _fuzzy_match_existing(dir_path, error_signature, tool_name)
        if fuzzy_file is not None:
            existing_file = fuzzy_file
            existing_fm, _ = _parse_frontmatter(fuzzy_file)
            fuzzy_match = True
            match_type = "fuzzy"

    today = datetime.utcnow().strftime("%Y-%m-%d")

    if existing_file is not None:
        hit_count = int(existing_fm.get("hit_count", 0))
        failed_attempts = int(existing_fm.get("failed_attempts", 0))
        if increment_hit:
            hit_count += 1
        if increment_failed:
            failed_attempts += 1

        fm_lines: list[str] = []
        for key, value in existing_fm.items():
            if key == "hit_count":
                fm_lines.append(f"hit_count: {hit_count}")
            elif key == "failed_attempts":
                fm_lines.append(f"failed_attempts: {failed_attempts}")
            elif key == "error_signature":
                fm_lines.append(f"error_signature: {error_signature}")
            elif key == "last_updated":
                fm_lines.append(f"last_updated: {today}")
            elif key == "updated_by":
                fm_lines.append("updated_by: agent")
            elif key == "status":
                fm_lines.append("status: draft")
            elif key == "heat":
                new_heat = _compute_heat(hit_count, failed_attempts, "draft", str(existing_fm.get("resolved_by", "agent")), "agent")
                fm_lines.append(f"heat: {new_heat}")
            elif key == "tags" and tags:
                existing_tags = list(existing_fm.get("tags", []))
                for t in tags:
                    if t not in existing_tags:
                        existing_tags.append(t)
                fm_lines.append(f"tags: {json.dumps(existing_tags, ensure_ascii=False)}")
            elif isinstance(value, list):
                fm_lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
            else:
                fm_lines.append(f"{key}: {value}")

        fm_text = "\n".join(fm_lines)
        _, old_body = _parse_frontmatter(existing_file)
        update_block = ""
        if fuzzy_match:
            if match_type == "sig_only":
                update_block = f"- {today} (agent): error_signature 精确匹配但 tool 不同（已有 tool=\"{existing_fm.get('tool', '')}\"，新请求 tool=\"{tool_name}\"）。自动合并到已有条目。"
            else:
                update_block = f"- {today} (agent): 模糊匹配命中（原 error_signature=\"{existing_fm.get('error_signature', '')}\"，新请求 error_signature=\"{error_signature}\"）。Jaccard 相似度触发合并。"
        if requested_error_signature != error_signature:
            canonical_note = f"- {today} (agent): 工具层将请求 error_signature=\"{requested_error_signature}\" 归一化为 canonical=\"{error_signature}\"。"
            update_block = (update_block + "\n" + canonical_note) if update_block else canonical_note
        if update_note:
            note = f"- {today} (agent): {update_note}"
        elif increment_failed:
            note = f"- {today} (agent): 经验解法执行后仍失败。failed_attempts += 1"
        elif increment_hit:
            note = f"- {today} (agent): 经验解法应用成功。hit_count += 1"
        else:
            note = ""
        if note:
            update_block = (update_block + "\n" + note) if update_block else note

        if update_block:
            existing_updates = ""
            for line in old_body.split("\n"):
                if line.startswith("## 解法更新记录"):
                    existing_updates = "\n".join(old_body.split("## 解法更新记录")[1:])
                    old_body = old_body.split("## 解法更新记录")[0].rstrip()
                    break
            new_body = old_body.rstrip() + "\n\n## 解法更新记录\n" + update_block
            if existing_updates:
                existing_updates = existing_updates.lstrip("\n")
                if existing_updates:
                    new_body += "\n" + existing_updates
        else:
            new_body = old_body

        new_content = "---\n" + fm_text + "\n---\n\n" + new_body.strip() + "\n"
        existing_file.write_text(new_content, encoding="utf-8")

        return json.dumps({
            "success": True,
            "action": "updated",
            "file": str(existing_file),
            "error_signature": error_signature,
            "requested_error_signature": requested_error_signature,
            "tool": tool_name,
            "hit_count": hit_count,
            "failed_attempts": failed_attempts,
        }, ensure_ascii=False, indent=2)

    # ── create new entry ──────────────────────────────────────────
    slug = _slugify(error_signature)
    new_file = dir_path / f"{slug}.md"
    counter = 1
    while new_file.exists():
        new_file = dir_path / f"{slug}_{counter}.md"
        counter += 1

    tags_list = tags or []
    heat = _compute_heat(0, 0, "draft", "agent", "agent")
    fm_data: dict[str, Any] = {
        "error_signature": error_signature,
        "failure_class": failure_class,
        "tool": tool_name,
        "tags": tags_list,
        "status": "draft",
        "hit_count": 0,
        "failed_attempts": 0,
        "created": today,
        "last_updated": today,
        "resolved_by": "agent",
        "updated_by": "agent",
        "heat": heat,
    }
    fm_lines_new = []
    for key, value in fm_data.items():
        if isinstance(value, list):
            fm_lines_new.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        elif isinstance(value, str):
            fm_lines_new.append(f'{key}: "{value}"' if ":" in value or value.startswith(" ") else f"{key}: {value}")
        else:
            fm_lines_new.append(f"{key}: {value}")
    fm_text_new = "\n".join(fm_lines_new)
    body_text = _build_experience_body(symptom, root_cause, solution)
    new_file.write_text("---\n" + fm_text_new + "\n---\n\n" + body_text, encoding="utf-8")

    return json.dumps({
        "success": True,
        "action": "created",
        "file": str(new_file),
        "error_signature": error_signature,
        "requested_error_signature": requested_error_signature,
        "tool": tool_name,
        "hit_count": 0,
        "failed_attempts": 0,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("create_object")
def create_object(
    doc_name: str,
    obj_type: str,
    obj_name: str = "Body",
    analysis_name: str | None = None,
    obj_properties: dict[str, Any] = None,
) -> str:
    """Create a new object in FreeCAD.
    Object type is starts with "Part::" or "Draft::" or "PartDesign::" or "Fem::".

    Args:
        doc_name: The name of the document to create the object in.
        obj_type: The type of the object to create (e.g. 'Part::Box', 'Part::Cylinder', 'Draft::Circle', 'PartDesign::Body', etc.).
        obj_name: The name of the object to create.
        obj_properties: The properties of the object to create.

    Returns:
        A message indicating the success or failure of the object creation and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        obj_data = {"Name": obj_name, "Type": obj_type, "Properties": obj_properties or {}, "Analysis": analysis_name}
        res = freecad.create_object(doc_name, obj_data)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": res["success"],
            "object_name": res.get("object_name", ""),
            "message": f"Object '{res['object_name']}' created successfully" if res["success"] else f"Failed to create object: {res['error']}",
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to create object: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to create object: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("edit_object")
def edit_object(
    doc_name: str, obj_name: str, obj_properties: dict[str, Any]
) -> str:
    """Edit an object in FreeCAD.
    This tool is used when the `create_object` tool cannot handle the object creation.

    Args:
        doc_name: The name of the document to edit the object in.
        obj_name: The name of the object to edit.
        obj_properties: The properties of the object to edit.

    Returns:
        A message indicating the success or failure of the object editing and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        # 处理 Placement 属性：如果值是 tuple/list (x,y,z)，转换为 App.Placement
        placement_value = obj_properties.get("Placement")
        if placement_value is not None and isinstance(placement_value, (tuple, list)):
            # 从属性中移除 Placement，稍后单独处理
            obj_properties = {k: v for k, v in obj_properties.items() if k != "Placement"}
            # 使用 execute_code 设置 Placement
            if len(placement_value) == 3:
                x, y, z = placement_value
                placement_script = f"""
import FreeCAD as App
doc = App.getDocument("{doc_name}")
if doc:
    obj = doc.getObject("{obj_name}")
    if obj:
        obj.Placement = App.Placement(App.Vector({x}, {y}, {z}), App.Rotation())
        print("PLACEMENT_SET")
"""
                placement_result = freecad.execute_code(placement_script)
                if not placement_result.get("success", False):
                    raise Exception(f"Failed to set Placement: {placement_result.get('error', 'Unknown error')}")
            else:
                raise Exception(f"Invalid Placement format: expected 3 values, got {len(placement_value)}")

        res = freecad.edit_object(doc_name, obj_name, obj_properties)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": res["success"],
            "object_name": res.get("object_name", ""),
            "message": f"Object '{res['object_name']}' edited successfully" if res["success"] else f"Failed to edit object: {res['error']}",
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to edit object: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to edit object: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("delete_object")
def delete_object(doc_name: str, obj_name: str) -> str:
    """Delete an object in FreeCAD.

    Args:
        doc_name: The name of the document to delete the object from.
        obj_name: The name of the object to delete.

    Returns:
        A message indicating the success or failure of the object deletion and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.delete_object(doc_name, obj_name)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": res["success"],
            "object_name": res.get("object_name", ""),
            "message": f"Object '{res['object_name']}' deleted successfully" if res["success"] else f"Failed to delete object: {res['error']}",
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to delete object: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to delete object: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("execute_code")
def execute_code(code: str) -> str:
    """Execute arbitrary Python code in FreeCAD.

    Args:
        code: The Python code to execute.

    Returns:
        A message indicating the success or failure of the code execution, the output of the code execution, and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.execute_code(code)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": res["success"],
            "message": res.get("message", ""),
            "error": res.get("error", ""),
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to execute code: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to execute code: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("get_view")
def get_view(view_name: Literal["Isometric", "Front", "Top", "Right", "Back", "Left", "Bottom", "Dimetric", "Trimetric"]) -> str:
    """Get a screenshot of the active view.

    Args:
        view_name: The name of the view to get the screenshot of.
        The following views are available:
        - "Isometric"
        - "Front"
        - "Top"
        - "Right"
        - "Back"
        - "Left"
        - "Bottom"
        - "Dimetric"
        - "Trimetric"

    Returns:
        A screenshot of the active view.
    """
    freecad = get_freecad_connection()
    screenshot = freecad.get_active_screenshot(view_name)
    
    if screenshot is not None:
        return json.dumps({
            "success": True,
            "has_screenshot": True,
            "message": "Screenshot available"
        }, ensure_ascii=False, indent=2)
    else:
        return json.dumps({
            "success": False,
            "has_screenshot": False,
            "message": "Cannot get screenshot in the current view type (such as TechDraw or Spreadsheet)"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("insert_part_from_library")
def insert_part_from_library(relative_path: str) -> str:
    """Insert a part from the parts library addon.

    Args:
        relative_path: The relative path of the part to insert.

    Returns:
        A message indicating the success or failure of the part insertion and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        res = freecad.insert_part_from_library(relative_path)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": res["success"],
            "message": res.get("message", ""),
            "error": res.get("error", ""),
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to insert part from library: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to insert part from library: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("get_objects")
def get_objects(doc_name: str) -> str:
    """Get all objects in a document.
    You can use this tool to get the objects in a document to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the objects from.

    Returns:
        A list of objects in the document and a screenshot of the document.
    """
    freecad = get_freecad_connection()
    try:
        objects = freecad.get_objects(doc_name)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": True,
            "objects": objects,
            "count": len(objects),
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to get objects: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to get objects: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("get_object")
def get_object(doc_name: str, obj_name: str) -> str:
    """Get an object from a document.
    You can use this tool to get the properties of an object to see what you can check or edit.

    Args:
        doc_name: The name of the document to get the object from.
        obj_name: The name of the object to get.

    Returns:
        The object and a screenshot of the object.
    """
    freecad = get_freecad_connection()
    try:
        obj = freecad.get_object(doc_name, obj_name)
        screenshot = freecad.get_active_screenshot()
        
        result = {
            "success": True,
            "object": obj,
            "has_screenshot": screenshot is not None
        }
        
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to get object: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to get object: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("get_parts_list")
def get_parts_list(inputs: str = "") -> str:
    """Get the list of parts in the parts library addon.

    Args:
        inputs: the user inputs.

    Returns:
        List of parts in the parts library.
    """
    freecad = get_freecad_connection()
    try:
        parts = freecad.get_parts_list()

        result = {
            "success": True,
            "parts": parts,
            "count": len(parts)
        }

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error in get_parts_list: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Error getting parts list: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("export_model")
def export_model(doc_name: str, stl_file_name: str = "model.stl", stp_file_name: str = "model.stp", export_dir: str = "") -> str:
    """Export both STL and STP (STEP) files from FreeCAD.

    Exports all objects with Shape from the specified document to both STL and STP files
    in separate subdirectories under a timestamped export directory.

    Args:
        doc_name: The name of the FreeCAD document to export from. If empty, uses ActiveDocument.
        stl_file_name: The name of the STL file to export (default: "model.stl").
        stp_file_name: The name of the STP file to export (default: "model.stp").
        export_dir: The directory to export to. If empty, uses FREECAD_EXPORT_BASE env var
                   or "/root/GSK/cache/agentCAD/models" as default. Uses Linux absolute paths
                   (e.g., "/root/GSK/coder/agent/deerflow/folder/").

    Returns:
        JSON with success status, message, and data containing paths for both formats and base dir.
    """
    freecad = get_freecad_connection()
    try:
        # 确定导出目录
        if not export_dir:
            export_dir = '/root/GSK/cache/agentCAD/models'

        # 保持 FreeCAD 可以识别的 Linux 绝对路径格式
            pass

        script = f"""
import FreeCAD as App
import os
import Mesh
import Import
import datetime
import shutil

export_base = r"{export_dir}"
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
export_dir_full = os.path.join(export_base, f"export_{{timestamp}}")
stl_dir = os.path.join(export_dir_full, "stl")
stp_dir = os.path.join(export_dir_full, "stp")

try:
    doc = App.getDocument("{doc_name}") if "{doc_name}" else App.ActiveDocument
    if doc is None:
        raise Exception("document_not_found:Document not found")

    all_objects = [obj for obj in doc.Objects if hasattr(obj, "Shape")]
    if not all_objects:
        raise Exception("no_objects:No exportable objects found in document")

    # Identify final object: boolean result NOT consumed by other boolean ops
    consumed = set()
    boolean_objects = {{}}  # name -> TypeId for boolean results
    for obj in getattr(doc, "Objects", []):
        type_id = getattr(obj, "TypeId", "")
        if "Part::" in type_id and any(k in type_id for k in ("Cut", "Fuse", "Common")):
            boolean_objects[obj.Name] = type_id
            base = getattr(obj, "Base", None)
            tool = getattr(obj, "Tool", None)
            for ref in (base, tool):
                if ref is None:
                    continue
                items = ref if isinstance(ref, (list, tuple)) else [ref]
                for item in items:
                    consumed.add(item.Name if hasattr(item, "Name") else str(item))

    boolean_final = [obj for obj in all_objects if obj.Name in boolean_objects and obj.Name not in consumed]
    if boolean_final:
        export_objects = [boolean_final[-1]]
    else:
        final_candidates = [obj for obj in all_objects if obj.Name not in consumed]
        export_objects = [final_candidates[-1]] if final_candidates else [all_objects[-1]]
    export_object_name = export_objects[0].Name

    os.makedirs(stl_dir, exist_ok=True)
    os.makedirs(stp_dir, exist_ok=True)

    stl_path = os.path.join(stl_dir, "{stl_file_name}")
    stp_path = os.path.join(stp_dir, "{stp_file_name}")

    Mesh.export(export_objects, stl_path)
    Import.export(export_objects, stp_path)

    print(f"MODEL_EXPORT_SUCCESS:{{stl_path}},{{stp_path}}")
    print(f"MODEL_EXPORT_DIR:{{export_dir_full}}")
    print(f"STL_PATH:{{stl_path}}")
    print(f"STP_PATH:{{stp_path}}")
    print(f"EXPORTED_OBJECT:{{export_object_name}}")
except Exception:
    if os.path.isdir(export_dir_full):
        shutil.rmtree(export_dir_full, ignore_errors=True)
    raise
"""
        res = freecad.execute_code(script)

        if not res.get("success", False):
            error_msg = res.get("error", res.get("message", "Unknown error"))
            return json.dumps({
                "success": False,
                "error": f"Failed to export model: {error_msg}"
            }, ensure_ascii=False, indent=2)

        output = res.get("message", "")
        stl_path = ""
        stp_path = ""
        export_dir_full = ""

        for line in output.split("\n"):
            if line.startswith("MODEL_EXPORT_DIR:"):
                export_dir_full = line.split(":", 1)[1].strip()
            elif line.startswith("STL_PATH:"):
                stl_path = line.split(":", 1)[1].strip()
            elif line.startswith("STP_PATH:"):
                stp_path = line.split(":", 1)[1].strip()

        return json.dumps({
            "success": True,
            "message": "Model exported successfully (STL and STP)",
            "data": {
                "stl_path": stl_path,
                "stp_path": stp_path,
                "dir": export_dir_full
            }
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to export model: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to export model: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("export_stp")
def export_stp(doc_name: str, stp_file_name: str = "model.stp", export_dir: str = "/root/GSK/cache/agentCAD/models", obj_name: str | None = None) -> str:
    """Export STP (STEP) file from FreeCAD.

    Exports specified object or all objects with Shape from the document to a STP file
    in a timestamped subdirectory under the export directory.

    Args:
        doc_name: The name of the FreeCAD document to export from. If empty, uses ActiveDocument.
        stp_file_name: The name of the STP file to export (default: "model.stp").
        export_dir: The directory to export to (default: "/root/GSK/cache/agentCAD/models").
        obj_name: The name of the specific object to export. If None, exports all objects.

    Returns:
        JSON with success status, message, and data containing stp_path.
    """
    freecad = get_freecad_connection()
    try:
        script = f"""
import FreeCAD as App
import os
import Import
import datetime
import shutil

export_base = r"{export_dir}"
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
export_dir_full = os.path.join(export_base, f"export_{{timestamp}}")
stp_dir = os.path.join(export_dir_full, "stp")

try:
    doc = App.getDocument("{doc_name}") if "{doc_name}" else App.ActiveDocument
    if doc is None:
        raise Exception("document_not_found:Document not found")

    obj_name = "{obj_name}" if "{obj_name}" else None
    if obj_name:
        target_obj = doc.getObject(obj_name)
        if target_obj is None:
            raise Exception(f"object_not_found:Object '{obj_name}' not found")
        if not hasattr(target_obj, "Shape"):
            raise Exception(f"object_no_shape:Object '{obj_name}' has no Shape")
        if target_obj.Shape.isNull():
            raise Exception(f"object_empty_shape:Object '{obj_name}' has empty Shape")
        objects = [target_obj]
    else:
        all_objects = [obj for obj in doc.Objects if hasattr(obj, "Shape")]
        # Identify final object: boolean result NOT consumed by other boolean ops
        consumed = set()
        boolean_objects = {{}}  # name -> TypeId for boolean results
        for obj in getattr(doc, "Objects", []):
            type_id = getattr(obj, "TypeId", "")
            if "Part::" in type_id and any(k in type_id for k in ("Cut", "Fuse", "Common")):
                boolean_objects[obj.Name] = type_id
                base = getattr(obj, "Base", None)
                tool = getattr(obj, "Tool", None)
                for ref in (base, tool):
                    if ref is None:
                        continue
                    items = ref if isinstance(ref, (list, tuple)) else [ref]
                    for item in items:
                        consumed.add(item.Name if hasattr(item, "Name") else str(item))
        boolean_final = [obj for obj in all_objects if obj.Name in boolean_objects and obj.Name not in consumed]
        if boolean_final:
            objects = [boolean_final[-1]]
        else:
            final_candidates = [obj for obj in all_objects if obj.Name not in consumed]
            objects = [final_candidates[-1]] if final_candidates else [all_objects[-1]]
        export_object_name = objects[0].Name
    if not objects:
        raise Exception("no_objects:No exportable objects found in document")

    os.makedirs(stp_dir, exist_ok=True)
    stp_path = os.path.join(stp_dir, "{stp_file_name}")

    Import.export(objects, stp_path)

    print(f"STP_EXPORT_SUCCESS:{{stp_path}}")
    print(f"EXPORTED_OBJECT:{{export_object_name}}")
    print(f"STP_EXPORT_DIR:{{export_dir_full}}")
    print(f"STP_PATH:{{stp_path}}")
except Exception:
    if os.path.isdir(export_dir_full):
        shutil.rmtree(export_dir_full, ignore_errors=True)
    raise
"""
        res = freecad.execute_code(script)

        if not res.get("success", False):
            error_msg = res.get("error", res.get("message", "Unknown error"))
            return json.dumps({
                "success": False,
                "error": f"Failed to export STP: {error_msg}"
            }, ensure_ascii=False, indent=2)

        output = res.get("message", "")
        stp_path = ""
        export_dir_full = ""

        for line in output.split("\n"):
            if line.startswith("STP_EXPORT_DIR:"):
                export_dir_full = line.split(":", 1)[1].strip()
            elif line.startswith("STP_PATH:"):
                stp_path = line.split(":", 1)[1].strip()

        return json.dumps({
            "success": True,
            "message": "STP exported successfully",
            "data": {
                "stp_path": stp_path,
                "dir": export_dir_full
            }
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to export STP: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to export STP: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("export_stl")
def export_stl(doc_name: str = "", stl_file_name: str = "model.stl", export_dir: str = "/root/GSK/cache/agentCAD/models", obj_name: str | None = None) -> str:
    """Export STL file from FreeCAD.

    Exports specified object or all objects with Shape from the document to a STL file
    in a timestamped subdirectory under the export directory.

    Args:
        doc_name: The name of the FreeCAD document to export from. If empty, uses ActiveDocument.
        stl_file_name: The name of the STL file to export (default: "model.stl").
        export_dir: The directory to export to (default: "/root/GSK/cache/agentCAD/models").
        obj_name: The name of the specific object to export. If None, exports all objects.

    Returns:
        JSON with success status, message, and data containing stl_path.
    """
    freecad = get_freecad_connection()
    try:
        script = f"""
import FreeCAD as App
import os
import Mesh
import datetime
import shutil

export_base = r"{export_dir}"
timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
export_dir_full = os.path.join(export_base, f"export_{{timestamp}}")
stl_dir = os.path.join(export_dir_full, "stl")

try:
    doc = App.getDocument("{doc_name}") if "{doc_name}" else App.ActiveDocument
    if doc is None:
        raise Exception("document_not_found:Document not found")

    obj_name = "{obj_name}" if "{obj_name}" else None
    if obj_name:
        target_obj = doc.getObject(obj_name)
        if target_obj is None:
            raise Exception(f"object_not_found:Object '{obj_name}' not found")
        if not hasattr(target_obj, "Shape"):
            raise Exception(f"object_no_shape:Object '{obj_name}' has no Shape")
        if target_obj.Shape.isNull():
            raise Exception(f"object_empty_shape:Object '{obj_name}' has empty Shape")
        objects = [target_obj]
    else:
        all_objects = [obj for obj in doc.Objects if hasattr(obj, "Shape")]
        # Identify final object: boolean result NOT consumed by other boolean ops
        consumed = set()
        boolean_objects = {{}}  # name -> TypeId for boolean results
        for obj in getattr(doc, "Objects", []):
            type_id = getattr(obj, "TypeId", "")
            if "Part::" in type_id and any(k in type_id for k in ("Cut", "Fuse", "Common")):
                boolean_objects[obj.Name] = type_id
                base = getattr(obj, "Base", None)
                tool = getattr(obj, "Tool", None)
                for ref in (base, tool):
                    if ref is None:
                        continue
                    items = ref if isinstance(ref, (list, tuple)) else [ref]
                    for item in items:
                        consumed.add(item.Name if hasattr(item, "Name") else str(item))
        boolean_final = [obj for obj in all_objects if obj.Name in boolean_objects and obj.Name not in consumed]
        if boolean_final:
            objects = [boolean_final[-1]]
        else:
            final_candidates = [obj for obj in all_objects if obj.Name not in consumed]
            objects = [final_candidates[-1]] if final_candidates else [all_objects[-1]]
        export_object_name = objects[0].Name
    if not objects:
        raise Exception("no_objects:No exportable objects found in document")

    os.makedirs(stl_dir, exist_ok=True)
    stl_path = os.path.join(stl_dir, "{stl_file_name}")

    Mesh.export(objects, stl_path)

    print(f"STL_EXPORT_SUCCESS:{{stl_path}}")
    print(f"EXPORTED_OBJECT:{{export_object_name}}")
    print(f"STL_EXPORT_DIR:{{export_dir_full}}")
    print(f"STL_PATH:{{stl_path}}")
except Exception:
    if os.path.isdir(export_dir_full):
        shutil.rmtree(export_dir_full, ignore_errors=True)
    raise
"""
        res = freecad.execute_code(script)

        if not res.get("success", False):
            error_msg = res.get("error", res.get("message", "Unknown error"))
            return json.dumps({
                "success": False,
                "error": f"Failed to export STL: {error_msg}"
            }, ensure_ascii=False, indent=2)

        output = res.get("message", "")
        stl_path = ""
        export_dir_full = ""

        for line in output.split("\n"):
            if line.startswith("STL_EXPORT_DIR:"):
                export_dir_full = line.split(":", 1)[1].strip()
            elif line.startswith("STL_PATH:"):
                stl_path = line.split(":", 1)[1].strip()

        return json.dumps({
            "success": True,
            "message": "STL exported successfully",
            "data": {
                "stl_path": stl_path,
                "dir": export_dir_full
            }
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to export STL: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to export STL: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("read_export_file")
def read_export_file(file_path: str) -> str:
    """Read the content of an exported file from export_nc directory.

    Args:
        file_path: The full path to the file to read (e.g., from export_nc result data.path).

    Returns:
        JSON with success status, file content, and path.
    """
    import os
    try:
        # 兼容反斜杠写法，统一为 Linux 路径分隔符
        file_path = file_path.replace("\\", "/")
        if not os.path.exists(file_path):
            return json.dumps({
                "success": False,
                "error": f"File not found: {file_path}"
            }, ensure_ascii=False, indent=2)
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return json.dumps({
            "success": True,
            "content": content,
            "path": file_path
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to read export file: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to read file: {str(e)}"
        }, ensure_ascii=False, indent=2)


@mcp.tool()
@observe_mcp_tool("export_nc")
def export_nc(doc_name: str, file_name: str = "model.nc", export_dir: str = "") -> str:
    """Export NC/G-code file from FreeCAD CAM job.

    Exports the CAM job from the specified document to an NC file
    in a timestamped subdirectory under the specified export directory.

    Args:
        doc_name: The name of the FreeCAD document to export from. If empty, uses ActiveDocument.
        file_name: The name of the NC file to export (default: "model.nc").
        export_dir: The directory to export to. Defaults to "/root/GSK/cache/agentCAD/gcodes".

    Returns:
        JSON with success status, message, and data containing path and dir.
    """
    freecad = get_freecad_connection()
    try:
        # 确定导出目录
        if not export_dir:
            export_dir = '/root/GSK/cache/agentCAD/gcodes'

        script = f"""
import os
import datetime
import FreeCAD as App

export_dir_full = r"{export_dir}"
os.makedirs(export_dir_full, exist_ok=True)

doc = App.getDocument("{doc_name}") if "{doc_name}" else App.ActiveDocument
if doc is None:
    raise Exception("document_not_found:Document not found")

# 查找 CAM Job 对象
job = None
for obj in doc.Objects:
    if obj.isDerivedFrom("Path::Feature"):
        job = obj
        break

if job is None:
    raise Exception("no_job:No CAM job found in document")

nc_path = os.path.join(export_dir_full, "{file_name}")

# 使用 FreeCAD CAM 后处理器导出 G-code
# PathScripts 模块负责处理 CAM 后处理
import FreeCAD
import Path
from PathScripts import PostUtils

# 首先尝试使用 PostUtils 导出
post_utils_success = False
try:
    PostUtils.export(job, nc_path)
    post_utils_success = True
except Exception as e:
    post_error = str(e)

# 如果 PostUtils 失败，尝试使用 Path 模块直接导出
if not post_utils_success:
    try:
        if hasattr(job, 'Path') and job.Path:
            path_data = job.Path
            with open(nc_path, 'w') as f:
                f.write(str(path_data))
        else:
            raise Exception(f"post_process_failed:PostUtils failed ({post_error}) and Job has no path data")
    except Exception as e2:
        raise Exception(f"post_process_failed:{str(e2)}")

print(f"NC_EXPORT_SUCCESS:{{nc_path}}")
print(f"NC_EXPORT_DIR:{{export_dir_full}}")
"""
        res = freecad.execute_code(script)

        if not res.get("success", False):
            error_msg = res.get("error", res.get("message", "Unknown error"))
            return json.dumps({
                "success": False,
                "error": f"Failed to export NC: {error_msg}"
            }, ensure_ascii=False, indent=2)

        output = res.get("message", "")
        nc_path = ""
        export_dir_result = ""

        for line in output.split("\n"):
            if line.startswith("NC_EXPORT_SUCCESS:"):
                nc_path = line.split(":", 1)[1].strip()
            elif line.startswith("NC_EXPORT_DIR:"):
                export_dir_result = line.split(":", 1)[1].strip()

        return json.dumps({
            "success": True,
            "message": "NC exported successfully",
            "data": {
                "nc_path": nc_path,
                "dir": export_dir_result
            }
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to export NC: {str(e)}")
        return json.dumps({
            "success": False,
            "error": f"Failed to export NC: {str(e)}"
        }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FreeCAD SSE MCP Server")
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind to (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--enable-coding-tools",
        action="store_true",
        help="Deprecated compatibility flag; open_cad_file is always registered"
    )

    args = parser.parse_args()

    # 更新服务器 host 和 port（FastMCP 已在创建时使用默认值，这里覆盖）
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    logger.info(f"Starting FreeCAD SSE MCP server on {args.host}:{args.port}")
    logger.info(f"Connecting to FreeCAD at {freecad_addr}:{freecad_port}")
    
    # 测试 FreeCAD 连接（带重试，等待 FreeCAD RPC 启动就绪）
    max_retries = 15
    retry_delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            freecad = get_freecad_connection()
            logger.info("Successfully connected to FreeCAD")
            break
        except Exception as e:
            if attempt < max_retries:
                logger.warning(
                    f"FreeCAD connection attempt {attempt}/{max_retries} failed: {e}. "
                    f"Retrying in {retry_delay}s..."
                )
                import time as _time
                _time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to FreeCAD after {max_retries} attempts: {e}")
                logger.warning("Server will start but FreeCAD operations will fail until connection is established")
    
    # 启动 SSE 服务器
    # ASGI 中间件：在内部重写 /messages → /messages/，避免 Starlette 的 307 重定向
    # MCP Inspector proxy 的 fetch 不跟随 POST 307，会导致 -32001 fetch failed
    import uvicorn

    class _FixSlashMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope.get("path") == "/messages":
                scope["path"] = "/messages/"
            await self.app(scope, receive, send)

    sse_app = mcp.sse_app()
    sse_app = _FixSlashMiddleware(sse_app)
    uvicorn.run(sse_app, host=args.host, port=args.port)
