"""LangGraph node functions for the crash analysis workflow.

Each node receives the full ``CrashAnalysisState`` dict and returns a
partial dict with the keys it wants to update.
"""

from __future__ import annotations

import logging
import os

from ..tools.debugger_tools import (
    get_or_create_session,
    locate_faulting_source,
)
from ..tools.git_tools import (
    _collect_changed_paths,
    _filter_shared_paths,
    _write_shared_patch_md,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Debugger-based nodes (no LLM required)
# ---------------------------------------------------------------------------


def analyze_dump_node(state: dict) -> dict:
    """Open a debugger session and run crash analysis."""
    logger.info("analyze_dump_node: starting analysis for %s", state["dump_path"])
    try:
        session = get_or_create_session(
            dump_path=state["dump_path"],
            debugger_path=state.get("debugger_path"),
            debugger_type=state.get("debugger_type", "auto"),
            symbols_path=state.get("symbols_path"),
            image_path=state.get("image_path"),
            timeout=state.get("timeout", 30),
            verbose=state.get("verbose", False),
        )

        crash_info = session.get_crash_info()
        analyze_output = session.run_crash_analysis()

        return {
            "crash_info": crash_info,
            "analyze_output": analyze_output,
            "status": "analyzing",
        }
    except Exception as exc:
        logger.error("analyze_dump_node failed: %s", exc)
        errors = list(state.get("errors", []))
        errors.append(f"analyze_dump: {exc}")
        return {
            "errors": errors,
            "retry_count": state.get("retry_count", 0) + 1,
            "status": "error",
        }


def extract_metadata_node(state: dict) -> dict:
    """Run supplementary debugger commands to extract crash metadata."""
    logger.info("extract_metadata_node: extracting metadata")
    try:
        session = get_or_create_session(
            dump_path=state["dump_path"],
            debugger_path=state.get("debugger_path"),
            debugger_type=state.get("debugger_type", "auto"),
            symbols_path=state.get("symbols_path"),
            image_path=state.get("image_path"),
            timeout=state.get("timeout", 30),
            verbose=state.get("verbose", False),
        )

        stack_trace = session.get_stack_trace()
        modules = session.get_loaded_modules()
        threads = session.get_threads()

        metadata = {}
        debugger_type = state.get("debugger_type", "auto")
        if debugger_type == "cdb":
            metadata["vertarget"] = "\n".join(session.send_command("vertarget"))
            metadata["time"] = "\n".join(session.send_command(".time"))
            metadata["registers"] = "\n".join(session.send_command("r"))
        elif debugger_type == "gdb":
            metadata["info_proc"] = "\n".join(session.send_command("info proc"))
            metadata["registers"] = "\n".join(session.send_command("info registers"))
        elif debugger_type == "lldb":
            metadata["process_status"] = "\n".join(session.send_command("process status"))
            metadata["registers"] = "\n".join(session.send_command("register read"))
        else:
            metadata["registers"] = "\n".join(session.send_command("r"))

        return {
            "stack_trace": stack_trace,
            "modules": modules,
            "threads": threads,
            "metadata": metadata,
            "status": "analyzing",
        }
    except Exception as exc:
        logger.error("extract_metadata_node failed: %s", exc)
        errors = list(state.get("errors", []))
        errors.append(f"extract_metadata: {exc}")
        return {"errors": errors, "status": "error"}


def source_lookup_node(state: dict) -> dict:
    """Locate the faulting source file in the local repo."""
    repo_path = state.get("repo_path")
    analyze_output = state.get("analyze_output", "")

    if not repo_path or not analyze_output:
        return {"faulting_source": None}

    source_section = locate_faulting_source(analyze_output, repo_path)
    return {"faulting_source": source_section}


# ---------------------------------------------------------------------------
# LLM-based nodes
# ---------------------------------------------------------------------------


def _get_llm(state: dict):
    """Lazily instantiate the LLM based on state config."""
    provider = state.get("llm_provider", "openai")
    model = state.get("llm_model", "gpt-4o")
    api_key = state.get("llm_api_key")

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_key)
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=api_key)
    elif provider == "azure":
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(model=model, api_key=api_key)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def root_cause_node(state: dict) -> dict:
    """Use an LLM to perform root cause analysis from crash data."""
    logger.info("root_cause_node: generating root cause analysis")
    try:
        llm = _get_llm(state)

        context_parts = []
        if state.get("crash_info"):
            context_parts.append(f"## Crash Event\n{state['crash_info']}")
        if state.get("analyze_output"):
            context_parts.append(f"## Analysis Output\n{state['analyze_output']}")
        if state.get("stack_trace"):
            context_parts.append(f"## Stack Trace\n{state['stack_trace']}")
        if state.get("faulting_source"):
            context_parts.append(f"## Faulting Source\n{state['faulting_source']}")

        context = "\n\n".join(context_parts)
        prompt = (
            "You are an expert crash dump debugger. Analyze the following crash dump data "
            "and provide a detailed root cause analysis.\n\n"
            "Include:\n"
            "1. What happened (exception type, faulting instruction)\n"
            "2. Why it happened (contributing factors)\n"
            "3. The code location if identifiable\n"
            "4. Severity assessment (Critical/High/Medium/Low)\n\n"
            f"{context}"
        )

        response = llm.invoke(prompt)
        root_cause = response.content if hasattr(response, "content") else str(response)

        return {"root_cause": root_cause, "status": "diagnosing"}
    except Exception as exc:
        logger.error("root_cause_node failed: %s", exc)
        errors = list(state.get("errors", []))
        errors.append(f"root_cause: {exc}")
        return {"errors": errors, "status": "error"}


def suggest_fix_node(state: dict) -> dict:
    """Use an LLM to suggest code fixes based on the root cause."""
    logger.info("suggest_fix_node: generating fix suggestions")
    try:
        llm = _get_llm(state)

        context_parts = []
        if state.get("root_cause"):
            context_parts.append(f"## Root Cause\n{state['root_cause']}")
        if state.get("faulting_source"):
            context_parts.append(f"## Faulting Source\n{state['faulting_source']}")
        if state.get("analyze_output"):
            context_parts.append(f"## Crash Analysis\n{state['analyze_output'][:2000]}")

        context = "\n\n".join(context_parts)
        prompt = (
            "You are an expert systems programmer. Based on the root cause analysis below, "
            "suggest specific code fixes.\n\n"
            "For each fix provide:\n"
            "- file_path: the file to modify\n"
            "- description: what to change and why\n"
            "- code_before: the problematic code snippet\n"
            "- code_after: the fixed code snippet\n\n"
            "Return your answer as a structured list of fixes.\n\n"
            f"{context}"
        )

        response = llm.invoke(prompt)
        fix_text = response.content if hasattr(response, "content") else str(response)

        return {
            "suggested_fixes": [{"raw_suggestion": fix_text}],
            "status": "fixing",
        }
    except Exception as exc:
        logger.error("suggest_fix_node failed: %s", exc)
        errors = list(state.get("errors", []))
        errors.append(f"suggest_fix: {exc}")
        return {"errors": errors, "status": "error"}


# ---------------------------------------------------------------------------
# Classification / output nodes
# ---------------------------------------------------------------------------


def classify_changes_node(state: dict) -> dict:
    """Classify pending changes as shared, repo-tracked, mixed, or none."""
    repo_path = state.get("repo_path")
    if not repo_path or not os.path.isdir(repo_path):
        return {"change_type": "none"}

    try:
        non_ignored, ignored = _collect_changed_paths(repo_path)
        shared_hints: list[str] = []
        shared = _filter_shared_paths(non_ignored + ignored, shared_hints)

        has_shared = len(shared) > 0
        has_repo = len(non_ignored) > len([p for p in non_ignored if p in shared])

        if has_shared and has_repo:
            change_type = "mixed"
        elif has_shared:
            change_type = "shared"
        elif has_repo:
            change_type = "repo"
        else:
            change_type = "none"

        return {"change_type": change_type}
    except Exception as exc:
        logger.warning("classify_changes_node failed: %s", exc)
        return {"change_type": "none"}


def create_pr_node(state: dict) -> dict:
    """Placeholder: in a real pipeline this would call handle_create_repo_pr."""
    logger.info("create_pr_node: PR creation would happen here")
    return {"status": "reporting"}


def shared_patch_node(state: dict) -> dict:
    """Create a shared-component patch markdown."""
    repo_path = state.get("repo_path")
    if not repo_path:
        return {"status": "reporting"}

    try:
        non_ignored, ignored = _collect_changed_paths(repo_path)
        shared_hints: list[str] = []
        shared_paths = _filter_shared_paths(non_ignored + ignored, shared_hints)

        patch_path = _write_shared_patch_md(
            repo_path,
            state.get("jira_id"),
            state.get("root_cause"),
            state.get("suggested_fixes", [{}])[0].get("raw_suggestion", ""),
            None,
            shared_paths,
        )
        return {"patch_path": patch_path, "status": "reporting"}
    except Exception as exc:
        logger.error("shared_patch_node failed: %s", exc)
        errors = list(state.get("errors", []))
        errors.append(f"shared_patch: {exc}")
        return {"errors": errors, "status": "error"}


def summary_node(state: dict) -> dict:
    """Compile the final analysis report from all accumulated state."""
    sections = ["# Crash Dump Analysis Report\n"]

    if state.get("crash_info"):
        sections.append(f"## Crash Event\n```\n{state['crash_info']}\n```\n")

    metadata = state.get("metadata", {})
    if metadata:
        sections.append("## Metadata\n")
        for key, val in metadata.items():
            sections.append(f"### {key}\n```\n{val}\n```\n")

    if state.get("analyze_output"):
        sections.append(f"## Crash Analysis\n```\n{state['analyze_output']}\n```\n")

    if state.get("stack_trace"):
        sections.append(f"## Stack Trace\n```\n{state['stack_trace']}\n```\n")

    if state.get("faulting_source"):
        sections.append(f"## Faulting Source\n{state['faulting_source']}\n")

    if state.get("root_cause"):
        sections.append(f"## Root Cause Analysis\n{state['root_cause']}\n")

    if state.get("suggested_fixes"):
        sections.append("## Suggested Fixes\n")
        for fix in state["suggested_fixes"]:
            sections.append(f"- {fix.get('raw_suggestion', str(fix))}\n")

    if state.get("pr_url"):
        sections.append(f"## Pull Request\n{state['pr_url']}\n")
    if state.get("patch_path"):
        sections.append(f"## Shared Patch\n{state['patch_path']}\n")

    errors = state.get("errors", [])
    if errors:
        sections.append("## Errors\n")
        for err in errors:
            sections.append(f"- {err}\n")

    report = "\n".join(sections)
    return {"report": report, "status": "done"}
