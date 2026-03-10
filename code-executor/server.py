"""
Minimal MCP code executor — exposes a single `execute_python` tool over
Streamable HTTP on port 3333. Runs submitted code in a subprocess with a
hard timeout and captures stdout + stderr.
"""

import logging
import subprocess
import sys
import textwrap

from mcp.server.fastmcp import FastMCP

# Silence per-probe noise: healthcheck GETs fire every 30s and create a new
# transport session, logging it at INFO. Not useful in steady state.
for _noisy in (
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "mcp.client.streamable_http",
    "uvicorn.access",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# uvicorn reconfigures its loggers on startup; set a filter on the access
# handler added later by patching the class-level default log config.
import uvicorn.config as _uvicorn_config
_orig_configure = _uvicorn_config.Config.configure_logging

def _patched_configure(self):
    _orig_configure(self)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

_uvicorn_config.Config.configure_logging = _patched_configure

# stateless_http=True: each request is self-contained — no session state is
# maintained between calls. This eliminates the session-termination race where
# a healthcheck probe closing its transport session can kill a concurrent
# tool call from the Lansky agent.
mcp = FastMCP("code-executor", host="0.0.0.0", port=3333, stateless_http=True)


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
