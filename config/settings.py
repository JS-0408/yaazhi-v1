"""
Yaazhi Configuration — Pydantic BaseSettings.

All configuration is loaded from environment variables (config/.env).
No hardcoded secrets, URLs, or ports anywhere in the system.

Usage:
    from config.settings import settings
    api_key = settings.groq_api_key
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ─── Nested Config Groups ────────────────────────────────────────────────────

class LLMSettings(BaseSettings):
    """Settings for language model API keys and endpoints."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    groq_api_key: str = Field(default="", description="Groq API key for fast inference")
    azure_openai_endpoint: str = Field(default="", description="Azure OpenAI endpoint URL")
    azure_openai_api_key: str = Field(default="", description="Azure OpenAI API key")
    azure_openai_deployment: str = Field(default="gpt-4o", description="Azure deployment name")
    azure_openai_api_version: str = Field(default="2024-02-01", description="Azure API version")
    google_api_key: str = Field(default="", description="Google AI API key for Gemini")
    anthropic_api_key: str = Field(default="", description="Anthropic API key for Claude")
    ollama_base_url: str = Field(default="http://localhost:11434", description="Ollama server URL")


class MemorySettings(BaseSettings):
    """Settings for all memory and database connections."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    chromadb_host: str = Field(default="localhost", description="ChromaDB hostname")
    chromadb_port: int = Field(default=8001, description="ChromaDB port")
    postgres_url: str = Field(default="", description="PostgreSQL connection URL with pgvector")
    redis_url: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")
    mem0_api_key: str = Field(default="", description="Mem0 API key for episodic memory")
    supabase_url: str = Field(default="", description="Supabase project URL")
    supabase_key: str = Field(default="", description="Supabase anonymous key")


class VoiceSettings(BaseSettings):
    """Settings for voice and language processing."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    bhashini_api_key: str = Field(default="", description="Bhashini government API key")
    bhashini_user_id: str = Field(default="", description="Bhashini user ID")
    bhashini_pipeline_url: str = Field(
        default="https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline",
        description="Bhashini pipeline endpoint",
    )
    whisper_model_size: str = Field(default="base", description="Whisper model size")
    wakeword_sensitivity: float = Field(default=0.7, description="Wake word detection threshold")
    telegram_bot_token: str = Field(default="", description="Telegram Bot API token")
    telegram_chat_id: str = Field(default="", description="Telegram chat ID for notifications")

    @field_validator("wakeword_sensitivity")
    @classmethod
    def validate_sensitivity(cls, v: float) -> float:
        """Ensure sensitivity is between 0 and 1."""
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"wakeword_sensitivity must be 0.0–1.0, got {v}")
        return v


class ActionsSettings(BaseSettings):
    """Settings for external action integrations."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    firecrawl_api_key: str = Field(default="", description="Firecrawl web scraping API key")
    n8n_webhook_base_url: str = Field(
        default="http://localhost:5678/webhook",
        description="n8n webhook base URL for notifications",
    )
    n8n_basic_auth_user: str = Field(default="yaazhi_admin", description="n8n basic auth user")
    n8n_basic_auth_password: str = Field(default="", description="n8n basic auth password")
    mqtt_broker_host: str = Field(default="localhost", description="MQTT broker hostname")
    mqtt_broker_port: int = Field(default=1883, description="MQTT broker port")
    mqtt_username: str = Field(default="yaazhi", description="MQTT username")
    mqtt_password: str = Field(default="", description="MQTT password")


class OpsSettings(BaseSettings):
    """Settings for observability and operations."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    langchain_tracing_v2: bool = Field(default=True, description="Enable LangSmith tracing")
    langchain_api_key: str = Field(default="", description="LangSmith API key")
    langchain_project: str = Field(default="yaazhi-production", description="LangSmith project name")
    logfire_token: str = Field(default="", description="Logfire observability token")
    cloudflare_tunnel_token: str = Field(default="", description="Cloudflare tunnel token")
    grafana_admin_user: str = Field(default="admin", description="Grafana admin username")
    grafana_admin_password: str = Field(default="", description="Grafana admin password")


class AppSettings(BaseSettings):
    """Core application settings."""

    model_config = SettingsConfigDict(env_file="config/.env", extra="ignore")

    app_name: str = Field(default="Yaazhi", description="Application name")
    app_version: str = Field(default="1.0.0", description="Application version")
    owner_name: str = Field(default="Santhosh", description="System owner name")
    owner_email: str = Field(default="", description="Owner email")
    yaazhi_api_key: str = Field(default="", description="API key for Yaazhi REST API auth")
    app_host: str = Field(default="0.0.0.0", description="FastAPI bind host")
    app_port: int = Field(default=8000, description="FastAPI bind port")
    debug: bool = Field(default=False, description="Enable debug mode")
    allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:8501",
        description="Comma-separated CORS allowed origins",
    )
    max_loop_count: int = Field(default=5, description="Max LangGraph reasoning loop iterations")
    backup_passphrase: str = Field(default="", description="Passphrase for encrypted backups")
    knowledge_base_dir: str = Field(default="knowledge", description="Knowledge base root directory")
    log_dir: str = Field(default="logs", description="Logs directory")

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse comma-separated origins into a list.

        Returns:
            List of allowed CORS origin strings.
        """
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


# ─── Root Settings ────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    """
    Master settings class combining all configuration groups.

    Loads from config/.env file and environment variables.
    All secrets come from environment — zero hardcoded values.
    """

    model_config = SettingsConfigDict(
        env_file="config/.env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── LLM Keys ────────────────────────────────────────
    groq_api_key: str = Field(default="")
    azure_openai_endpoint: str = Field(default="")
    azure_openai_api_key: str = Field(default="")
    azure_openai_deployment: str = Field(default="gpt-4o")
    azure_openai_api_version: str = Field(default="2024-02-01")
    google_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    ollama_base_url: str = Field(default="http://localhost:11434")

    # ── Memory ───────────────────────────────────────────
    chromadb_host: str = Field(default="localhost")
    chromadb_port: int = Field(default=8001)
    postgres_url: str = Field(default="")
    redis_url: str = Field(default="redis://localhost:6379/0")
    mem0_api_key: str = Field(default="")
    supabase_url: str = Field(default="")
    supabase_key: str = Field(default="")

    # ── Voice ────────────────────────────────────────────
    bhashini_api_key: str = Field(default="")
    bhashini_user_id: str = Field(default="")
    bhashini_pipeline_url: str = Field(
        default="https://meity-auth.ulcacontrib.org/ulca/apis/v0/model/getModelsPipeline"
    )
    whisper_model_size: str = Field(default="base")
    wakeword_sensitivity: float = Field(default=0.7)
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # ── Actions ───────────────────────────────────────────
    firecrawl_api_key: str = Field(default="")
    n8n_webhook_base_url: str = Field(default="http://localhost:5678/webhook")
    n8n_basic_auth_user: str = Field(default="yaazhi_admin")
    n8n_basic_auth_password: str = Field(default="")
    mqtt_broker_host: str = Field(default="localhost")
    mqtt_broker_port: int = Field(default=1883)
    mqtt_username: str = Field(default="yaazhi")
    mqtt_password: str = Field(default="")

    # ── Ops ────────────────────────────────────────────────
    langchain_tracing_v2: bool = Field(default=True)
    langchain_api_key: str = Field(default="")
    langchain_project: str = Field(default="yaazhi-production")
    logfire_token: str = Field(default="")
    cloudflare_tunnel_token: str = Field(default="")
    grafana_admin_user: str = Field(default="admin")
    grafana_admin_password: str = Field(default="")

    # ── App ────────────────────────────────────────────────
    app_name: str = Field(default="Yaazhi")
    app_version: str = Field(default="1.0.0")
    owner_name: str = Field(default="Santhosh")
    owner_email: str = Field(default="")
    yaazhi_api_key: str = Field(default="")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    debug: bool = Field(default=False)
    env: str = Field(default="development", description="Runtime environment: development|production")
    allowed_origins: str = Field(default="http://localhost:3000,http://localhost:8501")
    max_loop_count: int = Field(default=5)
    backup_passphrase: str = Field(default="")
    knowledge_base_dir: str = Field(default="knowledge")
    log_dir: str = Field(default="logs")
    # SEC-9 fix: not hardcoded — read from env or default to 'default'
    default_user_id: str = Field(default="default", description="Default user namespace for memory ops")
    uploads_path: str = Field(default="/tmp/yaazhi_uploads", description="Directory for uploaded files")
    daily_budget_usd: float = Field(default=1.0, description="Daily LLM spend alert threshold in USD")
    chroma_persist_path: str = Field(default="./chroma_data", description="ChromaDB local persistence path")

    @field_validator("allowed_origins")
    @classmethod
    def no_wildcard_in_production(cls, v: str, info: Any) -> str:
        """SEC-6 FIX: Block CORS wildcard (*) in production environment."""
        env = os.getenv("ENV", "development")
        if env == "production" and "*" in v:
            raise ValueError(
                "CORS wildcard (*) is not allowed in production. "
                "Set specific origins in ALLOWED_ORIGINS."
            )
        return v

    @property
    def allowed_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list.

        Returns:
            List of allowed CORS origin strings.
        """
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def chromadb_url(self) -> str:
        """Construct ChromaDB HTTP URL from host and port.

        Returns:
            Full ChromaDB URL string.
        """
        return f"http://{self.chromadb_host}:{self.chromadb_port}"

    def validate_critical_keys(self) -> None:
        """
        Check that the minimum required API keys are set.

        Raises:
            ValueError: If any required keys are missing, listing all missing ones.
        """
        required: dict[str, str] = {
            "GROQ_API_KEY": self.groq_api_key,
            "POSTGRES_URL": self.postgres_url,
            "REDIS_URL": self.redis_url,
        }
        missing: list[str] = [name for name, val in required.items() if not val.strip()]
        if missing:
            raise ValueError(
                f"Critical configuration keys are missing from config/.env: "
                f"{', '.join(missing)}. "
                f"Copy config/.env.example to config/.env and fill in the values."
            )

    def get_litellm_model(self, task_type: str) -> str:
        """
        Return the correct LiteLLM model string for a given task type.

        Reads from config/models.yaml routing_rules section.
        Result is cached in-process — the file is only read once per process lifetime.
        (VECTOR-2 FIX: was re-opened and re-parsed on every single LLM call.)

        Args:
            task_type: One of 'research', 'code', 'read_doc', 'browse',
                       'notify', 'memory', 'voice', 'review'.

        Returns:
            LiteLLM-compatible model string (e.g. 'groq/llama-3.3-70b-versatile').

        Raises:
            FileNotFoundError: If config/models.yaml does not exist.
            KeyError: If task_type is not found in routing_rules.
        """
        models_config = self._load_models_yaml()
        routing_rules: dict[str, str] = models_config.get("routing_rules", {})
        model_key: str = routing_rules.get(task_type, "fast_tasks")
        model_entry: dict[str, Any] = models_config.get(model_key, {})
        return str(model_entry.get("model_string", "groq/llama-3.3-70b-versatile"))

    def get_fallback_model(self, task_type: str) -> str:
        """
        Return the fallback LiteLLM model string for a given task type.

        Args:
            task_type: One of the recognised Yaazhi task type strings.

        Returns:
            LiteLLM-compatible fallback model string.

        Raises:
            FileNotFoundError: If config/models.yaml does not exist.
        """
        models_config = self._load_models_yaml()
        routing_rules: dict[str, str] = models_config.get("routing_rules", {})
        model_key: str = routing_rules.get(task_type, "fast_tasks")
        model_entry: dict[str, Any] = models_config.get(model_key, {})
        return str(model_entry.get("fallback_model_string", "ollama/llama3.2"))

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    _models_yaml_cache: Optional[dict] = None

    def _load_models_yaml(self) -> dict[str, Any]:
        """
        Load and cache config/models.yaml in-process.

        VECTOR-2 FIX: The file was previously re-opened on every get_litellm_model
        / get_fallback_model call.  This method caches the result on first load.
        """
        if self._models_yaml_cache is not None:
            return self._models_yaml_cache  # type: ignore[return-value]

        models_path = Path("config/models.yaml")
        if not models_path.exists():
            raise FileNotFoundError("config/models.yaml not found")

        with models_path.open("r", encoding="utf-8") as fh:
            data: dict[str, Any] = yaml.safe_load(fh)

        Settings._models_yaml_cache = data
        return data

    def __repr__(self) -> str:
        """Return a safe string representation without exposing secrets.

        Returns:
            String with app name, version, and whether debug is enabled.
        """
        return (
            f"Settings(app={self.app_name!r}, version={self.app_version!r}, "
            f"debug={self.debug}, port={self.app_port})"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the cached Settings singleton.

    Uses lru_cache to ensure .env is parsed only once per process.

    Returns:
        The global Settings instance.
    """
    return Settings()


# ─── Global singleton ─────────────────────────────────────────────────────────
settings: Settings = get_settings()
