"""
FreeCAD SSE MCP Server

这是一个使用 SSE 传输方式的 FreeCAD MCP 服务器。
基于原有的 stdio 版本 server.py 转换而来。
"""

import json
import logging
import os
import xmlrpc.client
import sys
from datetime import datetime
from typing import Any, Dict, List, Union, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, ImageContent

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("FreeCADSSEMCPserver")


# FreeCAD 连接配置
freecad_addr = "localhost"
_freecad_connection = None


class FreeCADConnection:
    def __init__(self, host: str = freecad_addr, port: int = 9875):
        # Create a transport with timeout
        import socket
        transport = xmlrpc.client.Transport()
        # Set timeout on the underlying socket
        transport.timeout = 30  # 30 second timeout
        
        # Create ServerProxy with custom transport
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
        _freecad_connection = FreeCADConnection(host=freecad_addr, port=9875)
        if not _freecad_connection.ping():
            logger.error("Failed to ping FreeCAD")
            _freecad_connection = None
            raise Exception(
                "Failed to connect to FreeCAD. Make sure the FreeCAD addon is running."
            )
    return _freecad_connection


# 创建 MCP 服务器实例
mcp = FastMCP("freecad-sse-server")


@mcp.tool()
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


@mcp.tool()
def create_object(
    doc_name: str,
    obj_type: str,
    obj_name: str,
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


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FreeCAD SSE MCP Server")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port to bind to (default: 8001)"
    )
    
    
    args = parser.parse_args()
    os.environ["MCP_SSE_HOST"] = args.host
    os.environ["MCP_SSE_PORT"] = str(args.port)
    
    logger.info(f"Starting FreeCAD SSE MCP server on {args.host}:{args.port}")
    logger.info(f"Connecting to FreeCAD at {freecad_addr}:9875")
    
    # 测试 FreeCAD 连接
    try:
        freecad = get_freecad_connection()
        logger.info("Successfully connected to FreeCAD")
    except Exception as e:
        logger.error(f"Failed to connect to FreeCAD: {e}")
        logger.warning("Server will start but FreeCAD operations will fail until connection is established")
    
    # 启动 SSE 服务器
    mcp.run(transport="sse")
