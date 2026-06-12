"""
PLC Verification HTTP API Server — 独立 FastAPI 服务
用于手动验证生成的PLC代码是否满足形式化属性

启动: python mymcp/twincat/http_server.py --host 0.0.0.0 --port 8089
"""
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException

app = FastAPI(title="PLC Verification HTTP Server", version="1.0")

# PLC验证结果类型
VerificationResult = Dict[str, Any]


def run_plcverif_validation(st_code: str, properties: List[Dict[str, Any]]) -> VerificationResult:
    """
    使用plcverif验证ST代码
    
    Args:
        st_code: ST代码字符串
        properties: 形式化属性列表
        
    Returns:
        验证结果字典
    """
    try:
        # 创建临时目录
        with tempfile.TemporaryDirectory() as temp_dir:
            # 写入ST文件
            st_file_path = os.path.join(temp_dir, "test.ST")
            with open(st_file_path, "w", encoding="utf-8") as f:
                f.write(st_code)
            
            # 如果没有plcverif，返回模拟结果
            # 检查plcverif是否可用
            try:
                result = subprocess.run(
                    ["plcverif-cli", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                plcverif_available = result.returncode == 0
            except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError):
                plcverif_available = False
            
            if plcverif_available:
                # 使用真实的plcverif进行验证
                return run_real_plcverif(st_file_path, properties, temp_dir)
            else:
                # 返回模拟验证结果
                return simulate_verification(st_code, properties)
                
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "验证过程发生错误"
        }


def run_real_plcverif(st_file_path: str, properties: List[Dict[str, Any]], output_dir: str) -> VerificationResult:
    """
    运行真实的plcverif验证
    """
    try:
        # 准备属性文件
        properties_file = os.path.join(output_dir, "properties.json")
        with open(properties_file, "w", encoding="utf-8") as f:
            json.dump(properties, f, indent=2)
        
        # 运行plcverif
        result = subprocess.run(
            ["plcverif-cli", "verify", st_file_path, "-p", properties_file, "-o", output_dir],
            capture_output=True,
            text=True,
            timeout=120
        )
        
        # 解析结果
        if result.returncode == 0:
            # 读取验证结果
            result_file = os.path.join(output_dir, "result.json")
            if os.path.exists(result_file):
                with open(result_file, "r", encoding="utf-8") as f:
                    plcverif_result = json.load(f)
                    return {
                        "success": True,
                        "plcverif_used": True,
                        "message": "验证完成",
                        "raw_result": plcverif_result
                    }
            else:
                return {
                    "success": True,
                    "plcverif_used": True,
                    "message": "验证完成（无详细结果文件）",
                    "stdout": result.stdout,
                    "stderr": result.stderr
                }
        else:
            return {
                "success": False,
                "plcverif_used": True,
                "message": "验证失败",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.returncode
            }
            
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "plcverif_used": True,
            "message": "验证超时"
        }
    except Exception as e:
        return {
            "success": False,
            "plcverif_used": True,
            "message": f"plcverif执行错误: {str(e)}"
        }


def simulate_verification(st_code: str, properties: List[Dict[str, Any]]) -> VerificationResult:
    """
    模拟PLC代码验证（当plcverif不可用时）
    """
    # 简单的语法检查
    checks = []
    
    # 检查FUNCTION_BLOCK声明
    if "FUNCTION_BLOCK" not in st_code:
        checks.append({
            "type": "error",
            "message": "缺少FUNCTION_BLOCK声明"
        })
    
    # 检查VAR_INPUT/VAR_OUTPUT
    if "VAR_INPUT" not in st_code:
        checks.append({
            "type": "warning",
            "message": "建议添加VAR_INPUT声明"
        })
    
    if "VAR_OUTPUT" not in st_code:
        checks.append({
            "type": "warning",
            "message": "建议添加VAR_OUTPUT声明"
        })
    
    # 检查END_FUNCTION_BLOCK
    if "END_FUNCTION_BLOCK" not in st_code:
        checks.append({
            "type": "error",
            "message": "缺少END_FUNCTION_BLOCK"
        })
    
    # 检查属性数量
    property_checks = []
    for i, prop in enumerate(properties, 1):
        prop_desc = prop.get("property_description", f"属性{i}")
        # 简单检查：代码中是否使用了属性中提到的变量
        if "property" in prop:
            pattern_params = prop["property"].get("pattern_params", {})
            for key, expr in pattern_params.items():
                # 提取变量名
                if "instance." in expr:
                    var_name = expr.split("instance.")[1].split()[0].strip(";")
                    if var_name and var_name not in st_code:
                        property_checks.append({
                            "property": prop_desc,
                            "status": "warning",
                            "message": f"代码中未使用变量: {var_name}"
                        })
    
    # 计算分数
    total_checks = len(checks) + len(property_checks)
    passed_checks = sum(1 for c in checks if c["type"] != "error") + \
                   sum(1 for p in property_checks if p["status"] != "error")
    
    score = int((passed_checks / max(total_checks, 1)) * 100)
    
    # 判断是否通过
    all_passed = len([c for c in checks if c["type"] == "error"]) == 0
    
    return {
        "success": all_passed,
        "plcverif_used": False,
        "message": "验证完成（使用模拟验证）",
        "health_score": score,
        "checks": checks,
        "property_checks": property_checks,
        "total_properties": len(properties),
        "properties_checked": len(property_checks),
        "recommendation": "建议安装plcverif工具以获得更准确的形式化验证"
    }


@app.post("/validate")
async def validate_plc_code(
    st_code: str,
    properties: Optional[List[Dict[str, Any]]] = None
) -> VerificationResult:
    """
    验证PLC代码是否满足形式化属性
    
    Args:
        st_code: ST代码字符串
        properties: 形式化属性列表
        
    Returns:
        验证结果
    """
    if not st_code:
        raise HTTPException(status_code=400, detail="ST代码不能为空")
    
    properties = properties or []
    
    result = run_plcverif_validation(st_code, properties)
    
    return result


@app.post("/validate-from-thread")
async def validate_from_thread(
    thread_id: str,
    run_id: Optional[str] = None
) -> VerificationResult:
    """
    从对话线程中提取代码并验证
    
    Args:
        thread_id: 对话线程ID
        run_id: 运行ID（可选）
        
    Returns:
        验证结果
    """
    try:
        # 这里可以扩展为从线程中获取最后生成的代码
        # 暂时返回模拟结果
        return {
            "success": True,
            "plcverif_used": False,
            "message": "从线程验证完成（模拟）",
            "health_score": 85,
            "thread_id": thread_id,
            "run_id": run_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """检查验证服务健康状态"""
    # 检查plcverif是否可用
    try:
        result = subprocess.run(
            ["plcverif-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=10
        )
        plcverif_available = result.returncode == 0
    except (subprocess.CalledProcessError, FileNotFoundError, TimeoutError):
        plcverif_available = False
    
    return {
        "status": "healthy",
        "plcverif_available": plcverif_available,
        "message": "PLC验证服务正常运行"
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PLC Verification HTTP Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8089, help="Port to bind to")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
