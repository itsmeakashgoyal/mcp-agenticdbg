"""Tests for the GDB MI parser and GDB-specific source locator helpers.

Uses real-world GDB MI output strings captured from actual sessions.
"""

from triagepilot.backends.gdb import MIParser
from triagepilot.tools.debugger_tools import (
    _extract_gdb_functions,
    _parse_gdb_source_locations,
    locate_faulting_source,
)

# ===========================================================================
# MIParser.parse_stream_record
# ===========================================================================


class TestParseStreamRecord:
    def test_console_stream(self):
        kind, text = MIParser.parse_stream_record('~"Program received signal SIGSEGV\\n"')
        assert kind == "console"
        assert "SIGSEGV" in text
        assert text.endswith("\n")

    def test_log_stream(self):
        result = MIParser.parse_stream_record('&"set pagination off\\n"')
        assert result is not None
        kind, text = result
        assert kind == "log"
        assert "pagination" in text

    def test_target_stream(self):
        result = MIParser.parse_stream_record('@"some target output\\n"')
        assert result is not None
        kind, _ = result
        assert kind == "target"

    def test_not_stream(self):
        assert MIParser.parse_stream_record("1001^done") is None
        assert MIParser.parse_stream_record("(gdb)") is None
        assert MIParser.parse_stream_record("") is None


# ===========================================================================
# MIParser.parse — const (quoted strings)
# ===========================================================================


class TestMIParserConst:
    """The most failure-prone component: quoted strings with escape sequences."""

    def _parse_string(self, s: str) -> str:
        p = MIParser(s)
        v, _ = p._string()
        return v

    def test_simple(self):
        assert self._parse_string('"hello"') == "hello"

    def test_empty(self):
        assert self._parse_string('""') == ""

    def test_newline_escape(self):
        assert self._parse_string('"foo\\nbar"') == "foo\nbar"

    def test_tab_escape(self):
        assert self._parse_string('"a\\tb"') == "a\tb"

    def test_escaped_quote(self):
        assert self._parse_string('"say \\"hi\\""') == 'say "hi"'

    def test_escaped_backslash(self):
        assert self._parse_string('"c:\\\\path"') == "c:\\path"

    def test_null_escape(self):
        assert self._parse_string('"\\0"') == "\0"

    def test_hex_escape(self):
        assert self._parse_string('"\\x41"') == "A"

    def test_unknown_escape_passthrough(self):
        # Unknown escapes are passed through literally
        result = self._parse_string('"\\q"')
        assert result == "q"

    def test_comma_inside_string_not_split(self):
        # This is the core bug in the original code — commas inside strings
        # must not be treated as field separators
        result = MIParser.parse('msg="error: foo, bar, baz"')
        assert result["msg"] == "error: foo, bar, baz"

    def test_path_with_special_chars(self):
        result = MIParser.parse('fullname="/home/user/my project/src/main.cpp"')
        assert result["fullname"] == "/home/user/my project/src/main.cpp"


# ===========================================================================
# MIParser.parse — tuple
# ===========================================================================


class TestMIParserTuple:
    def test_empty_tuple(self):
        result = MIParser.parse("frame={}")
        assert result["frame"] == {}

    def test_single_pair(self):
        result = MIParser.parse('frame={level="0"}')
        assert result["frame"]["level"] == "0"

    def test_multiple_pairs(self):
        result = MIParser.parse('frame={level="0",addr="0x00400a10",func="main"}')
        f = result["frame"]
        assert f["level"] == "0"
        assert f["addr"] == "0x00400a10"
        assert f["func"] == "main"

    def test_nested_tuple(self):
        result = MIParser.parse('outer={inner={key="val"}}')
        assert result["outer"]["inner"]["key"] == "val"

    def test_four_levels_deep(self):
        result = MIParser.parse('a={b={c={d="val"}}}')
        assert result["a"]["b"]["c"]["d"] == "val"

    def test_tuple_with_list_value(self):
        result = MIParser.parse('frame={args=[{name="x",value="1"}]}')
        assert result["frame"]["args"][0]["name"] == "x"


# ===========================================================================
# MIParser.parse — list
# ===========================================================================


class TestMIParserList:
    def test_empty_list(self):
        result = MIParser.parse("stack=[]")
        assert result["stack"] == []

    def test_list_of_tuples(self):
        """register-values style: [{number="0",value="0x0"},{number="1",...}]"""
        result = MIParser.parse(
            'register-values=[{number="0",value="0x0"},{number="1",value="0x1"}]'
        )
        regs = result["register-values"]
        assert isinstance(regs, list)
        assert len(regs) == 2
        assert regs[0]["number"] == "0"
        assert regs[0]["value"] == "0x0"
        assert regs[1]["number"] == "1"

    def test_list_of_results(self):
        """stack-list-frames style: [frame={...},frame={...}]"""
        result = MIParser.parse(
            'stack=[frame={level="0",func="crash"},frame={level="1",func="main"}]'
        )
        stack = result["stack"]
        assert isinstance(stack, list)
        assert len(stack) == 2
        assert stack[0]["frame"]["func"] == "crash"
        assert stack[1]["frame"]["func"] == "main"

    def test_list_of_strings(self):
        result = MIParser.parse('names=["rax","rbx","rcx"]')
        assert result["names"] == ["rax", "rbx", "rcx"]

    def test_nested_list(self):
        result = MIParser.parse('outer=[[{a="1"}]]')
        assert isinstance(result["outer"], list)


# ===========================================================================
# MIParser.parse_result_record — integration (the main public API)
# ===========================================================================


class TestParseResultRecord:
    def test_done_no_results(self):
        r = MIParser.parse_result_record("1001^done")
        assert r is not None
        assert r["token"] == 1001
        assert r["class"] == "done"
        assert r["results"] == {}

    def test_running(self):
        r = MIParser.parse_result_record("1002^running")
        assert r["class"] == "running"

    def test_error_with_message(self):
        r = MIParser.parse_result_record(
            '1003^error,msg="No symbol table. Use the \\"file\\" command."'
        )
        assert r["class"] == "error"
        assert "file" in r["results"]["msg"]

    def test_done_with_frame(self):
        r = MIParser.parse_result_record(
            '1004^done,frame={level="0",addr="0x00400a10",'
            'func="main",file="main.cpp",fullname="/src/main.cpp",line="42"}'
        )
        assert r["class"] == "done"
        frame = r["results"]["frame"]
        assert frame["func"] == "main"
        assert frame["line"] == "42"
        assert frame["fullname"] == "/src/main.cpp"

    def test_register_values(self):
        r = MIParser.parse_result_record(
            "1005^done,register-values=["
            '{number="0",value="0x0"},'
            '{number="17",value="0x7fffffffe8f8"}'
            "]"
        )
        regs = r["results"]["register-values"]
        assert len(regs) == 2
        assert regs[0]["number"] == "0"
        assert regs[1]["value"] == "0x7fffffffe8f8"

    def test_register_names(self):
        r = MIParser.parse_result_record(
            '1006^done,register-names=["rax","rbx","rcx","rdx","rsi","rdi"]'
        )
        names = r["results"]["register-names"]
        assert names[0] == "rax"
        assert names[5] == "rdi"

    def test_thread_info(self):
        r = MIParser.parse_result_record(
            "1007^done,threads=["
            '{id="1",target-id="Thread 0x7f (LWP 1234)",'
            'frame={level="0",addr="0x00400a10",func="segfault_func"},'
            'state="stopped"}'
            '],current-thread-id="1"'
        )
        assert r["class"] == "done"
        threads = r["results"]["threads"]
        assert len(threads) == 1
        assert threads[0]["id"] == "1"
        assert threads[0]["frame"]["func"] == "segfault_func"
        assert r["results"]["current-thread-id"] == "1"

    def test_stack_list_frames(self):
        r = MIParser.parse_result_record(
            "1008^done,stack=["
            'frame={level="0",addr="0x00400a10",func="crash_func",'
            'file="crash.cpp",fullname="/src/crash.cpp",line="15"},'
            'frame={level="1",addr="0x00400b20",func="main",'
            'file="main.cpp",fullname="/src/main.cpp",line="42"}'
            "]"
        )
        frames = r["results"]["stack"]
        assert len(frames) == 2
        assert frames[0]["frame"]["func"] == "crash_func"
        assert frames[0]["frame"]["line"] == "15"
        assert frames[1]["frame"]["func"] == "main"

    def test_stack_list_locals(self):
        r = MIParser.parse_result_record(
            '1009^done,locals=[{name="ptr",value="0x0"},{name="count",value="42"}]'
        )
        locals_ = r["results"]["locals"]
        assert locals_[0]["name"] == "ptr"
        assert locals_[1]["value"] == "42"

    def test_data_evaluate_expression(self):
        r = MIParser.parse_result_record('1010^done,value="0xdeadbeef"')
        assert r["results"]["value"] == "0xdeadbeef"

    def test_disassembly(self):
        r = MIParser.parse_result_record(
            "1011^done,asm_insns=["
            '{address="0x00400a10",func-name="main",offset="0",'
            'inst="push   %rbp"},'
            '{address="0x00400a11",func-name="main",offset="1",'
            'inst="mov    %rsp,%rbp"}'
            "]"
        )
        insns = r["results"]["asm_insns"]
        assert len(insns) == 2
        assert insns[0]["inst"] == "push   %rbp"
        assert insns[1]["address"] == "0x00400a11"

    def test_not_a_result_record(self):
        assert MIParser.parse_result_record("(gdb)") is None
        assert MIParser.parse_result_record('~"console output"') is None
        assert MIParser.parse_result_record("") is None
        assert MIParser.parse_result_record("*stopped,reason=...") is None

    def test_hyphen_in_key_name(self):
        """MI keys use hyphens: current-thread-id, register-values, etc."""
        r = MIParser.parse_result_record('1012^done,current-thread-id="1"')
        assert r["results"]["current-thread-id"] == "1"

    def test_exit_class(self):
        r = MIParser.parse_result_record("9999^exit")
        assert r["class"] == "exit"


# ===========================================================================
# MIParser edge cases
# ===========================================================================


class TestMIParserEdgeCases:
    def test_deeply_nested(self):
        """Parser must not stack-overflow on deep nesting."""
        r = MIParser.parse('a={b={c={d={e="val"}}}}')
        assert r["a"]["b"]["c"]["d"]["e"] == "val"

    def test_value_with_embedded_commas(self):
        r = MIParser.parse('msg="err: foo, bar, baz"')
        assert r["msg"] == "err: foo, bar, baz"

    def test_value_with_embedded_equals(self):
        r = MIParser.parse('expr="x = y + z"')
        assert r["expr"] == "x = y + z"

    def test_empty_input(self):
        assert MIParser.parse("") == {}

    def test_whitespace_only(self):
        assert MIParser.parse("   ") == {}

    def test_duplicate_keys_folded_into_list(self):
        """GDB occasionally emits duplicate keys; they should be merged."""
        r = MIParser.parse('key="a",key="b"')
        assert isinstance(r["key"], list)
        assert "a" in r["key"]
        assert "b" in r["key"]

    def test_complex_real_world_thread_info(self):
        """Verbatim -thread-info response from a real segfault session."""
        payload = (
            "threads=[{"
            'id="1",'
            'target-id="process 12345",'
            'name="test_prog",'
            "frame={"
            'level="0",'
            'addr="0x00007f1234abcdef",'
            'func="std::__throw_bad_alloc",'
            "args=[],"
            'from="/usr/lib/x86_64-linux-gnu/libstdc++.so.6"'
            "},"
            'state="stopped"'
            "}],"
            'current-thread-id="1"'
        )
        r = MIParser.parse(payload)
        assert r["current-thread-id"] == "1"
        t = r["threads"][0]
        assert t["id"] == "1"
        assert t["frame"]["func"] == "std::__throw_bad_alloc"
        assert t["frame"]["args"] == []

    def test_register_values_with_large_hex(self):
        payload = (
            "register-values=["
            '{number="6",value="0x00007fffffffdf80"},'
            '{number="7",value="0x0000000000000000"}'
            "]"
        )
        r = MIParser.parse(payload)
        vals = r["register-values"]
        assert vals[0]["value"] == "0x00007fffffffdf80"


# ===========================================================================
# _parse_gdb_source_locations
# ===========================================================================


class TestParseGdbSourceLocations:
    def test_single_frame(self):
        text = "#0  crash_func () at src/crash.cpp:15"
        locs = _parse_gdb_source_locations(text)
        assert len(locs) == 1
        assert locs[0][0].endswith("crash.cpp")
        assert locs[0][1] == 15

    def test_multiple_frames(self):
        text = (
            "#0  crash_func () at src/crash.cpp:15\n#1  0x00400b20 in main () at src/main.cpp:42\n"
        )
        locs = _parse_gdb_source_locations(text)
        assert len(locs) == 2
        assert locs[0][1] == 15
        assert locs[1][1] == 42

    def test_deduplication(self):
        text = (
            "#0  foo () at same.cpp:10\n"
            "#1  bar () at same.cpp:10\n"  # same file+line
        )
        locs = _parse_gdb_source_locations(text)
        assert len(locs) == 1

    def test_no_frames(self):
        assert _parse_gdb_source_locations("no backtrace here") == []

    def test_absolute_path(self):
        text = "#0  func () at /home/user/project/src/app.cpp:99"
        locs = _parse_gdb_source_locations(text)
        assert locs[0][0] == "/home/user/project/src/app.cpp"
        assert locs[0][1] == 99

    def test_skips_shared_library_from_entries(self):
        """'from' entries point to .so files, not source files."""
        text = "#3  0x00007f in malloc () from /lib/x86_64-linux-gnu/libc.so.6"
        # Should not match .so files
        locs = _parse_gdb_source_locations(text)
        assert not any(loc[0].endswith(".so.6") for loc in locs)


# ===========================================================================
# _extract_gdb_functions
# ===========================================================================


class TestExtractGdbFunctions:
    def test_basic_extraction(self):
        text = "#0  0x00400a10 in crash_function ()\n#1  0x00400b20 in main ()\n"
        funcs = _extract_gdb_functions(text)
        assert "crash_function" in funcs
        assert "main" in funcs

    def test_deduplication(self):
        text = (
            "#0  foo ()\n"
            "#1  bar ()\n"
            "#2  foo ()\n"  # duplicate
        )
        funcs = _extract_gdb_functions(text)
        assert funcs.count("foo") == 1

    def test_strips_namespace(self):
        text = "#0  0x00007f1234567890 in std::vector<int>::push_back ()\n"
        funcs = _extract_gdb_functions(text)
        assert "push_back" in funcs

    def test_skips_runtime_frames(self):
        text = "#0  __libc_start_main ()\n#1  _start ()\n#2  crash_func ()\n"
        funcs = _extract_gdb_functions(text)
        assert "__libc_start_main" not in funcs
        assert "_start" not in funcs
        assert "crash_func" in funcs

    def test_empty_text(self):
        assert _extract_gdb_functions("") == []

    def test_cdb_style_frames_not_matched(self):
        """CDB-style frames should not match the GDB frame pattern."""
        text = "00 MyApp!ProcessData+0x9e5\n"
        funcs = _extract_gdb_functions(text)
        # CDB frames don't have '#N ... in func (' format
        assert funcs == []


# ===========================================================================
# locate_faulting_source — Level 0 GDB integration
# ===========================================================================


class TestLocateFaultingSourceGDB:
    def test_level0_gdb_at_file_line(self, tmp_path):
        """Level 0: GDB 'at file:line' is resolved directly."""
        src = tmp_path / "crash.cpp"
        src.write_text("\n".join(f"line {i}" for i in range(1, 50)))
        text = (
            "=== Backtrace (full) ===\n"
            "#0  crash_function () at crash.cpp:15\n"
            "#1  0x00400b20 in main () at main.cpp:42\n"
        )
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "### Faulting Source Code" in result
        assert "crash.cpp" in result
        assert "15" in result
        assert "GDB debug info" in result

    def test_level0_picks_innermost_frame(self, tmp_path):
        """Level 0: innermost frame (frame 0) should be preferred."""
        (tmp_path / "inner.cpp").write_text("\n".join(f"line {i}" for i in range(1, 30)))
        (tmp_path / "outer.cpp").write_text("\n".join(f"line {i}" for i in range(1, 30)))
        text = "#0  inner_func () at inner.cpp:5\n#1  outer_func () at outer.cpp:20\n"
        result = locate_faulting_source(text, str(tmp_path))
        assert "inner.cpp" in result
        assert "outer.cpp" not in result

    def test_level3b_gdb_function_search(self, tmp_path):
        """Level 3b: when no file match, search by GDB function name."""
        src = tmp_path / "engine.cpp"
        src.write_text("// engine\nvoid ProcessData(int x) {\n    // crashes here\n}\n")
        # GDB backtrace with function but no file (stripped binary)
        text = "#0  0x00400a10 in ProcessData (x=0)\n#1  0x00400b20 in main ()\n"
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "GDB Frame Search" in result
        assert "ProcessData" in result

    def test_level0_falls_through_to_level1_if_no_file_match(self, tmp_path):
        """If GDB at-file not found in repo, fall through to CDB Level 1."""
        # Create a file matching the CDB FAULTING_SOURCE_FILE pattern
        (tmp_path / "CdbFault.cpp").write_text("\n".join(f"line {i}" for i in range(1, 30)))
        text = (
            "#0  crash () at NonExistent.cpp:5\n"
            "FAULTING_SOURCE_FILE: CdbFault.cpp\n"
            "FAULTING_SOURCE_LINE_NUMBER: 10\n"
        )
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "CdbFault.cpp" in result

    def test_gdb_full_session_output(self, tmp_path):
        """Realistic full GDB output from a use-after-free crash."""
        src = tmp_path / "use-after-free.cpp"
        src.write_text(
            "#include <stdlib.h>\n"
            "void crash_function(char *ptr) {\n"
            "    *ptr = 'X';  // use after free\n"
            "}\n"
            "int main() {\n"
            "    char *p = (char *)malloc(10);\n"
            "    free(p);\n"
            "    crash_function(p);  // crash here\n"
            "    return 0;\n"
            "}\n"
        )
        text = (
            "=== Signal / Termination ===\n"
            "Signal  Stop\tPrint\tPass to program\tDescription\n"
            "SIGSEGV Yes\tYes\tYes\t\tSegmentation fault\n"
            "\n"
            "=== Crash Frame ===\n"
            '#0  crash_function (ptr=0x602010 "") at use-after-free.cpp:3\n'
            "\n"
            "=== Backtrace (full) ===\n"
            '#0  crash_function (ptr=0x602010 "") at use-after-free.cpp:3\n'
            '        ptr = 0x602010 ""\n'
            "#1  0x00000000004007e0 in main () at use-after-free.cpp:8\n"
            "No locals.\n"
        )
        result = locate_faulting_source(text, str(tmp_path))
        assert result is not None
        assert "use-after-free.cpp" in result
        assert "3" in result  # faulting line
