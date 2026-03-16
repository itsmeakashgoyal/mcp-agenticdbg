Perform comprehensive analysis of a crash dump with deep inspection, structured reporting, and actionable fix guidance. Supports Windows (.dmp via CDB), Linux (core dumps via GDB), and macOS (.crash / core dumps via LLDB).

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

### Step 2: Initial Crash Analysis
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

The tool automatically runs a rich multi-section analysis for the detected platform:
- **Windows (CDB):** `.lastevent`, `!analyze -v`, `kb`, `~*kb` (all-thread backtraces), `r` (registers), `lm`, `vertarget`
- **Linux (GDB):** `bt full`, `info threads`, `info registers`, `info sharedlibrary`, `info proc`
- **macOS (LLDB):** `process status`, `frame info`, `bt`, `bt all`, `register read`, `image list`

**Source lookup is automatic and multi-level.** The tool uses a fallback chain:
1. **Debug info source** (GDB/LLDB `at file:line`, CDB `FAULTING_SOURCE_FILE`) — exact file + line when debug symbols have source info.
2. **Symbol name search** (CDB `SYMBOL_NAME: module!Function`) — greps the repo for the faulting function definition. Works with stripped/public-only symbols.
3. **Stack trace search** — walks every frame (`module!Function+0xOffset` on CDB, `#N in func` on GDB) and searches for definitions in the repo.

All levels scan the **entire repo tree including gitignored directories** (e.g. `vendor/`, `third_party/`), so shared-component source is always discoverable.

### Step 3: Deep Inspection

After the initial analysis, use `run_debugger_cmd` to gather deeper context based on the crash type. **Always gather at minimum: registers, all-thread backtraces, and disassembly around the crash point.** Then add crash-type-specific commands from the table below.

#### Platform Command Reference

| Inspection | CDB (Windows) | GDB (Linux) | LLDB (macOS) |
|---|---|---|---|
| **All-thread backtraces** | `~*kb` | `thread apply all bt` | `bt all` |
| **Exception context** | `.ecxr` | *(automatic)* | `thread info` |
| **Registers** | `r` | `info registers` | `register read` |
| **Disassemble crash point** | `u @rip L20` or `uf <function>` | `disassemble` | `disassemble --pc --count 30` |
| **Local variables** | `.frame 0` then `dv /t` | `info locals` | `frame variable` |
| **Evaluate expression** | `?? <expr>` or `? <expr>` | `print <expr>` | `expression <expr>` |
| **Memory dump** | `db <addr> L<len>` | `x/<N>xb <addr>` | `memory read --count <N> --format x <addr>` |
| **Memory map / regions** | `!address` | `info proc mappings` | `process status --verbose` |
| **Module list** | `lm` | `info sharedlibrary` | `image list` |
| **Target / OS info** | `vertarget` | `info proc` | `target list` |
| **Process environment** | `!peb` | `show environment` | *(N/A)* |
| **Heap state** | `!heap -s` | `info heap` (if available) | *(N/A)* |
| **Lock / critical section** | `!locks` | `info threads` (check for mutex) | `thread list` |

#### Crash-Type Deep Dive Playbook

Use the following additional commands based on the crash type identified in Step 2:

**Access Violation / Segfault / SIGBUS:**
1. Examine the faulting address — is it NULL, small (null-deref + offset), wild, or use-after-free?
2. Dump memory around the faulting address: `db <addr>` / `x/32xb <addr>` / `memory read <addr>`
3. Check memory region permissions: `!address <addr>` / `info proc mappings`
4. Walk the stack frames inspecting locals to find when the bad pointer was introduced

**Heap Corruption / Double-Free:**
1. Heap summary: `!heap -s` (CDB) / application-specific heap commands
2. Heap validation: `!heap -a <heap_handle>` (CDB)
3. Check if the crash is in an allocator function (malloc, free, RtlAllocateHeap, etc.)
4. Examine surrounding heap metadata: dump memory before/after the corrupt block
5. Check all threads for concurrent heap operations

**Stack Overflow:**
1. Examine stack depth and look for deep recursion patterns
2. Check thread stack limits: `!teb` (CDB) / `info proc mappings` (GDB)
3. Look for unbounded recursive calls in the backtrace
4. Measure frame size if large local arrays are involved

**Deadlock / Hang (if applicable to dump):**
1. Show all thread backtraces — look for threads blocked on synchronization
2. Check lock ownership: `!locks` (CDB) / look for mutex/futex in stacks
3. Identify the cycle: Thread A holds Lock X and waits on Lock Y, Thread B holds Lock Y and waits on Lock X
4. Check for thread pool exhaustion

**Use-After-Free / Dangling Pointer:**
1. Dump memory at the faulting address — look for fill patterns (0xFEEEFEEE on Windows, 0xDEADBEEF, etc.)
2. Check if the address falls within freed heap blocks
3. Walk up the stack to find where the dangling pointer came from
4. Look at the object's vtable pointer (first 8 bytes) for corruption signatures

**Assertion / Abort / SIGABRT:**
1. Look at the abort message in the stack or output
2. The crashing frame is typically the assertion macro — walk up to find the actual failure condition
3. Examine the variables involved in the assertion condition

**Unhandled C++ Exception / SIGABRT from throw:**
1. Check for `__cxa_throw` / `CxxThrowException` in the stack
2. Walk up past the exception handling frames to find the throw site
3. Examine the exception object if possible

### Step 3b: Source Code Investigation (When Debug Symbols Are Incomplete)

When the analysis does NOT produce `FAULTING_SOURCE_FILE` / `FAULTING_SOURCE_LINE_NUMBER`
(common with release builds or shared components), the tool still locates the faulting
function via the fallback search described above.

**You MUST still attempt to fix the code** even when:
- The crash is in a shared component (e.g. `vendor/`, `third_party/`)
- No `symbols_path` or `image_path` was provided
- Only public symbols are available

Use the function definition shown in the analysis output to understand the bug
and apply the fix directly to whichever file contains the function — whether it is
a repo-tracked file or a shared/gitignored file. The PR/patch tool selection gate
(below) handles routing the changes correctly.

### Step 4: Generate Analysis Report

Always return the result in this exact numbered structure:
1) Exception code/signal + faulting module/function
2) Full symbolized stack trace (crashing thread + any relevant secondary threads)
3) Register state at crash point
4) Memory context (faulting address region, relevant data)
5) Root cause analysis with evidence chain
6) Concrete fixes in current repo — apply them (shared or repo-tracked, either is fine)
7) Verification steps
8) Create a PR/Patch once changes are finalized (use `jira_id` if provided)

## OUTPUT FORMAT

```markdown
# Crash Dump Analysis Report
**Analysis Date:** [Date]
**Dump File:** [filename]
**File Path:** [Full path]
**Platform:** [Windows/Linux/macOS] — [Debugger: CDB/GDB/LLDB]

## Executive Summary
- **Crash Type:** [Exception type / signal name]
- **Severity:** [Critical/High/Medium/Low]
- **Root Cause:** [Brief description]
- **Confidence:** [High/Medium/Low — based on symbol quality and evidence]
- **Recommended Action:** [Next steps]

## Dump Metadata
- **Creation Time:** [Timestamp]
- **OS / Platform:** [OS version and architecture]
- **Process Name:** [Process name and PID]
- **Debugger:** [CDB/GDB/LLDB with version if available]

## Crash Details

### Exception / Signal
- **Exception Code / Signal:** [0xC0000005 / SIGSEGV / EXC_BAD_ACCESS / etc.]
- **Exception Description:** [Access Violation reading 0x0 / Segmentation fault / etc.]
- **Faulting Address:** [Address being accessed]
- **Instruction Address:** [Address of faulting instruction]
- **Faulting Module:** [module name + version if available]
- **Faulting Function:** [fully qualified function name + offset]

### Register State
[Key registers at crash point, especially instruction pointer, stack pointer, and any registers involved in the faulting instruction]

### Faulting Disassembly
[5-10 instructions around the crash point, with the faulting instruction highlighted]

### Crashing Thread Call Stack
[Full stack frames with module!function+offset and file:line where available]

### All Thread Backtraces
[Summary of all threads — highlight any that are relevant (blocked, holding locks, performing related operations)]

### Loaded Modules
[Key modules — especially the faulting module and any without symbols]

## Memory Analysis
- **Faulting Address Region:** [What memory region does the faulting address fall in — heap, stack, unmapped, freed, etc.]
- **Data at Faulting Address:** [Hex dump if readable, or "unmapped/inaccessible"]
- **Relevant Pointer Chain:** [If the crash involves a chain of dereferences, show the chain]

## Root Cause Analysis
- **What happened:** [Technical description of the immediate failure]
- **Why it happened:** [Contributing factors — logic error, race condition, missing null check, etc.]
- **Evidence chain:** [Connect the register state, memory contents, and stack trace to the root cause]
- **Code location:** [Function/file:line if known, or best-match function from repo search]
- **Crash classification:** [NULL dereference / use-after-free / buffer overflow / stack overflow / race condition / assertion failure / unhandled exception / etc.]

## Faulting Source Code
[Source code snippet around the faulting location, with the faulting line highlighted]

## Fix
### Root Cause Fix
[The actual code change — applied directly to the file if repo_path was provided]

### Defensive Improvements (if applicable)
[Additional hardening — bounds checks, null guards, assertions — only if directly relevant]

## Recommendations
### Immediate Actions
1. [Action item]

### Verification Steps
1. [How to verify the fix resolves the crash]
2. [How to reproduce the crash for testing]

### Prevention
1. [Code changes, static analysis rules, or testing strategies to prevent recurrence]

### Related Risk Areas
[Other code paths that may have the same pattern — similar functions, callers of the faulting function, etc.]
```

## ANALYSIS DEPTH
Provide technical details for developers to:
- Understand the failure mechanism at the instruction level
- Trace the corrupted state back to its origin
- Identify the root cause in source code with confidence
- Implement a correct, minimal fix
- Verify the fix addresses the root cause (not just the symptom)
- Identify related code that may have the same vulnerability

## POST-ANALYSIS DEVELOPMENT FLOW

**Code fixes are part of the required report (step 6).** When `repo_path` is provided and
the faulting source is located, you MUST write the concrete fix directly in the source file —
do not just describe it. Apply the minimal, correct change to address the root cause.

After applying the fix:
1. Validate changes (run tests/build checks where possible).
2. **Only if the user explicitly asks to raise a PR**, follow the MANDATORY TOOL SELECTION GATE below.

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
