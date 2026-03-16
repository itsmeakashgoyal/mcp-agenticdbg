"""Centralized configuration for triagepilot using pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    """Server configuration populated from env vars (prefix ``TRIAGEPILOT_``) and CLI args."""

    model_config = SettingsConfigDict(env_prefix="TRIAGEPILOT_")

    debugger_type: str = "auto"
    debugger_path: str | None = None
    cdb_path: str | None = None
    symbols_path: str | None = None
    image_path: str | None = None
    repo_path: str | None = None
    timeout: int = 30
    verbose: bool = False
    log_level: str = "INFO"

    # LLM / LangGraph settings (only used when langgraph extra is installed)
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    llm_api_key: str | None = None
    langsmith_api_key: str | None = None
    langsmith_project: str = "triagepilot"
    max_retries: int = 3

    # Session pool
    max_concurrent_sessions: int = 5

    # Persistent memory system
    memory_enabled: bool = True
    memory_db_path: str | None = None  # default: ~/.triagepilot/memory.db
    memory_max_entries: int = 10000
    memory_confidence_half_life_days: float = 90.0
    memory_auto_recall: bool = True
    memory_auto_save: bool = True

    @property
    def effective_debugger_path(self) -> str | None:
        """Return ``debugger_path`` if set, otherwise fall back to ``cdb_path``."""
        return self.debugger_path or self.cdb_path
