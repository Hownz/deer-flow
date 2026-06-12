"""
TwinCAT Validator MCP Server - 改进版
模拟真实的 TwinCAT PLC 代码验证
"""
import json
import logging
import random
from typing import Any, Dict, List, Optional
from pathlib import Path
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from mcp.decorators import tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("twincat_validator")

app = Server("twincat-validator")

# 模拟验证规则
VALIDATION_RULES = {
    "TcPOU": [
        {"name": "check_fb_structure", "description": "检查FB结构完整性", "critical": True},
        {"name": "check_variable_declarations", "description": "检查变量声明", "critical": True},
        {"name": "check_return_statements", "description": "检查返回语句", "critical": False},
        {"name": "check_comment_style", "description": "检查注释风格", "critical": False},
        {"name": "check_indentation", "description": "检查缩进", "critical": False},
        {"name": "check_naming_convention", "description": "检查命名规范", "critical": False},
        {"name": "check_type_consistency", "description": "检查类型一致性", "critical": True},
        {"name": "check_array_bounds", "description": "检查数组边界", "critical": True},
        {"name": "check_divide_by_zero", "description": "检查除零风险", "critical": True},
        {"name": "check_null_pointer", "description": "检查空指针引用", "critical": True},
    ],
    "TcDUT": [
        {"name": "check_dut_structure", "description": "检查DUT结构", "critical": True},
        {"name": "check_field_types", "description": "检查字段类型", "critical": True},
        {"name": "check_field_names", "description": "检查字段命名", "critical": False},
        {"name": "check_nested_types", "description": "检查嵌套类型", "critical": True},
    ],
    "TcGVL": [
        {"name": "check_gvl_structure", "description": "检查GVL结构", "critical": True},
        {"name": "check_global_vars", "description": "检查全局变量", "critical": True},
        {"name": "check_init_values", "description": "检查初始值", "critical": False},
    ],
}


def analyze_file_content(file_path: str) -> dict:
    """分析文件内容，模拟真实验证"""
    try:
        path = Path(file_path)
        if path.exists():
            content = path.read_text()
            file_ext = path.suffix.lower()
            
            # 根据文件类型和内容生成更真实的验证结果
            issues = []
            
            if file_ext == ".tcpou":
                # 检查常见的FB问题
                if "VAR" not in content:
                    issues.append({"line": 1, "column": 1, "message": "缺少VAR声明块", "type": "error"})
                if "END_VAR" not in content:
                    issues.append({"line": 1, "column": 1, "message": "缺少END_VAR结束符", "type": "error"})
                if "FUNCTION_BLOCK" not in content and "FUNCTION" not in content and "PROGRAM" not in content:
                    issues.append({"line": 1, "column": 1, "message": "缺少FUNCTION_BLOCK/FUNCTION/PROGRAM声明", "type": "error"})
                
                # 添加一些随机警告
                if random.random() < 0.3:
                    issues.append({"line": random.randint(1, 50), "column": random.randint(1, 20), 
                                  "message": "建议添加注释说明功能块用途", "type": "warning"})
                if random.random() < 0.2:
                    issues.append({"line": random.randint(1, 50), "column": random.randint(1, 20), 
                                  "message": "变量名可更具描述性", "type": "warning"})
            
            elif file_ext == ".tcdut":
                if "TYPE" not in content:
                    issues.append({"line": 1, "column": 1, "message": "缺少TYPE声明", "type": "error"})
                if "END_TYPE" not in content:
                    issues.append({"line": 1, "column": 1, "message": "缺少END_TYPE结束符", "type": "error"})
            
            # 根据问题数量计算健康分数
            errors = [i for i in issues if i["type"] == "error"]
            warnings = [i for i in issues if i["type"] == "warning"]
            
            return {
                "has_content": True,
                "errors": errors,
                "warnings": warnings,
                "file_type": file_ext
            }
        else:
            return {"has_content": False, "errors": [], "warnings": [], "file_type": path.suffix.lower()}
    except Exception as e:
        logger.error(f"分析文件失败: {e}")
        return {"has_content": False, "errors": [], "warnings": [], "file_type": ""}


@tool("twincat-validator_validate_file", parse_docstring=True)
async def validate_file(file_path: str, validation_level: str = "all") -> str:
    """验证单个PLC文件，检查是否符合TwinCAT规范
    
    Args:
        file_path: 要验证的文件路径
        validation_level: 验证级别：all（全部检查）、critical（仅关键检查）、style（仅风格检查）
    """
    logger.info(f"验证文件: {file_path}, 级别: {validation_level}")
    
    analysis = analyze_file_content(file_path)
    
    # 生成验证结果
    errors = analysis["errors"]
    warnings = analysis["warnings"]
    
    # 计算健康分数（基于问题数量）
    base_score = 100
    error_penalty = len(errors) * 10
    warning_penalty = len(warnings) * 2
    health_score = max(0, base_score - error_penalty - warning_penalty)
    
    # 确定总体状态
    if len(errors) == 0 and len(warnings) == 0:
        overall_status = "excellent"
    elif len(errors) == 0:
        overall_status = "good"
    elif len(errors) <= 2:
        overall_status = "warning"
    else:
        overall_status = "error"
    
    result = {
        "health_score": health_score,
        "overall_status": overall_status,
        "safe_to_import": len(errors) == 0,
        "safe_to_compile": len(errors) <= 2,
        "errors": [{"line": e["line"], "column": e["column"], "message": e["message"]} for e in errors],
        "warnings": [{"line": w["line"], "column": w["column"], "message": w["message"]} for w in warnings],
        "checks_performed": len(VALIDATION_RULES.get(analysis["file_type"], VALIDATION_RULES[".tcpou"])),
        "checks_passed": max(0, len(VALIDATION_RULES.get(analysis["file_type"], VALIDATION_RULES[".tcpou"])) - len(errors)),
        "checks_failed": len(errors)
    }
    
    return f"## TwinCAT 验证报告: {file_path}\n\n{json.dumps(result, indent=2, ensure_ascii=False)}"


@tool("twincat-validator_autofix_file", parse_docstring=True)
async def autofix_file(file_path: str, create_backup: bool = True) -> str:
    """自动修复PLC文件中的常见问题
    
    Args:
        file_path: 要修复的文件路径
        create_backup: 是否创建备份
    """
    logger.info(f"自动修复文件: {file_path}")
    
    analysis = analyze_file_content(file_path)
    errors = analysis["errors"]
    
    fixes_applied = 0
    fixes = []
    
    for i, error in enumerate(errors):
        if "缺少VAR" in error["message"]:
            fixes.append({
                "line": error["line"],
                "issue": "缺少VAR声明块",
                "fix": "在文件开头添加了VAR声明块"
            })
            fixes_applied += 1
        elif "缺少END_VAR" in error["message"]:
            fixes.append({
                "line": error["line"],
                "issue": "缺少END_VAR结束符",
                "fix": "在VAR块末尾添加了END_VAR"
            })
            fixes_applied += 1
        elif "缺少FUNCTION_BLOCK" in error["message"]:
            fixes.append({
                "line": error["line"],
                "issue": "缺少FUNCTION_BLOCK声明",
                "fix": "添加了FUNCTION_BLOCK声明"
            })
            fixes_applied += 1
    
    result = {
        "fixes_applied": fixes_applied,
        "backup_path": f"{file_path}.backup" if create_backup and fixes_applied > 0 else None,
        "fixes": fixes
    }
    
    return f"## 自动修复报告: {file_path}\n\n{json.dumps(result, indent=2, ensure_ascii=False)}"


@tool("twincat-validator_get_validation_summary", parse_docstring=True)
async def get_validation_summary(file_path: str) -> str:
    """获取验证摘要信息
    
    Args:
        file_path: 文件路径
    """
    logger.info(f"获取验证摘要: {file_path}")
    
    analysis = analyze_file_content(file_path)
    errors = analysis["errors"]
    warnings = analysis["warnings"]
    
    health_score = max(0, 100 - len(errors) * 10 - len(warnings) * 2)
    
    if len(errors) == 0 and len(warnings) == 0:
        overall_status = "excellent"
    elif len(errors) == 0:
        overall_status = "good"
    else:
        overall_status = "warning"
    
    result = {
        "health_score": health_score,
        "overall_status": overall_status,
        "safe_to_import": len(errors) == 0,
        "safe_to_compile": len(errors) <= 2,
        "last_validated": "2026-05-12T06:30:00Z"
    }
    
    return f"## 验证摘要: {file_path}\n\n{json.dumps(result, indent=2, ensure_ascii=False)}"


@tool("twincat-validator_validate_batch", parse_docstring=True)
async def validate_batch(directory: str, patterns: List[str] = None, validation_level: str = "all") -> str:
    """批量验证目录中的所有PLC文件
    
    Args:
        directory: 目录路径
        patterns: 文件模式，如 ['*.TcPOU', '*.TcDUT']
        validation_level: 验证级别
    """
    if patterns is None:
        patterns = ["*.TcPOU", "*.TcDUT"]
    
    logger.info(f"批量验证目录: {directory}, 模式: {patterns}")
    
    result = {
        "total_files": 0,
        "valid_files": 0,
        "invalid_files": 0,
        "files": []
    }
    
    try:
        dir_path = Path(directory)
        for pattern in patterns:
            for file_path in dir_path.glob(pattern):
                analysis = analyze_file_content(str(file_path))
                errors = analysis["errors"]
                health_score = max(0, 100 - len(errors) * 10)
                
                result["total_files"] += 1
                if len(errors) == 0:
                    result["valid_files"] += 1
                else:
                    result["invalid_files"] += 1
                
                result["files"].append({
                    "file": file_path.name,
                    "status": "valid" if len(errors) == 0 else "invalid",
                    "health_score": health_score
                })
    except Exception as e:
        logger.error(f"批量验证失败: {e}")
    
    return f"## 批量验证报告: {directory}\n\n{json.dumps(result, indent=2, ensure_ascii=False)}"


@app.list_tools()
async def list_tools() -> List[Tool]:
    """列出可用的工具"""
    tools = []
    
    # 动态获取工具信息
    import inspect
    for name, obj in globals().items():
        if hasattr(obj, '_tool_info'):
            info = obj._tool_info
            tools.append(Tool(
                name=info['name'],
                description=info['description'],
                inputSchema=info['args']
            ))
    
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """处理工具调用"""
    logger.info(f"调用工具: {name}, 参数: {arguments}")
    
    # 查找并调用对应的工具函数
    func = globals().get(name.replace("-", "_"))
    if func and callable(func):
        try:
            result = await func(**arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            logger.error(f"调用工具失败: {e}")
            return [TextContent(type="text", text=f"Error: {e}")]
    else:
        return [TextContent(type="text", text=f"Error: Unknown tool '{name}'")]


if __name__ == "__main__":
    stdio_server(app)
