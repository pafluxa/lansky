"""
Minimal MCP code executor — exposes a single `execute_python` tool over
Streamable HTTP on port 3333. Runs submitted code in a subprocess with a
hard timeout and captures stdout + stderr.
"""

import subprocess
import sys
import textwrap

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("code-executor", host="0.0.0.0", port=3333)


@mcp.tool()
def execute_python(code: str) -> str:
    """
    Execute a Python code snippet in an isolated subprocess and return
    the combined stdout and stderr output (truncated to 8 KB).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(code)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = "Error: execution timed out after 15 seconds."
    except Exception as e:
        output = f"Error: {e}"

    return output[:8192] if len(output) > 8192 else output


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
