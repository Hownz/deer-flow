# TwinCAT PLC Validator

## 两种启动方式

### MCP Server (stdio)
由 extensions_config.json 自动拉起，agent 通过 MCP 协议调用。
```bash
python mymcp/twincat/mcp_server.py
```

### HTTP Server (独立)
手动触发 PLC 代码验证。
```bash
python mymcp/twincat/http_server.py --host 0.0.0.0 --port 8089
```

## MCP Server 提供的工具

| 工具名 | 功能 |
|--------|------|
| twincat-validator_validate_file | 45项代码规范检查 |
| twincat-validator_autofix_file | 自动修复常见问题 |
| twincat-validator_get_validation_summary | 获取最终验证报告 |

## HTTP API

| 端点 | 方法 | 功能 |
|------|------|------|
| /validate | POST | 验证 ST 代码 |
| /validate-from-thread | POST | 从线程提取代码并验证 |
| /health | GET | 健康检查 |
