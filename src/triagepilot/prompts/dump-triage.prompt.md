Perform comprehensive analysis of a crash dump with detailed metadata extraction and structured reporting. Supports Windows (.dmp), Linux (core dumps), and macOS (.crash / core dumps).

## QUICK USAGE (SHORT PROMPT)

Use this prompt with arguments instead of a long free-form message:
- `dump_path`
- `symbols_path` (optional)
- `image_path` (optional)
- `repo_path` (optional, needed for source lookup and code fixes)
- `jira_id` (optional, reused for PR/patch tools)

When these arguments are provided, do not ask for them again unless missing or invalid.

## WORKFLOW

### Step 1: Dump File Identification
**If no dump file path provided:**
- Ask user to provide the crash dump file path
- Use `list_dumps` to help find available dumps
- Ask for optional `symbols_path` and `image_path` when analyzing private binaries

### Step 2: Comprehensive Dump Analysis
**Analyze the dump file:**

**Tool:** `analyze_dump` (preferred) or `open_dump`
- **Parameters:**
  - `dump_path`: The dump file path
  - `symbols_path` (optional): Per-analysis symbols/debug info path (folder or file path)
  - `image_path` (optional): Per-analysis image path (folder, or executable file path)
  - `repo_path` (optional): Local repo path for faulting source lookup and code fix application
  - `include_stack_trace`: true
  - `include_modules`: true
  - `include_threads`: true

The tool automatically runs the appropriate analysis command for the platform:
- **Windows (CDB):** `!analyze -v`
- **Linux (GDB):** `bt full`
- **macOS (LLDB):** `bt all`

**Source lookup is automatic and multi-level.** The tool uses a fallback chain:
1. **Debug info source** — exact file + line when debug symbols have source info.
2. **Function name search** — greps the repo for the faulting function definition
   using `SYMBOL_NAME` (e.g. `MyAppCore!ProcessTreeNode`). Works with
   stripped/public-only symbols that lack source file info.
3. **Stack trace search** — walks every `module!Function+0xOffset` frame from
   the call stack and searches for definitions in the repo.

All three levels scan the **entire repo tree including gitignored directories**
(e.g. `vendor/`, `third_party/`), so shared-component source is always discoverable.

**Extract additional metadata with:** `run_debugger_cmd`

Platform-specific useful commands:
- **Windows (CDB):** `vertarget`, `lm`, `k`, `.time`, `!peb`, `r`
- **Linux (GDB):** `info proc`, `info sharedlibrary`, `bt`, `info threads`, `info registers`
- **macOS (LLDB):** `process status`, `image list`, `bt`, `thread list`, `register read`

**Cleanup:** `close_dump`

### Step 2b: Source Code Investigation (When Debug Symbols Are Incomplete)

When the analysis does NOT produce `FAULTING_SOURCE_FILE` / `FAULTING_SOURCE_LINE_NUMBER`
(common with release builds or shared components), the tool still locates the faulting
function via the fallback search described above.

**You MUST still attempt to fix the code** even when:
- The crash is in a shared component (e.g. `vendor/`, `third_party/`)
- No `symbols_path` or `image_path` was provided
- Only public symbols are available

Use the function definition shown in the analysis output to understand the bug
and apply the fix directly to whichever file contains the function—whether it is
a repo-tracked file or a shared/gitignored file. The PR/patch tool selection gate
(below) handles routing the changes correctly.

### Step 3: Generate Analysis Report

Always return the result in this exact numbered structure:
1) exception code + faulting module/function
2) full symbolized stack trace
3) likely root cause
4) concrete fixes in current repo and apply them (shared or repo-tracked, either is fine)
5) verification steps
6) create a PR/Patch once changes are finalized (use `jira_id` if provided)

## OUTPUT FORMAT

```markdown
# Crash Dump Analysis Report
**Analysis Date:** [Date]
**Dump File:** [filename]
**File Path:** [Full path]

## Executive Summary
- **Crash Type:** [Exception type]
- **Severity:** [Critical/High/Medium/Low]
- **Root Cause:** [Brief description]
- **Recommended Action:** [Next steps]

## Dump Metadata
- **Creation Time:** [Timestamp]
- **OS / Platform:** [OS version and architecture]
- **Process Name:** [Process name and PID]

## Crash Analysis
**Exception Details:**
- **Exception Code / Signal:** [0xC0000005, SIGSEGV, etc.]
- **Exception Address:** [Address]
- **Faulting Module:** [module name]

**Call Stack:**
[Stack frames with module!function+offset or file:line]

**Thread Information:**
- **Crashing Thread ID:** [ID]
- **Thread Count:** [Total]

## Root Cause Analysis
- **What happened:** [Technical description]
- **Why it happened:** [Contributing factors]
- **Code location:** [Function/line if known]

## Recommendations
### Immediate Actions
1. [Action item]

### Investigation Steps
1. [Follow-up steps]

### Prevention
1. [Code changes needed]
```

## ANALYSIS DEPTH
Provide technical details for developers to:
- Understand the failure mechanism
- Identify root cause in source code
- Implement appropriate fixes
- Prevent similar issues

## OPTIONAL POST-ANALYSIS DEVELOPMENT FLOW
If the user explicitly asks for code changes after the dump analysis:
1. Implement the requested code changes.
2. Validate changes (run tests/build checks where possible).
3. **Only if the user explicitly asks to raise a PR**, follow the MANDATORY TOOL SELECTION GATE below.

**Do not create commits or PRs automatically. PR creation must be user-requested explicitly.**

## MANDATORY TOOL SELECTION GATE — FOLLOW EXACTLY

**BEFORE calling any PR or patch tool you MUST classify every changed file path into one of two buckets:**

| Bucket | Path pattern | Example |
|--------|-------------|---------|
| **Shared / gitignored** | Any submodule, vendor, or external dependency path | `vendor/libs/core/src/Engine.cpp` |
| **Repo-tracked** | Everything else (files that `git status` shows as modified/added) | `src/myapp/core/Application.cpp` |

### Decision tree (execute top-to-bottom, STOP at the first matching case):

**CASE A — ALL changed files are shared / gitignored (no repo-tracked changes):**
1. Call **`create_shared_patch`**.
2. **STOP. DO NOT call `create_repo_pr`. DO NOT run any git commit/push. Execution ends here.**

**CASE B — ALL changed files are repo-tracked (no shared component changes):**
1. Call **`create_repo_pr`**.

**CASE C — MIXED (both shared AND repo-tracked files changed):**
1. Call **`create_shared_patch`** for the shared/gitignored paths.
2. Call **`create_repo_pr`** for the repo-tracked paths only.

### How to verify which case applies
Run `git status` and check if ANY changed file appears as modified, added, or untracked (but **not** ignored).
- If `git status` shows **no** modified/added/untracked files → you are in **CASE A**.
- If `git status` shows modified/added/untracked files AND they are all under repo-tracked paths → **CASE B**.
- If both → **CASE C**.

**CRITICAL REMINDERS:**
- Shared components live in gitignored directories. They will NEVER appear as tracked changes in `git status`.
- If the only code changes you made are to files under shared/gitignored paths, there is NOTHING to commit or PR. Call `create_shared_patch` and STOP.
- NEVER call `create_repo_pr` hoping it will "figure it out" — it cannot commit gitignored files.
- NEVER create a PR whose only content is a generated markdown patch file.
- DO NOT create crash-analysis or documentation markdown files in the repo and then treat them as committable changes. All analysis/patch documentation must go through `create_shared_patch` parameters (`issue_description`, `changes_description`, `follow_ups`), not as separate files written to the repo.

## Tool Parameters

### For `create_shared_patch` (shared component changes):
```json
{
  "repo_path": "<repo_root>",
  "jira_id": "<TICKET_ID>",
  "issue_description": "<root cause analysis>",
  "changes_description": "<suggested code changes>",
  "follow_ups": "<follow-up notes>"
}
```

### For `create_repo_pr` (repo changes):
- If `jira_id` was provided as prompt input/context, pass it through directly.
- Put all analysis in:
  - `issue_description`
  - `changes_description`
  - `follow_ups`
- Keep top-level template sections as-is unless user explicitly asks to fill them:
  - `PUBLIC RELEASE NOTE`
  - `TEST IMPACT`
- Always map user-provided ticket ID to `jira_id`

Branch naming for PR creation must be strict:
- `users/agent/<fix_feature>`
