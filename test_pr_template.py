"""Test PR template formatting to ensure it follows the structure."""
import sys
import os

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from triagepilot.tools.git_tools import _resolve_pr_body
from triagepilot.server import CreateRepoPrParams

def test_pr_template_formatting():
    """Test that PR body follows the exact template structure."""
    
    # Create test parameters matching your crash analysis
    params = CreateRepoPrParams(
        commit_message="Fix null pointer dereference in PDNameTree operations",
        pr_title="Fix null pointer dereference in tree node operations [APP-12345]",
        jira_id="APP-12345",
        release_note="Fixed crash in PDF export when document handles were invalid",
        test_impact="""Test PDF save operations with job options enabled
Test with corrupted/malformed PDF files
Test PDF/X compliance workflows
Verify normal PDF operations still work
Monitor logs for new warning messages""",
        issue_description="""**Root Cause Analysis**

The crash occurred in MyAppCore.dll at ProcessTreeNode+0x9e5 due to a null pointer dereference during atomic reference counting operations.

**Crash Details**
- Exception: c0000005 (Access Violation - Write)
- Faulting Address: 0x0000000000000009 (near-null pointer)
- Instruction: lock xadd dword ptr [rcx+8], eax
- Module: MyAppCore.dll
- Stack Trace: ProcessTreeNode -> Document::EmbedOptions

**Technical Details**
The issue was in CAIPDFDocument::EmbedJobOptions() where:
1. PDDocCreateNameTree() returned an invalid handle
2. Missing null checks before PDNameTreeIsValid() and PDNameTreePut()
3. Invalid name tree caused atomic ref-count operations on corrupted memory""",
        changes_description="""- Added PDDoc/CosDoc validation before PDDocCreateNameTree
- Added null check for PDNameTree handle before PDNameTreeIsValid
- Added defensive validation before PDNameTreePut call
- Added warning logs for debugging invalid states
- Restructured code to use if-else pattern instead of goto for better control flow""",
        follow_ups="""Monitor logs for warning messages in production
Consider adding similar checks in other PDNameTree operations
Related ticket: APP-12346 for comprehensive error handling""",
        repo_path=os.path.dirname(__file__),
        base_branch="main",
    )
    
    # Generate PR body
    pr_body = _resolve_pr_body(os.path.dirname(__file__), params)
    
    print("=" * 80)
    print("GENERATED PR BODY:")
    print("=" * 80)
    print(pr_body)
    print("=" * 80)
    
    # Verify structure
    assert "- **JIRA LINK**" in pr_body
    assert "APP-12345" in pr_body
    assert "- **PUBLIC RELEASE NOTE**" in pr_body
    assert "Fixed crash in PDF export" in pr_body
    assert "- **TEST IMPACT**" in pr_body
    assert "Test PDF save operations" in pr_body
    assert "- **DEV DESCRIPTION (Mandatory)**" in pr_body
    assert "  - **Issue**" in pr_body
    assert "Root Cause Analysis" in pr_body
    assert "  - **What are the changes to fix this issue?**" in pr_body
    assert "Added PDDoc/CosDoc validation" in pr_body
    assert "  - **Follow-ups:**" in pr_body
    assert "Monitor logs for warning messages" in pr_body
    
    # Verify placeholders are replaced
    assert "_Briefly describe the problem or requirement._" not in pr_body
    assert "_Summarize the key changes made to address the issue._" not in pr_body
    assert "_List any pending scenarios and related JIRA tickets._" not in pr_body
    
    print("\n[SUCCESS] All assertions passed! PR template follows the correct structure.")

if __name__ == "__main__":
    test_pr_template_formatting()
