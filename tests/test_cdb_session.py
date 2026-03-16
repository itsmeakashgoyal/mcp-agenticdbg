"""Tests for CDBSession initialisation, validation, and helper methods."""

import os

import pytest

from triagepilot.backends.cdb import CDBSession

# ---------------------------------------------------------------------------
# find_debugger_executable
# ---------------------------------------------------------------------------


class TestFindCdbExecutable:
    def test_custom_path_valid(self, tmp_path):
        dummy = tmp_path / "cdb.exe"
        dummy.write_text("fake")
        assert CDBSession.find_debugger_executable(str(dummy)) == str(dummy)

    def test_custom_path_missing(self):
        result = CDBSession.find_debugger_executable("/nonexistent/cdb.exe")
        assert result is None or os.path.isfile(result)


# ---------------------------------------------------------------------------
# _normalize_symbols_path
# ---------------------------------------------------------------------------


class TestNormalizeSymbolsPath:
    def _call(self, value):
        inst = CDBSession.__new__(CDBSession)
        return inst._normalize_symbols_path(value)

    def test_none(self):
        assert self._call(None) is None

    def test_directory_unchanged(self, tmp_path):
        assert self._call(str(tmp_path)) == str(tmp_path)

    def test_pdb_converted_to_dir(self, tmp_path):
        pdb = tmp_path / "foo.pdb"
        pdb.write_text("fake")
        assert self._call(str(pdb)) == str(tmp_path)

    def test_semicolon_list(self, tmp_path):
        pdb = tmp_path / "bar.pdb"
        pdb.write_text("fake")
        result = self._call(f"C:\\symbols;{pdb}")
        parts = result.split(";")
        assert parts[0] == "C:\\symbols"
        assert parts[1] == str(tmp_path)


# ---------------------------------------------------------------------------
# _normalize_image_path
# ---------------------------------------------------------------------------


class TestNormalizeImagePath:
    def _call(self, value):
        inst = CDBSession.__new__(CDBSession)
        return inst._normalize_image_path(value)

    def test_none(self):
        assert self._call(None) is None

    def test_exe_converted_to_dir(self, tmp_path):
        exe = tmp_path / "app.exe"
        exe.write_text("fake")
        assert self._call(str(exe)) == str(tmp_path)

    def test_dll_converted_to_dir(self, tmp_path):
        dll = tmp_path / "lib.dll"
        dll.write_text("fake")
        assert self._call(str(dll)) == str(tmp_path)


# ---------------------------------------------------------------------------
# _is_slow_command
# ---------------------------------------------------------------------------


class TestIsSlowCommand:
    def _call(self, cmd):
        inst = CDBSession.__new__(CDBSession)
        return inst._is_slow_command(cmd)

    def test_analyze_slow(self):
        assert self._call("!analyze -v") is True

    def test_reload_slow(self):
        assert self._call(".reload /f") is True

    def test_regular_not_slow(self):
        assert self._call("kb") is False

    def test_case_insensitive(self):
        assert self._call("!ANALYZE -v") is True


# ---------------------------------------------------------------------------
# __init__ validation
# ---------------------------------------------------------------------------


class TestInitValidation:
    def test_no_target_raises(self):
        with pytest.raises((ValueError, TypeError)):
            CDBSession()

    def test_missing_dump_raises(self):
        with pytest.raises(FileNotFoundError, match="Dump file not found"):
            CDBSession(dump_path="/nonexistent/test.dmp")
