"""Start the WASPID Infrastructure MCP server (stdio) from any checkout folder.

  python serve_mcp.py        # same as `python -m waspid.mcp_server.server`, but works
                             # whatever this folder is called (e.g. waspid-runbook-executor)
"""
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: E402,F401 — makes `waspid.*` importable

runpy.run_module("waspid.mcp_server.server", run_name="__main__")
