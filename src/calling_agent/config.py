"""Application configuration, loaded from environment / .env."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    assemblyai_api_key: str = Field(default="", alias="ASSEMBLYAI_API_KEY")
    assemblyai_agent_ws_url: str = Field(
        default="wss://agents.assemblyai.com/v1/ws", alias="ASSEMBLYAI_AGENT_WS_URL"
    )

    agent_voice: str = Field(default="ivy", alias="AGENT_VOICE")
    agent_greeting: str = Field(
        default="Hi! I'm your calling agent. What can I help you with?",
        alias="AGENT_GREETING",
    )
    # Optional stored-agent binding. Mutually exclusive with inline config.
    agent_id: str = Field(default="", alias="AGENT_ID")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    public_hostname: str = Field(default="", alias="PUBLIC_HOSTNAME")


settings = Settings()
