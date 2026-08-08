"""Tests for repo_path/symbols_path root confinement and symlink handling."""

import os

import pytest
from mcp.shared.exceptions import McpError

from triagepilot.tools.debugger_tools import (
    _find_file_in_repo,
    _find_function_in_repo,
    _resolve_scoped_path,
)


class TestResolveScopedPath:
    def test_no_base_configured_passes_through(self):
        assert _resolve_scoped_path("/anywhere", None, "repo_path") == "/anywhere"

    def test_no_candidate_falls_back_to_base(self, tmp_path):
        base = str(tmp_path)
        assert _resolve_scoped_path(None, base, "repo_path") == base

    def test_candidate_equal_to_base_is_allowed(self, tmp_path):
        base = str(tmp_path)
        assert _resolve_scoped_path(base, base, "repo_path") == base

    def test_candidate_inside_base_is_allowed(self, tmp_path):
        base = tmp_path
        sub = base / "module"
        sub.mkdir()
        assert _resolve_scoped_path(str(sub), str(base), "repo_path") == str(sub)

    def test_candidate_outside_base_is_rejected(self, tmp_path):
        base = tmp_path / "repo"
        base.mkdir()
        outside = tmp_path / "other"
        outside.mkdir()
        with pytest.raises(McpError):
            _resolve_scoped_path(str(outside), str(base), "repo_path")

    def test_sibling_directory_with_shared_prefix_is_rejected(self, tmp_path):
        # "repo-evil" starts with "repo" as a string but is not a descendant
        # of it -- a naive startswith(base) check would wrongly allow this.
        base = tmp_path / "repo"
        base.mkdir()
        sibling = tmp_path / "repo-evil"
        sibling.mkdir()
        with pytest.raises(McpError):
            _resolve_scoped_path(str(sibling), str(base), "repo_path")

    def test_traversal_out_and_back_is_rejected(self, tmp_path):
        base = tmp_path / "repo"
        base.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        traversal = str(base / ".." / "secret")
        with pytest.raises(McpError):
            _resolve_scoped_path(traversal, str(base), "repo_path")


class TestSymlinkEscapePrevention:
    def test_find_file_in_repo_skips_symlinked_file(self, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret = outside_dir / "secret.cpp"
        secret.write_text("int leaked() { return 1; }\n")

        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "secret.cpp"
        os.symlink(secret, link)

        matches = _find_file_in_repo("secret.cpp", str(repo))
        assert matches == []

    def test_find_function_in_repo_skips_symlinked_file(self, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret = outside_dir / "secret.cpp"
        secret.write_text("int leaked_function() { return 1; }\n")

        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "secret.cpp"
        os.symlink(secret, link)

        matches = _find_function_in_repo("leaked_function", str(repo))
        assert matches == []

    def test_find_file_in_repo_still_matches_real_files(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.cpp").write_text("int main() { return 0; }\n")

        matches = _find_file_in_repo("main.cpp", str(repo))
        assert len(matches) == 1
