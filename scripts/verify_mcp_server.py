#!/usr/bin/env python3
"""Cross-platform installation smoke test for the TriagePilot MCP server.

Launches the installed server as a real subprocess over the stdio
transport and performs the standard MCP handshake (initialize ->
list_tools -> list_prompts) using the official `mcp` client SDK. This
verifies the package actually installed correctly and is reachable over
the protocol on this platform/Python combination -- not just that the
console script exists or that `--help` parses.

No real debugger (gdb/lldb/cdb) or crash dump is required: server startup
only registers tool/prompt schemas and (optionally) opens the local
SQLite memory store -- it never touches a debugger until a tool is
actually invoked with a dump path.

Usage:
    python scripts/verify_mcp_server.py
    python scripts/verify_mcp_server.py --command "uv run triagepilot"
    python scripts/verify_mcp_server.py --command "python -m triagepilot"
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Tools registered unconditionally by server.py, regardless of optional
# extras (langgraph) or whether the memory store initialized successfully.
CORE_TOOLS = {
    "analyze_dump",
    "open_dump",
    "run_debugger_cmd",
    "close_dump",
    "send_ctrl_break",
    "list_dumps",
    "create_repo_pr",
    "create_shared_patch",
}


async def verify(command: str, timeout: float) -> int:
    parts = shlex.split(command)
    server_params = StdioServerParameters(command=parts[0], args=parts[1:], env=None)

    async def _run() -> None:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                init_result = await session.initialize()
                try:
                    info = init_result.serverInfo
                    print(f"[verify] initialized: server={info.name} version={info.version}")
                except AttributeError:
                    print("[verify] initialized (server did not report name/version)")

                tools_result = await session.list_tools()
                tool_names = {t.name for t in tools_result.tools}
                print(
                    f"[verify] {len(tool_names)} tool(s) registered: "
                    f"{', '.join(sorted(tool_names))}"
                )

                missing = CORE_TOOLS - tool_names
                if missing:
                    raise RuntimeError(f"missing expected core tool(s): {sorted(missing)}")

                prompts_result = await session.list_prompts()
                print(f"[verify] {len(prompts_result.prompts)} prompt(s) registered")
                if not prompts_result.prompts:
                    raise RuntimeError("expected at least one registered prompt")

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- report every failure mode clearly
        print(f"[verify] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("[verify] OK -- MCP server installed and responding correctly")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command",
        default="triagepilot",
        help="Command used to launch the server (default: the installed console script)",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="Handshake timeout in seconds (default: 30)"
    )
    args = parser.parse_args()
    return asyncio.run(verify(args.command, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
