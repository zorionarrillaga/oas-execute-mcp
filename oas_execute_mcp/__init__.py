"""OAS-execute MCP server. Phase A.0 = stub backend; MT5 bridge backend.

Wired into Claude Code via .claude/settings.json mcpServers entry:
    "oas-execute": {
        "command": "/path/to/.venv/bin/python",
        "args": ["-m", "oas_execute_mcp.server"],
        "cwd": "/path/to/parent-of-this-package"
    }
"""
