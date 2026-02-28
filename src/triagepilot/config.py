"""Centralized configuration for triagepilot using pydantic-settings."""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    """Server configuration populated from env vars (prefix ``TRIAGEPILOT_``) and CLI args."""

    model_config = SettingsConfigDict(env_prefix="TRIAGEPILOT_")

    debugger_type: str = "auto"
    debugger_path: Optional[str] = None
    cdb_path: Optional[str] = None
    symbols_path: Optional[str] = None
    image_path: Optional[str] = None
    repo_path: Optional[str] = None
    timeout: int = 30
    verbose: bool = False
    log_level: str = "INFO"

    # LLM / LangGraph settings (only used when langgraph extra is installed)
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    llm_api_key: Optional[str] = None
    langsmith_api_key: Optional[str] = None
    langsmith_project: str = "triagepilot"
    max_retries: int = 3

    # Session pool
    max_concurrent_sessions: int = 5

    @property
    def effective_debugger_path(self) -> Optional[str]:
        """Return ``debugger_path`` if set, otherwise fall back to ``cdb_path``."""
        return self.debugger_path or self.cdb_path
