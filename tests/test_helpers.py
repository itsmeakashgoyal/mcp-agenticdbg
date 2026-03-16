"""Tests for faulting source helpers (parse, find, match, read, locate)."""

import os

from triagepilot.tools.debugger_tools import (
    _best_match,
    _extract_stack_functions,
    _find_file_in_repo,
    _find_function_in_repo,
    _parse_faulting_module_function,
    _parse_faulting_source,
    _read_source_context,
    locate_faulting_source,
)

# ---------------------------------------------------------------------------
# _parse_faulting_source
# ---------------------------------------------------------------------------


class TestParseFaultingSource:
    def test_both_present(self):
        text = "FAULTING_SOURCE_FILE: C:\\src\\main.cpp\nFAULTING_SOURCE_LINE_NUMBER: 42\n"
        file, line = _parse_faulting_source(text)
        assert file == "C:\\src\\main.cpp"
        assert line == 42

    def test_file_only(self):
        text = "FAULTING_SOURCE_FILE: foo.cpp\n"
        file, line = _parse_faulting_source(text)
        assert file == "foo.cpp"
        assert line is None

    def test_neither(self):
        file, line = _parse_faulting_source("no relevant data")
        assert file is None
        assert line is None


# ---------------------------------------------------------------------------
# _find_file_in_repo
# ---------------------------------------------------------------------------


class TestFindFileInRepo:
    def test_finds_file(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        target = sub / "Widget.cpp"
        target.write_text("code")
        matches = _find_file_in_repo("Widget.cpp", str(tmp_path))
        assert len(matches) == 1
        assert os.path.basename(matches[0]) == "Widget.cpp"

    def test_case_insensitive(self, tmp_path):
        f = tmp_path / "Main.CPP"
        f.write_text("code")
        matches = _find_file_in_repo("main.cpp", str(tmp_path))
        assert len(matches) == 1

    def test_no_match(self, tmp_path):
        assert _find_file_in_repo("missing.h", str(tmp_path)) == []


# ---------------------------------------------------------------------------
# _best_match
# ---------------------------------------------------------------------------


class TestBestMatch:
    def test_single_candidate(self):
        assert _best_match("x/y/z.cpp", ["/repo/z.cpp"]) == "/repo/z.cpp"

    def test_deeper_wins(self):
        build_path = "C:/build/projects/ate/src/Widget.cpp"
        candidates = [
            "/repo/random/Widget.cpp",
            "/repo/projects/ate/src/Widget.cpp",
        ]
        assert _best_match(build_path, candidates) == candidates[1]


# ---------------------------------------------------------------------------
# _read_source_context
# ---------------------------------------------------------------------------


class TestReadSourceContext:
    def test_snippet_around_line(self, tmp_path):
        src = tmp_path / "main.cpp"
        src.write_text("\n".join(f"line {i}" for i in range(1, 20)))
        snippet = _read_source_context(str(src), 10, context=2)
        assert ">>>" in snippet
        assert "line 10" in snippet

    def test_missing_file(self):
        result = _read_source_context("/nonexistent/file.cpp", 5)
        assert "unable to read" in result


# ---------------------------------------------------------------------------
# _parse_faulting_module_function
# ---------------------------------------------------------------------------


class TestParseFaultingModuleFunction:
    def test_symbol_and_module(self):
        text = "MODULE_NAME: MyAppCore\nSYMBOL_NAME:  MyAppCore!ProcessTreeNode+0x9e5\n"
        module, func = _parse_faulting_module_function(text)
        assert module == "MyAppCore"
        assert func == "ProcessTreeNode"

    def test_namespaced_symbol(self):
        text = "MODULE_NAME: MyApp\nSYMBOL_NAME:  MyApp!app::Core::HandleEvent+0x42\n"
        module, func = _parse_faulting_module_function(text)
        assert module == "MyApp"
        assert func == "HandleEvent"

    def test_module_only(self):
        text = "MODULE_NAME: ntdll\n"
        module, func = _parse_faulting_module_function(text)
        assert module == "ntdll"
        assert func is None

    def test_neither(self):
        module, func = _parse_faulting_module_function("no relevant data")
        assert module is None
        assert func is None


# ---------------------------------------------------------------------------
# _extract_stack_functions
# ---------------------------------------------------------------------------


class TestExtractStackFunctions:
    def test_extracts_unique_functions(self):
        text = (
            "00 MyAppCore!ProcessTreeNode+0x9e5\n"
            "01 MyAppCore!GetDocObject+0x123\n"
            "02 MyApp!app::Document::Open+0x42\n"
            "03 MyAppCore!ProcessTreeNode+0xabc\n"  # duplicate
        )
        result = _extract_stack_functions(text)
        assert len(result) == 3
        assert result[0] == ("MyAppCore", "ProcessTreeNode")
        assert result[1] == ("MyAppCore", "GetDocObject")
        assert result[2] == ("MyApp", "Open")

    def test_empty_text(self):
        assert _extract_stack_functions("no stack frames here") == []


# ---------------------------------------------------------------------------
# _find_function_in_repo
# ---------------------------------------------------------------------------


class TestFindFunctionInRepo:
    def test_finds_definition(self, tmp_path):
        src = tmp_path / "Widget.cpp"
        src.write_text("int Widget::doStuff(int x) {\n    return x + 1;\n}\n")
        matches = _find_function_in_repo("doStuff", str(tmp_path))
        assert len(matches) == 1
        assert matches[0][0] == str(src)
        assert matches[0][1] == 1

    def test_finds_standalone_function(self, tmp_path):
        src = tmp_path / "utils.cpp"
        src.write_text(
            "#include <stdio.h>\n\nvoid ProcessTreeNode(TreeObj obj) {\n    // body\n}\n"
        )
        matches = _find_function_in_repo("ProcessTreeNode", str(tmp_path))
        assert len(matches) == 1
        assert matches[0][1] == 3

    def test_module_hint_sorts(self, tmp_path):
        (tmp_path / "other").mkdir()
        (tmp_path / "pdfl").mkdir()
        f1 = tmp_path / "other" / "a.cpp"
        f1.write_text("void MyFunc() {}\n")
        f2 = tmp_path / "pdfl" / "b.cpp"
        f2.write_text("void MyFunc() {}\n")
        matches = _find_function_in_repo("MyFunc", str(tmp_path), module_hint="pdfl")
        assert len(matches) == 2
        assert "pdfl" in matches[0][0].replace("\\", "/").lower()

    def test_no_matches(self, tmp_path):
        src = tmp_path / "empty.cpp"
        src.write_text("int main() { return 0; }\n")
        assert _find_function_in_repo("NonExistent", str(tmp_path)) == []

    def test_ignores_non_source_files(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("void MyFunc() {}\n")
        assert _find_function_in_repo("MyFunc", str(tmp_path)) == []

    def test_searches_gitignored_dirs(self, tmp_path):
        shared = tmp_path / "vendor" / "libs" / "core"
        shared.mkdir(parents=True)
        src = shared / "CoreEngine.cpp"
        src.write_text("bool ProcessTreeNode(TreeObj tree) {\n    return true;\n}\n")
        matches = _find_function_in_repo("ProcessTreeNode", str(tmp_path))
        assert len(matches) == 1
        assert "vendor" in matches[0][0].replace("\\", "/")


# ---------------------------------------------------------------------------
# locate_faulting_source
# ---------------------------------------------------------------------------


class TestLocateFaultingSource:
    def test_no_repo_path(self):
        assert locate_faulting_source("anything", None) is None

    def test_no_faulting_info_at_all(self, tmp_path):
        assert locate_faulting_source("no data", str(tmp_path)) is None

    def test_file_not_found_in_repo(self, tmp_path):
        text = "FAULTING_SOURCE_FILE: Missing.cpp\nFAULTING_SOURCE_LINE_NUMBER: 1\n"
        result = locate_faulting_source(text, str(tmp_path))
        # Falls through to function search; with no SYMBOL_NAME it returns None
        # or a "Could not locate" message if there's enough info.
        assert result is None or "Could not locate" in result

    def test_file_found(self, tmp_path):
        src = tmp_path / "Found.cpp"
        src.write_text("\n".join(f"line {i}" for i in range(1, 30)))
        text = "FAULTING_SOURCE_FILE: C:\\build\\Found.cpp\nFAULTING_SOURCE_LINE_NUMBER: 10\n"
        result = locate_faulting_source(text, str(tmp_path))
        assert "### Faulting Source Code" in result
        assert "Found.cpp" in result

    def test_fallback_to_function_search(self, tmp_path):
        src = tmp_path / "CoreEngine.cpp"
        src.write_text(
            "// core engine\nbool ProcessTreeNode(TreeObj tree) {\n    return true;\n}\n"
        )
        text = "MODULE_NAME: MyAppCore\nSYMBOL_NAME:  MyAppCore!ProcessTreeNode+0x9e5\n"
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "Located by Symbol Name Search" in result
        assert "ProcessTreeNode" in result
        assert "CoreEngine.cpp" in result

    def test_fallback_to_stack_trace_search(self, tmp_path):
        src = tmp_path / "DocUtils.cpp"
        src.write_text("void GetDocObject(int x) { }\n")
        text = (
            "MODULE_NAME: MyAppCore\n"
            "SYMBOL_NAME: MyAppCore+9e5\n"  # no function name in SYMBOL_NAME
            "Child-SP          RetAddr           Call Site\n"
            "00 MyAppCore!GetDocObject+0x123\n"
        )
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "Located by Stack Trace Search" in result
        assert "GetDocObject" in result

    def test_nothing_found_returns_diagnostic(self, tmp_path):
        text = "MODULE_NAME: MyAppCore\nSYMBOL_NAME:  MyAppCore!SomeObscureFunc+0x9e5\n"
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "Could not locate source" in result
        assert "MyAppCore" in result
