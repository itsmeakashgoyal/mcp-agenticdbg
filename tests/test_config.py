"""Tests for ServerConfig env-var loading."""

from triagepilot.config import ServerConfig


class TestServerConfigDefaults:
    def test_default_values(self):
        cfg = ServerConfig()
        assert cfg.cdb_path is None
        assert cfg.debugger_type == "auto"
        assert cfg.debugger_path is None
        assert cfg.timeout == 30
        assert cfg.verbose is False
        assert cfg.log_level == "INFO"
        assert cfg.llm_provider == "openai"
        assert cfg.llm_model == "gpt-4o"
        assert cfg.max_retries == 3
        assert cfg.max_concurrent_sessions == 5

    def test_effective_debugger_path_none(self):
        cfg = ServerConfig()
        assert cfg.effective_debugger_path is None


class TestServerConfigEnvOverride:
    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_TIMEOUT", "120")
        monkeypatch.setenv("TRIAGEPILOT_VERBOSE", "true")
        monkeypatch.setenv("TRIAGEPILOT_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("TRIAGEPILOT_LLM_PROVIDER", "anthropic")
        cfg = ServerConfig()
        assert cfg.timeout == 120
        assert cfg.verbose is True
        assert cfg.log_level == "DEBUG"
        assert cfg.llm_provider == "anthropic"

    def test_cdb_path_from_env(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_CDB_PATH", "C:\\custom\\cdb.exe")
        cfg = ServerConfig()
        assert cfg.cdb_path == "C:\\custom\\cdb.exe"

    def test_debugger_type_from_env(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_DEBUGGER_TYPE", "lldb")
        cfg = ServerConfig()
        assert cfg.debugger_type == "lldb"

    def test_debugger_path_from_env(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_DEBUGGER_PATH", "/usr/local/bin/lldb")
        cfg = ServerConfig()
        assert cfg.debugger_path == "/usr/local/bin/lldb"

    def test_effective_debugger_path_prefers_debugger_path(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_DEBUGGER_PATH", "/usr/bin/lldb")
        monkeypatch.setenv("TRIAGEPILOT_CDB_PATH", "C:\\cdb.exe")
        cfg = ServerConfig()
        assert cfg.effective_debugger_path == "/usr/bin/lldb"

    def test_effective_debugger_path_falls_back_to_cdb(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_CDB_PATH", "C:\\cdb.exe")
        cfg = ServerConfig()
        assert cfg.effective_debugger_path == "C:\\cdb.exe"


class TestServerConfigExplicitOverride:
    def test_kwargs_override_env(self, monkeypatch):
        monkeypatch.setenv("TRIAGEPILOT_TIMEOUT", "120")
        cfg = ServerConfig(timeout=60)
        assert cfg.timeout == 60

    def test_debugger_type_kwarg(self):
        cfg = ServerConfig(debugger_type="gdb")
        assert cfg.debugger_type == "gdb"
