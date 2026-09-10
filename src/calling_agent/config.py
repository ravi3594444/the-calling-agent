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

    # "arjun" is a multilingual Hindi/Hinglish voice that code-switches with
    # English automatically -- the right default for Indian callers. "ivy",
    # the API's own example, is lighter and reads as more synthetic.
    agent_voice: str = Field(default="arjun", alias="AGENT_VOICE")
    agent_greeting: str = Field(
        default="Thanks for calling The Copper Kettle, this is Meera. How can I help?",
        alias="AGENT_GREETING",
    )
    # Optional stored-agent binding. Mutually exclusive with inline config.
    agent_id: str = Field(default="", alias="AGENT_ID")

    # --- Turn detection: the main lever on perceived response speed ---
    # min_silence is how long the caller must stop talking before the agent
    # decides they are done. Lowering it makes replies feel faster but risks
    # cutting people off mid-sentence.
    turn_detection: bool = Field(default=True, alias="AGENT_TURN_DETECTION")
    vad_threshold: float = Field(default=0.5, alias="AGENT_VAD_THRESHOLD")
    min_silence_ms: int = Field(default=320, alias="AGENT_MIN_SILENCE_MS")
    max_silence_ms: int = Field(default=1500, alias="AGENT_MAX_SILENCE_MS")
    allow_interruptions: bool = Field(default=True, alias="AGENT_ALLOW_INTERRUPTIONS")

    # --- Restaurant ---
    restaurant_name: str = Field(default="The Copper Kettle", alias="RESTAURANT_NAME")
    restaurant_cuisine: str = Field(
        default="modern North Indian food", alias="RESTAURANT_CUISINE"
    )
    restaurant_address: str = Field(default="", alias="RESTAURANT_ADDRESS")
    max_party_size: int = Field(default=12, alias="RESTAURANT_MAX_PARTY")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    public_hostname: str = Field(default="", alias="PUBLIC_HOSTNAME")


settings = Settings()
