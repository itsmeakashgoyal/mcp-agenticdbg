"""Tests for git/PR tool helpers."""

import pytest

from triagepilot.server import CreateRepoPrParams
from triagepilot.tools.git_tools import (
    _filter_committable_paths,
    _filter_shared_paths,
    _is_path_in_prefixes,
    _normalize_rel_path,
    _parse_porcelain_path,
    _resolve_pr_body,
    _validate_branch_name,
)

# ---------------------------------------------------------------------------
# _validate_branch_name
# ---------------------------------------------------------------------------


class TestValidateBranchName:
    def test_valid(self):
        _validate_branch_name("users/agent/fix_feature")

    def test_valid_with_dots(self):
        _validate_branch_name("users/agent/fix.feature-1")

    def test_missing_prefix(self):
        with pytest.raises(ValueError, match="Invalid branch name"):
            _validate_branch_name("feature/xyz")

    def test_wrong_ldap(self):
        with pytest.raises(ValueError, match="Invalid branch name"):
            _validate_branch_name("users/john/fix")

    def test_empty_feature(self):
        with pytest.raises(ValueError, match="Invalid branch name"):
            _validate_branch_name("users/agent/")

    def test_invalid_chars(self):
        with pytest.raises(ValueError, match="Feature segment"):
            _validate_branch_name("users/agent/fix feature")


# ---------------------------------------------------------------------------
# _normalize_rel_path
# ---------------------------------------------------------------------------


class TestNormalizeRelPath:
    def test_backslash(self):
        assert _normalize_rel_path("src\\main.cpp") == "src/main.cpp"

    def test_leading_dot_slash(self):
        assert _normalize_rel_path("./src/main.cpp") == "src/main.cpp"

    def test_plain(self):
        assert _normalize_rel_path("file.txt") == "file.txt"


# ---------------------------------------------------------------------------
# _is_path_in_prefixes
# ---------------------------------------------------------------------------


class TestIsPathInPrefixes:
    def test_exact_match(self):
        assert _is_path_in_prefixes("vendor", ["vendor"]) is True

    def test_nested(self):
        assert _is_path_in_prefixes("vendor/lib/x.cpp", ["vendor"]) is True

    def test_no_match(self):
        assert _is_path_in_prefixes("src/main.cpp", ["vendor"]) is False

    def test_empty_prefixes(self):
        assert _is_path_in_prefixes("anything", []) is False


# ---------------------------------------------------------------------------
# _filter_shared_paths
# ---------------------------------------------------------------------------


class TestFilterSharedPaths:
    def test_matches(self):
        paths = ["vendor/libs/core/a.cpp", "src/b.cpp"]
        assert _filter_shared_paths(paths, ["vendor/libs/core/"]) == ["vendor/libs/core/a.cpp"]

    def test_no_hints(self):
        paths = ["a.cpp", "b.cpp"]
        assert _filter_shared_paths(paths, []) == paths


# ---------------------------------------------------------------------------
# _filter_committable_paths
# ---------------------------------------------------------------------------


class TestFilterCommittablePaths:
    def test_excludes_shared_and_submodule(self):
        paths = ["src/main.cpp", "vendor/libs/core/lib.cpp", "external/foo.cpp", "submod"]
        result = _filter_committable_paths(
            paths,
            shared_hints=["vendor/libs/core/"],
            external_hints=["external/"],
            submodule_paths=["submod"],
        )
        assert result == ["src/main.cpp"]


# ---------------------------------------------------------------------------
# _parse_porcelain_path
# ---------------------------------------------------------------------------


class TestParsePorcelainPath:
    def test_modified(self):
        assert _parse_porcelain_path(" M src/main.cpp") == "src/main.cpp"

    def test_rename(self):
        assert _parse_porcelain_path("R  old.txt -> new.txt") == "new.txt"

    def test_short_line(self):
        assert _parse_porcelain_path("M") is None

    def test_ignored(self):
        assert _parse_porcelain_path("!! ignored/file.o") == "ignored/file.o"


# ---------------------------------------------------------------------------
# _resolve_pr_body
# ---------------------------------------------------------------------------


class TestResolvePrBody:
    def test_populates_sections(self, tmp_path):
        """Smoke test: ensure template gets populated when available."""
        gh_dir = tmp_path / ".github"
        gh_dir.mkdir()
        tpl = gh_dir / "pull_request_template.md"
        tpl.write_text(
            "- **JIRA LINK**\n"
            "- **PUBLIC RELEASE NOTE**\n"
            "- **TEST IMPACT**\n"
            "- **DEV DESCRIPTION (Mandatory)**\n"
            "  - **Issue**\n"
            "    _Briefly describe the problem or requirement._\n"
            "  - **What are the changes to fix this issue?**\n"
            "    _Summarize the key changes made to address the issue._\n"
            "  - **Follow-ups:**\n"
            "    _List any pending scenarios and related JIRA tickets._\n"
        )
        params = CreateRepoPrParams(
            commit_message="fix",
            pr_title="Fix issue",
            jira_id="JIRA-1",
            issue_description="root cause",
            changes_description="patched",
            follow_ups="none",
            repo_path=str(tmp_path),
        )
        body = _resolve_pr_body(str(tmp_path), params)
        assert "JIRA-1" in body
        assert "root cause" in body
        assert "patched" in body
