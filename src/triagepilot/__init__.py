"""TriagePilot - AI-powered cross-platform crash dump triage MCP server."""

from .server import serve


def main():
    """MCP crash dump analysis server -- supports CDB (Windows), LLDB (macOS/Linux), and GDB (Linux)."""
    import argparse
    import asyncio

    from .config import ServerConfig
    from .logging_config import configure_logging

    parser = argparse.ArgumentParser(
        description="TriagePilot - AI-powered crash dump triage MCP server (Windows/Linux/macOS)"
    )
    parser.add_argument(
        "--debugger-type",
        type=str,
        default=None,
        choices=["auto", "cdb", "lldb", "gdb"],
        help="Debugger backend to use (default: auto-detect by platform)",
    )
    parser.add_argument("--debugger-path", type=str, help="Custom path to the debugger executable")
    parser.add_argument(
        "--cdb-path",
        type=str,
        help="Custom path to cdb.exe (Windows, deprecated -- use --debugger-path)",
    )
    parser.add_argument(
        "--symbols-path",
        type=str,
        help="Path to symbol/debug info files, prepended to symbol search path",
    )
    parser.add_argument(
        "--image-path", type=str, help="Path to executable/binary images for analysis"
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        help="Local repository path to locate faulting source files (bypasses .gitignore)",
    )
    parser.add_argument("--timeout", type=int, default=None, help="Command timeout in seconds")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--log-level", type=str, default=None, help="Log level (DEBUG, INFO, WARNING, ERROR)"
    )

    args = parser.parse_args()

    overrides = {
        k: v
        for k, v in {
            "debugger_type": args.debugger_type,
            "debugger_path": args.debugger_path,
            "cdb_path": args.cdb_path,
            "symbols_path": args.symbols_path,
            "image_path": args.image_path,
            "repo_path": args.repo_path,
            "timeout": args.timeout,
            "verbose": args.verbose or None,
            "log_level": args.log_level,
        }.items()
        if v is not None
    }

    config = ServerConfig(**overrides)

    configure_logging(log_level=config.log_level, verbose=config.verbose)

    asyncio.run(serve(config=config))


if __name__ == "__main__":
    main()
