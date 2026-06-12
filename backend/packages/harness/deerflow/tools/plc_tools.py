"""PLC Write-and-Validate Tool: Forces TwinCAT validation after every file write.

This tool wraps filesystem write operations with automatic TwinCAT validation,
ensuring that generated PLC code is ALWAYS validated - no way for the LLM to skip it.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Optional, Type

from langchain.tools import BaseTool, InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT
from pydantic import BaseModel, Field

from deerflow.agents.thread_state import ThreadState

logger = logging.getLogger(__name__)

# 虚拟路径前缀
VIRTUAL_PATH_PREFIX = "/mnt/user-data"


class PLCWriteFileInput(BaseModel):
    """Input schema for plc_write_file tool."""
    
    file_path: str = Field(description="Path to write the PLC file (e.g., FB_MotorControl.TcPOU)")
    content: str = Field(description="Content of the PLC file to write")
    validation_level: str = Field(
        default="all", 
        description="Validation level: 'all' (45 checks), 'critical', or 'style'"
    )
    auto_fix: bool = Field(
        default=True,
        description="Automatically fix issues found during validation"
    )


def _get_mcp_tool(tool_name: str) -> Optional[BaseTool]:
    """Get an MCP tool by name from cached tools."""
    try:
        from deerflow.mcp.cache import get_cached_mcp_tools
        
        tools = get_cached_mcp_tools()
        for tool in tools:
            if tool.name == tool_name:
                return tool
        return None
    except Exception as e:
        logger.warning(f"Failed to get MCP tool '{tool_name}': {e}")
        return None


def _invoke_mcp_tool(tool_name: str, **kwargs) -> str:
    """Invoke an MCP tool and return its result."""
    tool = _get_mcp_tool(tool_name)
    if tool is None:
        return f"Error: Tool '{tool_name}' not found in MCP tools"
    
    try:
        result = tool.invoke(kwargs)
        if isinstance(result, dict):
            return json.dumps(result, indent=2, ensure_ascii=False)
        return str(result)
    except Exception as e:
        return f"Error executing '{tool_name}': {e}"


def _get_thread_id(runtime: ToolRuntime[ContextT, ThreadState]) -> str | None:
    """Resolve the current thread id from runtime context or RunnableConfig."""
    thread_id = runtime.context.get("thread_id") if runtime.context else None
    if thread_id:
        return thread_id

    runtime_config = getattr(runtime, "config", None) or {}
    thread_id = runtime_config.get("configurable", {}).get("thread_id")
    if thread_id:
        return thread_id

    try:
        from langgraph.config import get_config
        return get_config().get("configurable", {}).get("thread_id")
    except RuntimeError:
        return None


def _build_thread_aware_path(file_path: str, thread_id: str | None) -> str:
    """Build a file path that includes thread ID for organization.
    
    Args:
        file_path: Original file path (e.g., /mnt/user-data/workspace/FB_Test.TcPOU)
        thread_id: Current thread ID
        
    Returns:
        Modified path with thread ID subdirectory (e.g., /mnt/user-data/workspace/thread_abc123/FB_Test.TcPOU)
    """
    if not thread_id:
        return file_path
    
    # Parse the path
    path = Path(file_path)
    
    # If path already starts with thread_ prefix, don't modify it
    if any(part.startswith("thread_") for part in path.parts):
        return file_path
    
    # Build thread-specific directory name
    thread_dir = f"thread_{thread_id}"
    
    # Insert thread directory before the filename
    # e.g., /mnt/user-data/workspace/FB_Test.TcPOU -> /mnt/user-data/workspace/thread_xxx/FB_Test.TcPOU
    new_path = path.parent / thread_dir / path.name
    
    return str(new_path)


@tool("plc_write_file", parse_docstring=True)
def plc_write_file_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    file_path: str,
    content: str,
    validation_level: str = "all",
    auto_fix: bool = True,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
) -> Command:
    """Write a PLC file AND automatically validate it with TwinCAT.

    This tool combines filesystem write with mandatory TwinCAT validation.
    The LLM CANNOT skip validation - it's enforced at the code level.

    After writing, this tool will:
    1. Save the file to disk
    2. Run TwinCAT validation (45 automated checks)
    3. Auto-fix common issues (if auto_fix=True)
    4. Return the complete validation report with health score
    5. Automatically present the file for user preview

    Use this for ALL PLC file generation - never use raw filesystem_write_file for .TcPOU/.TcDUT files!

    Args:
        file_path: Path to write the PLC file (e.g., FB_MotorControl.TcPOU)
        content: Content of the PLC file to write
        validation_level: Validation level: 'all' (45 checks), 'critical', or 'style'
        auto_fix: Automatically fix issues found during validation
    """
    # Get thread ID for organizing files
    thread_id = _get_thread_id(runtime)
    
    # Build thread-aware path
    actual_file_path = _build_thread_aware_path(file_path, thread_id)
    
    results = []
    results.append(f"## 📝 PLC File Write & Validate: {file_path}\n")
    if thread_id and actual_file_path != file_path:
        results.append(f"📁 **Thread ID:** `{thread_id}`\n")
        results.append(f"📂 **Saving to:** `{actual_file_path}`\n")
    
    # Step 1: Write the file using filesystem MCP tool
    results.append("### Step 1: Writing file...\n")
    write_result = _invoke_mcp_tool("filesystem_write_file", path=actual_file_path, content=content)
    results.append(f"**Write Result:** {write_result[:200]}...\n")
    
    # Step 2: Validate with TwinCAT
    results.append("\n### Step 2: TwinCAT Validation (MANDATORY)\n")
    validate_result = _invoke_mcp_tool(
        "twincat-validator_validate_file",
        file_path=actual_file_path,
        validation_level=validation_level
    )
    
    # Parse and format validation result
    try:
        val_data = json.loads(validate_result) if isinstance(validate_result, str) else validate_result
        
        score = val_data.get("health_score", "N/A")
        status = val_data.get("overall_status", "unknown")
        safe_import = val_data.get("safe_to_import", "N/A")
        safe_compile = val_data.get("safe_to_compile", "N/A")
        errors = len(val_data.get("errors", []))
        warnings = len(val_data.get("warnings", []))
        
        results.append(f"| Metric | Value |")
        results.append(f"|--------|-------|")
        results.append(f"| **Health Score** | {score}/100 |")
        results.append(f"| **Status** | {status} |")
        results.append(f"| **Safe to Import** | {safe_import} |")
        results.append(f"| **Safe to Compile** | {safe_compile} |")
        results.append(f"| **Errors** | {errors} |")
        results.append(f"| **Warnings** | {warnings} |")
        
        if errors > 0:
            results.append(f"\n❌ **Errors found:**")
            for err in val_data.get("errors", [])[:5]:
                results.append(f"   - Line {err.get('line', '?')}: {err.get('message', err)}")
        
        if warnings > 0:
            results.append(f"\n⚠️ **Warnings:**")
            for warn in val_data.get("warnings", [])[:5]:
                results.append(f"   - Line {warn.get('line', '?')}: {warn.get('message', warn)}")
                
    except (json.JSONDecodeError, TypeError):
        results.append(f"**Raw Validation Result:**\n```json\n{validate_result[:1000]}\n```\n")
    
    # Step 3: Auto-fix if requested and there are fixable issues
    if auto_fix:
        results.append("\n### Step 3: Auto-Fix (if needed)\n")
        fix_result = _invoke_mcp_tool(
            "twincat-validator_autofix_file",
            file_path=actual_file_path,
            create_backup=True
        )
        
        try:
            fix_data = json.loads(fix_result) if isinstance(fix_result, str) else fix_result
            fixes_applied = fix_data.get("fixes_applied", 0)
            if fixes_applied > 0:
                results.append(f"✅ Applied {fixes_applied} automatic fix(es)")
                if fix_data.get("backup_path"):
                    results.append(f"📄 Backup created: {fix_data['backup_path']}")
            else:
                results.append("ℹ️ No auto-fixable issues found")
        except (json.JSONDecodeError, TypeError):
            results.append(f"**Fix Result:** {str(fix_result)[:200]}")
    
    # Step 4: Re-validate after fix
    if auto_fix:
        results.append("\n### Step 4: Post-Fix Validation\n")
        revalidate_result = _invoke_mcp_tool(
            "twincat-validator_get_validation_summary",
            file_path=actual_file_path
        )
        try:
            reval_data = json.loads(revalidate_result) if isinstance(revalidate_result, str) else revalidate_result
            final_score = reval_data.get("health_score", "N/A")
            results.append(f"🎯 **Final Health Score: {final_score}/100**\n")
        except (json.JSONDecodeError, TypeError):
            results.append(f"**Re-validation:** {str(revalidate_result)[:200]}")
    
    # Step 5: Present the file to user for preview
    results.append("\n### Step 5: Presenting file for preview\n")
    
    # 获取 artifacts 路径
    artifacts = []
    try:
        # 使用真实的 runtime 获取线程信息（前面已经获取过 thread_id）
        if thread_id and runtime.state:
            thread_data = runtime.state.get("thread_data") or {}
            outputs_path = thread_data.get("outputs_path")
            workspace_path = thread_data.get("workspace_path")
            
            # 规范化文件路径（使用实际的文件路径）
            actual_path = Path(actual_file_path).expanduser().resolve()
            virtual_path = None
            
            # 首先尝试从 outputs 目录计算相对路径
            if outputs_path:
                outputs_dir = Path(outputs_path).resolve()
                try:
                    relative_path = actual_path.relative_to(outputs_dir)
                    virtual_path = f"{VIRTUAL_PATH_PREFIX}/outputs/{relative_path.as_posix()}"
                except ValueError:
                    pass
            
            # 如果不在 outputs 目录，尝试从 workspace 目录计算
            if virtual_path is None and workspace_path:
                workspace_dir = Path(workspace_path).resolve()
                try:
                    relative_path = actual_path.relative_to(workspace_dir)
                    virtual_path = f"{VIRTUAL_PATH_PREFIX}/workspace/{relative_path.as_posix()}"
                except ValueError:
                    pass
            
            # 如果还是不行，尝试从 user-data 目录计算
            if virtual_path is None and outputs_path:
                # outputs_path 通常格式是 /path/to/threads/<thread_id>/user-data/outputs
                user_data_dir = Path(outputs_path).parent
                try:
                    relative_path = actual_path.relative_to(user_data_dir)
                    virtual_path = f"{VIRTUAL_PATH_PREFIX}/{relative_path.as_posix()}"
                except ValueError:
                    pass
            
            # 如果所有方法都失败，使用文件名作为备用
            if virtual_path is None:
                virtual_path = f"{VIRTUAL_PATH_PREFIX}/outputs/{Path(file_path).name}"
            
            artifacts.append(virtual_path)
            results.append(f"✅ 文件已展示: {virtual_path}")
        else:
            results.append("⚠️ 无法获取线程信息")
            
    except Exception as e:
        logger.warning(f"Failed to present file {file_path}: {e}")
        results.append(f"注意: 文件已生成但展示失败: {str(e)}")
    
    results.append("\n---\n✅ **PLC Write & Validate Complete** - Validation was MANDATORY and executed.\n")
    
    return Command(
        update={
            "artifacts": artifacts,
            "messages": [ToolMessage("\n".join(results), tool_call_id=tool_call_id)],
        },
    )


# Also create a batch version for multiple files
class PLCBatchValidateInput(BaseModel):
    """Input schema for plc_validate_batch tool."""
    
    directory: str = Field(description="Directory containing PLC files to validate")
    file_patterns: list[str] = Field(
        default=["*.TcPOU", "*.TcDUT", "*.TcGVL"],
        description="File patterns to match (e.g., ['*.TcPOU', '*.TcDUT'])"
    )
    validation_level: str = Field(default="all", description="Validation level")


class PLCBatchValidateTool(BaseTool):
    """Batch validate all PLC files in a directory."""
    
    name: str = "plc_validate_batch"
    description: str = """
Batch validate ALL PLC files in a directory using TwinCAT Validator.
Runs 45 checks on each file and returns a comprehensive quality report.

Use this after generating multiple PLC files to verify the entire project.
""".strip()
    args_schema: Type[BaseModel] = PLCBatchValidateInput
    
    def _run(self, directory: str, file_patterns: list[str] = None, validation_level: str = "all") -> str:
        """Execute batch validation."""
        if file_patterns is None:
            file_patterns = ["*.TcPOU", "*.TcDUT", "*.TcGVL"]
            
        result = _invoke_mcp_tool(
            "twincat-validator_validate_batch",
            directory=directory,
            patterns=file_patterns,
            validation_level=validation_level
        )
        
        return f"## 📦 TwinCAT Batch Validation Report\n\n{result}"


def get_plc_tools() -> list:
    """Return the PLC-specific tools that enforce TwinCAT validation."""
    return [
        plc_write_file_tool,
        PLCBatchValidateTool(),
    ]
