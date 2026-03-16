"""MCP Server implementation for crash dump analysis."""

from __future__ import annotations

import atexit
import logging
import traceback
from typing import TYPE_CHECKING

import anyio
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    ErrorData,
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    TextContent,
    Tool,
)
from pydantic import BaseModel, Field

from .prompts import load_prompt
from .tools import (
    handle_analyze_dump,
    handle_close_dump,
    handle_create_repo_pr,
    handle_create_shared_patch,
    handle_list_dumps,
    handle_open_dump,
    handle_run_cmd,
)
from .tools.debugger_tools import (
    cleanup_all_sessions,
    set_max_concurrent_sessions,
)

if TYPE_CHECKING:
    from .config import ServerConfig

logger = logging.getLogger(__name__)

try:
    from .graph import build_crash_analysis_graph  # noqa: F401

    _LANGGRAPH_AVAILABLE = True
except ImportError:
    _LANGGRAPH_AVAILABLE = False


# ============================================================================
# Pydantic Models for Tool Parameters
# ============================================================================


class OpenDumpParams(BaseModel):
    """Parameters for analyzing a crash dump."""

    dump_path: str = Field(description="Path to the crash dump file (.dmp, core dump, or .crash)")
    symbols_path: str | None = Field(
        default=None,
        description="Optional symbols/debug info path for this dump analysis",
    )
    image_path: str | None = Field(
        default=None,
        description="Optional executable image path for this dump analysis",
    )
    repo_path: str | None = Field(
        default=None,
        description="Local repository path to locate faulting source files (searches all files including gitignored ones)",
    )
    include_stack_trace: bool = Field(default=True, description="Include stack traces")
    include_modules: bool = Field(default=True, description="Include loaded modules")
    include_threads: bool = Field(default=True, description="Include thread information")


class AnalyzeDumpParams(OpenDumpParams):
    """Parameters for one-shot crash dump analysis."""

    pass


class RunCommandParams(BaseModel):
    """Parameters for executing a debugger command."""

    dump_path: str = Field(description="Path to the crash dump file")
    symbols_path: str | None = Field(
        default=None,
        description="Optional symbols path override for session creation/replacement",
    )
    image_path: str | None = Field(
        default=None,
        description="Optional image path override for session creation/replacement",
    )
    command: str = Field(description="Debugger command to execute")
    timeout: int | None = Field(
        default=None,
        ge=1,
        description="Optional per-command timeout in seconds",
    )


class CloseDumpParams(BaseModel):
    """Parameters for closing a dump session."""

    dump_path: str = Field(description="Path to the crash dump file to close")


class ListDumpsParams(BaseModel):
    """Parameters for listing crash dumps."""

    directory_path: str | None = Field(
        default=None, description="Directory to search (defaults to system dump path)"
    )
    recursive: bool = Field(default=False, description="Search subdirectories")


class CreateRepoPrParams(BaseModel):
    """Parameters for creating a PR from local repository changes.

    The PR body is built from .github/pull_request_template.md.
    """

    commit_message: str = Field(description="Commit message for the staged changes")
    pr_title: str = Field(description="Pull request title")
    jira_id: str | None = Field(
        default=None,
        description="Issue tracker ticket ID (e.g. APP-12345). Fills the JIRA LINK section.",
    )
    release_note: str | None = Field(
        default=None,
        description="Public-facing release note. Fills the PUBLIC RELEASE NOTE section.",
    )
    test_impact: str | None = Field(
        default=None,
        description="Test impact or testing recommendations. Fills the TEST IMPACT section.",
    )
    issue_description: str | None = Field(
        default=None,
        description="Description of the problem or requirement (root cause, crash details, etc.). Fills the Issue sub-section under DEV DESCRIPTION.",
    )
    changes_description: str | None = Field(
        default=None,
        description="Summary of changes made to fix the issue. Fills the 'What are the changes' sub-section under DEV DESCRIPTION.",
    )
    follow_ups: str | None = Field(
        default=None,
        description="Pending scenarios or related tickets. Fills the Follow-ups sub-section under DEV DESCRIPTION.",
    )
    reviewer: str | None = Field(default=None, description="Optional GitHub reviewer username")
    repo_path: str | None = Field(
        default=None, description="Repository path (defaults to current working directory)"
    )
    branch_name: str | None = Field(
        default=None,
        description="Branch name matching 'users/agent/<fix_feature>'. Auto-generated in this format if omitted.",
    )
    base_branch: str = Field(default="main", description="Base branch for the pull request")
    auto_create_branch: bool = Field(
        default=True,
        description="If true, auto-create/switch to a branch when currently on main/master or detached HEAD",
    )
    stage_all: bool = Field(default=True, description="If true, run `git add -A` before committing")
    exclude_markdown_files: bool = Field(
        default=True,
        description="If true, automatically unstage all .md files before commit.",
    )
    include_gitignored_files: bool = Field(
        default=False,
        description="Deprecated safety flag. Gitignored files are never force-added by create_repo_pr.",
    )
    create_suggested_changes_md_when_no_commit: bool = Field(
        default=True,
        description="If true, create a suggested-changes markdown file when no commitable files are found.",
    )
    suggested_changes_md_path: str | None = Field(
        default=None,
        description="Optional output path for suggested changes markdown. Relative paths are resolved from repo root.",
    )
    handle_shared_component_changes: bool = Field(
        default=True,
        description="If true, detect gitignored shared-component changes and generate a shared patch markdown.",
    )
    exclude_submodule_changes: bool = Field(
        default=True,
        description="If true, ignore submodule pointer/dirty changes when deciding whether PR creation is allowed.",
    )
    external_dependency_path_hints: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative path prefixes to treat as external dependencies and exclude from PR gating "
            "(e.g. ['third_party/', 'external/', 'vendor/'])."
        ),
    )
    shared_component_path_hints: list[str] = Field(
        default_factory=list,
        description="Repo-relative prefixes considered shared components for patch generation (e.g. ['vendor/', 'third_party/']).",
    )
    shared_patch_output_path: str | None = Field(
        default=None,
        description="Optional output path for shared patch markdown. Relative paths are resolved from repo root.",
    )


class CreateSharedPatchParams(BaseModel):
    """Parameters for creating a markdown patch summary for shared/gitignored changes."""

    repo_path: str | None = Field(
        default=None, description="Repository path (defaults to current working directory)"
    )
    jira_id: str | None = Field(default=None, description="Optional issue tracker ticket ID")
    issue_description: str | None = Field(
        default=None,
        description="Problem/analysis summary to include in the patch document.",
    )
    changes_description: str | None = Field(
        default=None,
        description="Suggested code changes for shared/gitignored paths.",
    )
    follow_ups: str | None = Field(
        default=None,
        description="Follow-up tasks or validation notes.",
    )
    shared_component_path_hints: list[str] = Field(
        default_factory=list,
        description="Repo-relative path prefixes treated as shared components (usually gitignored, e.g. ['vendor/', 'third_party/']).",
    )
    patch_output_path: str | None = Field(
        default=None,
        description="Optional output path for generated markdown patch file. Relative to repo root if not absolute.",
    )


class AutoTriageParams(BaseModel):
    """Parameters for the autonomous LangGraph crash triage pipeline."""

    dump_path: str = Field(description="Path to the crash dump file")
    symbols_path: str | None = Field(default=None, description="Optional symbols path")
    image_path: str | None = Field(default=None, description="Optional executable image path")
    repo_path: str | None = Field(
        default=None, description="Local repository path for source lookup and PR/patch"
    )
    jira_id: str | None = Field(default=None, description="Optional issue tracker ticket ID")


atexit.register(cleanup_all_sessions)


# ============================================================================
# MCP Server
# ============================================================================


async def serve(
    cdb_path: str | None = None,
    symbols_path: str | None = None,
    image_path: str | None = None,
    repo_path: str | None = None,
    timeout: int = 30,
    verbose: bool = False,
    *,
    config: ServerConfig | None = None,
) -> None:
    """Run the MCP server with stdio transport."""
    debugger_type = "auto"
    debugger_path = None

    if config is not None:
        debugger_type = config.debugger_type
        debugger_path = config.effective_debugger_path
        cdb_path = config.cdb_path
        symbols_path = config.symbols_path
        image_path = config.image_path
        repo_path = config.repo_path
        timeout = config.timeout
        verbose = config.verbose
        set_max_concurrent_sessions(config.max_concurrent_sessions)

    server = Server("triagepilot")

    debugger_ctx = dict(
        cdb_path=cdb_path,
        debugger_path=debugger_path,
        debugger_type=debugger_type,
        symbols_path=symbols_path,
        image_path=image_path,
        repo_path=repo_path,
        timeout=timeout,
        verbose=verbose,
    )

    # -------------------------------------------------------------------------
    # Tools
    # -------------------------------------------------------------------------

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools = [
            Tool(
                name="analyze_dump",
                description="One-shot crash dump analysis with optional per-call symbols/image paths. Supports Windows (.dmp), Linux (core), and macOS (.crash) dumps.",
                inputSchema=AnalyzeDumpParams.model_json_schema(),
            ),
            Tool(
                name="open_dump",
                description="Open a crash dump file and run initial analysis commands.",
                inputSchema=OpenDumpParams.model_json_schema(),
            ),
            Tool(
                name="run_debugger_cmd",
                description="Execute a debugger command on a loaded dump session.",
                inputSchema=RunCommandParams.model_json_schema(),
            ),
            Tool(
                name="close_dump",
                description="Close a crash dump session and release resources.",
                inputSchema=CloseDumpParams.model_json_schema(),
            ),
            Tool(
                name="list_dumps",
                description="List crash dump files in a directory. Detects platform-appropriate file types.",
                inputSchema=ListDumpsParams.model_json_schema(),
            ),
            Tool(
                name="create_repo_pr",
                description="Create a git commit, push branch, and open a GitHub PR from current repository changes.",
                inputSchema=CreateRepoPrParams.model_json_schema(),
            ),
            Tool(
                name="create_shared_patch",
                description="Create a markdown patch summary for shared/gitignored component changes.",
                inputSchema=CreateSharedPatchParams.model_json_schema(),
            ),
        ]
        if _LANGGRAPH_AVAILABLE:
            tools.append(
                Tool(
                    name="auto_triage_dump",
                    description=(
                        "Autonomous end-to-end crash dump triage using a LangGraph workflow. "
                        "Runs debugger analysis, extracts metadata, performs LLM-based root cause "
                        "analysis, suggests fixes, and optionally creates a PR or shared patch."
                    ),
                    inputSchema=AutoTriageParams.model_json_schema(),
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            if name in ("analyze_dump", "analyze_windbg_dump"):
                return await handle_analyze_dump(
                    arguments, **debugger_ctx, AnalyzeDumpParams=AnalyzeDumpParams
                )

            elif name in ("open_dump", "open_windbg_dump"):
                return await handle_open_dump(
                    arguments, **debugger_ctx, OpenDumpParams=OpenDumpParams
                )

            elif name in ("run_debugger_cmd", "run_windbg_cmd"):
                return await handle_run_cmd(
                    arguments, **debugger_ctx, RunCommandParams=RunCommandParams
                )

            elif name in ("close_dump", "close_windbg_dump"):
                return await handle_close_dump(arguments, CloseDumpParams=CloseDumpParams)

            elif name in ("list_dumps", "list_windbg_dumps"):
                return await handle_list_dumps(
                    arguments, debugger_type=debugger_type, ListDumpsParams=ListDumpsParams
                )

            elif name == "create_shared_patch":
                return await handle_create_shared_patch(
                    arguments, CreateSharedPatchParams=CreateSharedPatchParams
                )

            elif name == "create_repo_pr":
                return await handle_create_repo_pr(arguments, CreateRepoPrParams=CreateRepoPrParams)

            elif name == "auto_triage_dump" and _LANGGRAPH_AVAILABLE:
                import asyncio

                args = AutoTriageParams(**arguments)
                include_llm = bool(config and config.llm_api_key) if config is not None else False

                graph = build_crash_analysis_graph(include_llm_nodes=include_llm)

                initial_state = {
                    "dump_path": args.dump_path,
                    "symbols_path": args.symbols_path or symbols_path,
                    "image_path": args.image_path or image_path,
                    "repo_path": args.repo_path or repo_path,
                    "jira_id": args.jira_id,
                    "debugger_path": debugger_path or cdb_path,
                    "debugger_type": debugger_type,
                    "timeout": timeout,
                    "verbose": verbose,
                    "max_retries": config.max_retries if config else 3,
                    "retry_count": 0,
                    "errors": [],
                    "status": "analyzing",
                }

                if include_llm and config is not None:
                    initial_state.update(
                        {
                            "llm_provider": config.llm_provider,
                            "llm_model": config.llm_model,
                            "llm_api_key": config.llm_api_key,
                        }
                    )

                final_state = await asyncio.to_thread(graph.invoke, initial_state)
                report = final_state.get("report", "No report generated.")
                return [TextContent(type="text", text=report)]

            raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown tool: {name}"))

        except McpError:
            raise
        except Exception as e:
            raise McpError(
                ErrorData(code=INTERNAL_ERROR, message=f"Error: {str(e)}\n{traceback.format_exc()}")
            )

    # -------------------------------------------------------------------------
    # Prompts
    # -------------------------------------------------------------------------

    @server.list_prompts()
    async def list_prompts() -> list[Prompt]:
        return [
            Prompt(
                name="dump-triage",
                title="Crash Dump Triage Analysis",
                description="Comprehensive crash dump analysis with detailed reporting (Windows/Linux/macOS)",
                arguments=[
                    PromptArgument(
                        name="dump_path",
                        description="Path to the crash dump file (optional)",
                        required=False,
                    ),
                    PromptArgument(
                        name="symbols_path",
                        description="Optional symbols path for this analysis",
                        required=False,
                    ),
                    PromptArgument(
                        name="image_path",
                        description="Optional executable image path for this analysis",
                        required=False,
                    ),
                    PromptArgument(
                        name="repo_path",
                        description="Optional repository path for faulting source lookup and code changes",
                        required=False,
                    ),
                    PromptArgument(
                        name="jira_id",
                        description="Optional issue tracker ticket ID to reuse for patch/PR tools",
                        required=False,
                    ),
                ],
            ),
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None) -> GetPromptResult:
        if arguments is None:
            arguments = {}

        if name == "dump-triage":
            dump_path = arguments.get("dump_path", "")
            symbols_path_arg = arguments.get("symbols_path", "")
            image_path_arg = arguments.get("image_path", "")
            repo_path_arg = arguments.get("repo_path", "")
            jira_id_arg = arguments.get("jira_id", "")
            try:
                prompt_content = load_prompt("dump-triage")
            except FileNotFoundError as e:
                raise McpError(
                    ErrorData(code=INTERNAL_ERROR, message=f"Prompt file not found: {e}")
                )

            context_lines = []
            if dump_path:
                context_lines.append(f"**Dump file to analyze:** {dump_path}")
            if symbols_path_arg:
                context_lines.append(f"**Symbols path (for this run):** {symbols_path_arg}")
            if image_path_arg:
                context_lines.append(f"**Image path (for this run):** {image_path_arg}")
            if repo_path_arg:
                context_lines.append(f"**Repo path (for this run):** {repo_path_arg}")
            if jira_id_arg:
                context_lines.append(f"**Ticket ID (for this run):** {jira_id_arg}")

            if context_lines:
                prompt_text = "\n".join(context_lines) + "\n\n" + prompt_content
            else:
                prompt_text = prompt_content

            return GetPromptResult(
                description="Comprehensive crash dump analysis workflow",
                messages=[
                    PromptMessage(
                        role="user",
                        content=TextContent(type="text", text=prompt_text),
                    ),
                ],
            )

        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Unknown prompt: {name}"))

    # -------------------------------------------------------------------------
    # Run Server
    # -------------------------------------------------------------------------

    options = server.create_initialization_options()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, options, raise_exceptions=True)
    except (anyio.BrokenResourceError, Exception) as exc:
        # Swallow broken-pipe errors that occur when the MCP client disconnects;
        # re-raise anything unexpected.
        if not isinstance(exc, anyio.BrokenResourceError):
            raise
