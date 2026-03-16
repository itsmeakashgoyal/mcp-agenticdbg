"""Git/PR tool handlers for the MCP server."""

from __future__ import annotations

import logging
import os
import subprocess
import time

from mcp.shared.exceptions import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ErrorData, TextContent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

_DEFAULT_LDAP = "agent"
_GIT_CMD_TIMEOUT_SEC = max(5, int(os.environ.get("TRIAGEPILOT_GIT_CMD_TIMEOUT_SEC", "60")))


def _run_process(cmd: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess command and capture output."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=_GIT_CMD_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        stderr_text = (exc.stderr or "").strip()
        stdout_text = (exc.stdout or "").strip()
        timeout_msg = f"Command timed out after {_GIT_CMD_TIMEOUT_SEC}s: {' '.join(cmd)}"
        if stderr_text:
            timeout_msg = f"{timeout_msg}\n{stderr_text}"
        raise subprocess.CalledProcessError(
            returncode=124,
            cmd=cmd,
            output=stdout_text,
            stderr=timeout_msg,
        ) from exc


def _format_process_failure(exc: subprocess.CalledProcessError) -> str:
    """Format subprocess failure with actionable stdout/stderr details."""
    stdout = (exc.stdout or "").strip()
    stderr = (exc.stderr or "").strip()
    details = [f"Command failed: {' '.join(exc.cmd)}", f"Exit code: {exc.returncode}"]
    if stdout:
        details.append(f"stdout:\n{stdout}")
    if stderr:
        details.append(f"stderr:\n{stderr}")
    return "\n".join(details)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _normalize_rel_path(path_value: str) -> str:
    """Normalize a git-relative path for prefix matching."""
    return path_value.replace("\\", "/").lstrip("./")


def _parse_porcelain_path(line: str) -> str | None:
    """Parse path from one git status --porcelain line."""
    if len(line) < 4:
        return None
    raw = line[3:].strip()
    if not raw:
        return None
    if " -> " in raw:
        raw = raw.split(" -> ", 1)[1]
    return _normalize_rel_path(raw)


def _collect_changed_paths(repo_path: str) -> tuple[list[str], list[str]]:
    """Collect changed non-ignored and ignored paths from git status."""
    output = _run_process(["git", "status", "--porcelain", "--ignored"], cwd=repo_path).stdout
    non_ignored: list[str] = []
    ignored: list[str] = []
    for line in output.splitlines():
        parsed = _parse_porcelain_path(line)
        if not parsed:
            continue
        if line.startswith("!! "):
            ignored.append(parsed)
        else:
            non_ignored.append(parsed)
    return non_ignored, ignored


def _list_ignored_paths(repo_path: str) -> list[str]:
    """List ignored paths reported by git status."""
    output = _run_process(["git", "status", "--porcelain", "--ignored"], cwd=repo_path).stdout
    ignored: list[str] = []
    for line in output.splitlines():
        if line.startswith("!! "):
            ignored.append(_normalize_rel_path(line[3:].strip()))
    return ignored


def _filter_shared_paths(paths: list[str], hints: list[str]) -> list[str]:
    """Filter paths that match any shared-component hint prefix."""
    normalized_hints = [h.replace("\\", "/").rstrip("/") + "/" for h in hints if h.strip()]
    if not normalized_hints:
        return paths
    matched = []
    for p in paths:
        np = _normalize_rel_path(p)
        if any(np.startswith(prefix) for prefix in normalized_hints):
            matched.append(np)
    return matched


def _is_path_in_prefixes(path_value: str, prefixes: list[str]) -> bool:
    """Return True when path equals/is nested under any normalized prefix."""
    normalized = _normalize_rel_path(path_value)
    normalized_prefixes = [
        _normalize_rel_path(prefix).rstrip("/") for prefix in prefixes if prefix and prefix.strip()
    ]
    for prefix in normalized_prefixes:
        if not prefix:
            continue
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _list_submodule_paths(repo_path: str) -> list[str]:
    """List git submodule paths using git index mode 160000."""
    output = _run_process(["git", "ls-files", "--stage"], cwd=repo_path).stdout
    paths: list[str] = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        left, right = line.split("\t", 1)
        parts = left.split()
        if parts and parts[0] == "160000":
            paths.append(_normalize_rel_path(right.strip()))
    return paths


def _filter_committable_paths(
    paths: list[str],
    shared_hints: list[str],
    external_hints: list[str],
    submodule_paths: list[str],
) -> list[str]:
    """Keep only paths that should be considered committable in current repo."""
    shared = set(_filter_shared_paths(paths, shared_hints))
    external = set(_filter_shared_paths(paths, external_hints)) if external_hints else set()
    result: list[str] = []
    for path_value in paths:
        normalized = _normalize_rel_path(path_value)
        if normalized in shared:
            continue
        if normalized in external:
            continue
        if _is_path_in_prefixes(normalized, submodule_paths):
            continue
        result.append(normalized)
    return result


# ---------------------------------------------------------------------------
# PR template / markdown helpers
# ---------------------------------------------------------------------------


def _load_pr_template(repo_path: str) -> str:
    """Load the PR template, checking repo first then MCP server fallback."""
    template_candidates = [
        os.path.join(repo_path, ".github", "pull_request_template.md"),
        os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "..", ".github", "pull_request_template.md"
            )
        ),
    ]

    for template_path in template_candidates:
        if os.path.isfile(template_path):
            with open(template_path, encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                return content

    raise ValueError(
        "No pull_request_template.md found in target repo or MCP server. "
        "Place one at <repo>/.github/pull_request_template.md"
    )


def _resolve_pr_body(repo_path: str, args) -> str:
    """Build PR body strictly from template with analysis only in DEV DESCRIPTION."""
    template = _load_pr_template(repo_path)

    if args.jira_id and args.jira_id.strip():
        template = template.replace(
            "- **JIRA LINK**\n",
            f"- **JIRA LINK**\n  {args.jira_id.strip()}\n",
            1,
        )

    if args.release_note and args.release_note.strip():
        template = template.replace(
            "- **PUBLIC RELEASE NOTE**\n",
            f"- **PUBLIC RELEASE NOTE**\n  {args.release_note.strip()}\n",
            1,
        )

    if args.test_impact and args.test_impact.strip():
        template = template.replace(
            "- **TEST IMPACT**\n",
            f"- **TEST IMPACT**\n  {args.test_impact.strip()}\n",
            1,
        )

    if args.issue_description and args.issue_description.strip():
        template = template.replace(
            "    _Briefly describe the problem or requirement._",
            f"    {args.issue_description.strip()}",
            1,
        )

    if args.changes_description and args.changes_description.strip():
        template = template.replace(
            "    _Summarize the key changes made to address the issue._",
            f"    {args.changes_description.strip()}",
            1,
        )

    if args.follow_ups and args.follow_ups.strip():
        template = template.replace(
            "    _List any pending scenarios and related JIRA tickets._",
            f"    {args.follow_ups.strip()}",
            1,
        )

    return template


def _write_suggested_changes_md(repo_path: str, args, reason: str) -> str:
    """Write a markdown file with suggested changes instead of creating a PR."""
    if args.suggested_changes_md_path:
        md_path = args.suggested_changes_md_path
        if not os.path.isabs(md_path):
            md_path = os.path.join(repo_path, md_path)
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        md_path = os.path.join(repo_path, f"suggested_changes_{ts}.md")

    content_lines = [
        "# Suggested Changes (No PR Created)",
        "",
        f"**Reason:** {reason}",
        "",
        "## PR Metadata",
        f"- **Title:** {args.pr_title}",
        f"- **Commit Message:** {args.commit_message}",
        f"- **JIRA:** {args.jira_id or 'N/A'}",
        "",
        "## Suggested DEV DESCRIPTION",
        "### Issue",
        args.issue_description.strip()
        if args.issue_description
        else "_Briefly describe the problem or requirement._",
        "",
        "### What are the changes to fix this issue?",
        args.changes_description.strip()
        if args.changes_description
        else "_Summarize the key changes made to address the issue._",
        "",
        "### Follow-ups",
        args.follow_ups.strip()
        if args.follow_ups
        else "_List any pending scenarios and related JIRA tickets._",
        "",
    ]

    if args.test_impact:
        content_lines.extend(["## Test Impact", args.test_impact.strip(), ""])
    if args.release_note:
        content_lines.extend(["## Public Release Note", args.release_note.strip(), ""])

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(content_lines))

    return os.path.abspath(md_path)


def _write_shared_patch_md(
    repo_path: str,
    jira_id: str | None,
    issue_description: str | None,
    changes_description: str | None,
    follow_ups: str | None,
    shared_paths: list[str],
    output_path: str | None = None,
) -> str:
    """Write markdown patch instructions for shared/gitignored changes."""
    if output_path:
        md_path = (
            output_path if os.path.isabs(output_path) else os.path.join(repo_path, output_path)
        )
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        md_path = os.path.join(repo_path, f"shared_patch_{ts}.md")

    lines = [
        "# Shared Component Patch (No PR for these files)",
        "",
        f"- **JIRA:** {jira_id or 'N/A'}",
        "",
        "## Shared / Gitignored Paths",
    ]
    if shared_paths:
        lines.extend([f"- `{p}`" for p in shared_paths])
    else:
        lines.append("- _No matching shared/gitignored paths detected._")

    lines.extend(
        [
            "",
            "## Issue",
            issue_description.strip() if issue_description else "_Briefly describe the issue._",
            "",
            "## Suggested Changes",
            changes_description.strip()
            if changes_description
            else "_Describe suggested patch changes._",
            "",
            "## Follow-ups",
            follow_ups.strip() if follow_ups else "_List follow-up validation steps._",
            "",
        ]
    )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return os.path.abspath(md_path)


def _unstage_markdown_files(repo_path: str) -> list[str]:
    """Unstage all staged markdown files and return the list."""
    staged_output = _run_process(["git", "diff", "--cached", "--name-only"], cwd=repo_path).stdout
    staged_files = [line.strip() for line in staged_output.splitlines() if line.strip()]
    markdown_files = [f for f in staged_files if f.lower().endswith(".md")]
    for file_path in markdown_files:
        _run_process(["git", "reset", "HEAD", "--", file_path], cwd=repo_path, check=False)
    return markdown_files


def _validate_branch_name(branch: str) -> None:
    """Validate branch name strictly matches users/agent/<fix_feature>."""
    parts = branch.split("/", 2)
    if len(parts) != 3 or parts[0] != "users" or parts[1] != _DEFAULT_LDAP or not parts[2]:
        raise ValueError(
            f"Invalid branch name '{branch}'. "
            f"Expected format: users/{_DEFAULT_LDAP}/feature_or_jira"
        )
    feature = parts[2]
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789._-"
    if any(ch.lower() not in allowed for ch in feature):
        raise ValueError(
            f"Invalid branch name '{branch}'. "
            f"Feature segment must contain only letters, digits, '.', '_' or '-'."
        )


def _ensure_branch_for_pr(
    repo_path: str,
    requested_branch: str | None,
    auto_create_branch: bool,
    jira_id: str | None = None,
) -> str:
    """Ensure we are on a usable branch and return branch name."""
    current_branch = _run_process(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path
    ).stdout.strip()

    if requested_branch:
        _validate_branch_name(requested_branch)
        if requested_branch == current_branch:
            return current_branch
        branch_exists = (
            _run_process(
                ["git", "show-ref", "--verify", f"refs/heads/{requested_branch}"],
                cwd=repo_path,
                check=False,
            ).returncode
            == 0
        )
        if branch_exists:
            _run_process(["git", "checkout", requested_branch], cwd=repo_path)
        else:
            _run_process(["git", "checkout", "-b", requested_branch], cwd=repo_path)
        return requested_branch

    if current_branch in {"main", "master", "HEAD"}:
        if not auto_create_branch:
            raise ValueError(
                "Current branch is main/master or detached HEAD. "
                "Set auto_create_branch=true or provide branch_name."
            )
        source = jira_id.strip().lower() if jira_id else "fix_feature"
        source = source.replace(" ", "_").replace("/", "_").replace("\\", "_").replace("-", "_")
        feature = (
            "".join(ch for ch in source if ch.isalnum() or ch in "._-").strip("._-")
            or "fix_feature"
        )
        generated = f"users/{_DEFAULT_LDAP}/{feature}"
        candidate = generated
        counter = 2
        while (
            _run_process(
                ["git", "show-ref", "--verify", f"refs/heads/{candidate}"],
                cwd=repo_path,
                check=False,
            ).returncode
            == 0
        ):
            candidate = f"{generated}_{counter}"
            counter += 1

        _run_process(["git", "checkout", "-b", candidate], cwd=repo_path)
        return candidate

    _validate_branch_name(current_branch)
    return current_branch


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def handle_create_shared_patch(
    arguments: dict, *, CreateSharedPatchParams
) -> list[TextContent]:
    args = CreateSharedPatchParams(**arguments)
    target_repo_path = os.path.abspath(args.repo_path or os.getcwd())

    repo_check = _run_process(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=target_repo_path,
        check=False,
    )
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message=f"Not a git repository: {target_repo_path}")
        )

    non_ignored_paths, ignored_paths = _collect_changed_paths(target_repo_path)
    shared_paths = _filter_shared_paths(
        non_ignored_paths + ignored_paths, args.shared_component_path_hints
    )
    md_path = _write_shared_patch_md(
        target_repo_path,
        args.jira_id,
        args.issue_description,
        args.changes_description,
        args.follow_ups,
        shared_paths,
        args.patch_output_path,
    )

    return [
        TextContent(
            type="text",
            text=(
                "Created shared patch markdown.\n"
                f"- Repo: {target_repo_path}\n"
                f"- Output: {md_path}\n"
                f"- Shared paths detected: {len(shared_paths)}"
            ),
        )
    ]


async def handle_create_repo_pr(arguments: dict, *, CreateRepoPrParams) -> list[TextContent]:
    args = CreateRepoPrParams(**arguments)
    repo_path = os.path.abspath(args.repo_path or os.getcwd())

    repo_check = _run_process(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path,
        check=False,
    )
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        raise McpError(ErrorData(code=INVALID_PARAMS, message=f"Not a git repository: {repo_path}"))

    try:
        non_ignored_paths, ignored_paths = _collect_changed_paths(repo_path)
        shared_hints = (
            args.shared_component_path_hints if args.handle_shared_component_changes else []
        )
        submodule_paths = _list_submodule_paths(repo_path) if args.exclude_submodule_changes else []

        shared_paths: list[str] = (
            _filter_shared_paths(non_ignored_paths + ignored_paths, shared_hints)
            if args.handle_shared_component_changes
            else []
        )

        committable_paths = _filter_committable_paths(
            non_ignored_paths, shared_hints, args.external_dependency_path_hints, submodule_paths
        )
        if args.exclude_markdown_files:
            committable_paths = [p for p in committable_paths if not p.lower().endswith(".md")]

        shared_patch_path = None
        if shared_paths:
            shared_patch_path = _write_shared_patch_md(
                repo_path,
                args.jira_id,
                args.issue_description,
                args.changes_description,
                args.follow_ups,
                shared_paths,
                args.shared_patch_output_path,
            )

        if not committable_paths:
            if shared_patch_path:
                return [
                    TextContent(
                        type="text",
                        text=(
                            "Detected shared/gitignored component changes only.\n"
                            "Created shared patch markdown; PR was not created because there are no commitable repo changes "
                            "(excluding shared, submodule, and external dependency paths).\n"
                            f"- Shared patch: {shared_patch_path}\n"
                            f"- Shared paths detected: {len(shared_paths)}"
                        ),
                    )
                ]
            if args.create_suggested_changes_md_when_no_commit:
                md_path = _write_suggested_changes_md(
                    repo_path,
                    args,
                    "No commitable repo changes found after excluding shared, submodule, and external dependency paths.",
                )
                return [
                    TextContent(
                        type="text",
                        text=f"No commitable changes found, so PR was not created.\nCreated suggested changes file: {md_path}",
                    )
                ]
            return [
                TextContent(type="text", text="No commitable repo changes found. Nothing to PR.")
            ]

        if args.stage_all:
            _run_process(["git", "add", "-A"], cwd=repo_path)
            for hint in shared_hints:
                pathspec = hint.replace("\\", "/").rstrip("/")
                if pathspec:
                    _run_process(
                        ["git", "reset", "HEAD", "--", pathspec], cwd=repo_path, check=False
                    )
            for hint in args.external_dependency_path_hints:
                pathspec = hint.replace("\\", "/").rstrip("/")
                if pathspec:
                    _run_process(
                        ["git", "reset", "HEAD", "--", pathspec], cwd=repo_path, check=False
                    )
            for sub_path in submodule_paths:
                _run_process(["git", "reset", "HEAD", "--", sub_path], cwd=repo_path, check=False)
            _run_process(
                ["git", "reset", "HEAD", "--", "mcp-server.log"], cwd=repo_path, check=False
            )

        excluded_markdown = []
        if args.exclude_markdown_files:
            excluded_markdown = _unstage_markdown_files(repo_path)

        staged_files = _run_process(
            ["git", "diff", "--cached", "--name-only"], cwd=repo_path
        ).stdout.strip()
        staged_list = [line.strip() for line in staged_files.splitlines() if line.strip()]
        staged_committable = _filter_committable_paths(
            staged_list, shared_hints, args.external_dependency_path_hints, submodule_paths
        )
        if args.exclude_markdown_files:
            staged_committable = [p for p in staged_committable if not p.lower().endswith(".md")]

        if not staged_files:
            if shared_patch_path:
                return [
                    TextContent(
                        type="text",
                        text=(
                            "Detected shared/gitignored component changes.\n"
                            "Created shared patch markdown; PR was not created because no commitable staged repo changes remain.\n"
                            f"- Shared patch: {shared_patch_path}\n"
                            f"- Shared paths detected: {len(shared_paths)}"
                        ),
                    )
                ]
            if args.create_suggested_changes_md_when_no_commit:
                md_path = _write_suggested_changes_md(
                    repo_path,
                    args,
                    "No staged changes found after filters (e.g., markdown exclusion or non-commitable paths).",
                )
                return [
                    TextContent(
                        type="text",
                        text=f"No commitable staged changes found, so PR was not created.\nCreated suggested changes file: {md_path}",
                    )
                ]
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message="No staged changes found after staging step. Nothing to commit.",
                )
            )

        if not staged_committable:
            if shared_patch_path:
                return [
                    TextContent(
                        type="text",
                        text=(
                            "Only non-committable paths are staged (shared/submodule/external).\n"
                            "Created shared patch markdown; PR was not created.\n"
                            f"- Shared patch: {shared_patch_path}\n"
                            f"- Shared paths detected: {len(shared_paths)}"
                        ),
                    )
                ]
            if args.create_suggested_changes_md_when_no_commit:
                md_path = _write_suggested_changes_md(
                    repo_path,
                    args,
                    "Only shared/submodule/external dependency paths are staged. PR creation is skipped.",
                )
                return [
                    TextContent(
                        type="text",
                        text=f"Only non-committable staged paths were found, so PR was not created.\nCreated suggested changes file: {md_path}",
                    )
                ]
            raise McpError(
                ErrorData(
                    code=INVALID_PARAMS,
                    message="Only non-committable paths are staged. Nothing to PR.",
                )
            )

        branch_name = _ensure_branch_for_pr(
            repo_path, args.branch_name, args.auto_create_branch, args.jira_id
        )

        _run_process(["git", "commit", "-m", args.commit_message], cwd=repo_path)
        _run_process(["git", "push", "-u", "origin", branch_name], cwd=repo_path)

        pr_cmd = [
            "gh",
            "pr",
            "create",
            "--base",
            args.base_branch,
            "--head",
            branch_name,
            "--title",
            args.pr_title,
            "--body",
            _resolve_pr_body(repo_path, args),
        ]
        if args.reviewer:
            pr_cmd.extend(["--reviewer", args.reviewer])

        pr_result = _run_process(pr_cmd, cwd=repo_path)
        pr_url = pr_result.stdout.strip()

        summary = (
            f"Created PR successfully.\n"
            f"- Repo: {repo_path}\n"
            f"- Branch: {branch_name}\n"
            f"- Base: {args.base_branch}\n"
            f"- PR URL: {pr_url}\n"
            f"- Staged files:\n{staged_files}"
        )
        if shared_patch_path:
            summary += f"\n- Shared patch also created: {shared_patch_path}\n- Shared paths detected: {len(shared_paths)}"
        if excluded_markdown:
            summary += "\n- Excluded markdown files:\n" + "\n".join(excluded_markdown)
        return [TextContent(type="text", text=summary)]
    except subprocess.CalledProcessError as exc:
        raise McpError(ErrorData(code=INTERNAL_ERROR, message=_format_process_failure(exc)))
