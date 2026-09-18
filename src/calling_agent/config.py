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

    # "module.path:callable" -- a callable taking this connection's query
    # parameters and returning an AgentDefinition. Called ONCE PER CONNECTION so
    # the agent it builds can be bound to that caller. Empty means the
    # restaurant, which is the whole of this repo's own behaviour. It exists so
    # another codebase can serve its own agent through this relay and this UI
    # without forking either.
    #
    # A RECONNECT IS A NEW CONNECTION, so the factory is called again -- and the
    # upstream session it rejoins still has the prompt and tool declarations of
    # the FIRST one. A factory whose answer varies between those two calls gets
    # the first agent's tool calls dispatched into the second agent's tools. The
    # parameters carry `resume` for exactly this: derive whatever must stay
    # stable across a reconnect from it, and a factory that ignores it must
    # return the same agent for the same parameters.
    agent_factory: str = Field(default="", alias="AGENT_FACTORY")

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

    # --- Database -------------------------------------------------------
    # The whole product's truth. Absent, the relay still runs (a consumer
    # serving its own agent through this transport needs no schema of ours),
    # but every booking tool answers that the book is unavailable rather than
    # inventing a table.
    database_url: str = Field(
        default="postgresql+psycopg://tableline@localhost:5432/tableline",
        alias="DATABASE_URL",
    )
    db_pool_size: int = Field(default=5, alias="DB_POOL_SIZE")
    db_max_overflow: int = Field(default=5, alias="DB_MAX_OVERFLOW")
    # Applied on startup. Off in production, where migrations are a deploy step.
    migrate_on_start: bool = Field(default=False, alias="MIGRATE_ON_START")

    # --- Tenancy --------------------------------------------------------
    # A transport that knows the dialled number resolves the tenant from it.
    # The browser transport does not have one, so it falls back to this slug.
    # It is a POINTER to a row, never business behaviour: nothing in the code
    # may branch on its value.
    default_business_slug: str = Field(default="", alias="DEFAULT_BUSINESS_SLUG")

    # --- Dashboard ------------------------------------------------------
    # Tokens are stored hashed with this pepper, so a database dump alone does
    # not open anybody's dashboard. Changing it invalidates every live link.
    token_pepper: str = Field(default="", alias="TOKEN_PEPPER")
    # Bind an unauthenticated dashboard to one business. Development only:
    # refused unless ALLOW_INSECURE_DASHBOARD is also set.
    dev_dashboard_business: str = Field(default="", alias="DEV_DASHBOARD_BUSINESS")
    allow_insecure_dashboard: bool = Field(default=False, alias="ALLOW_INSECURE_DASHBOARD")
    dashboard_base_url: str = Field(default="", alias="DASHBOARD_BASE_URL")

    # --- Jobs -----------------------------------------------------------
    # Expired holds must give their capacity back or the room fills with
    # ghosts. One minute, per PRD §7.
    sweep_interval_seconds: int = Field(default=60, alias="SWEEP_INTERVAL_SECONDS")
    run_jobs_in_process: bool = Field(default=True, alias="RUN_JOBS_IN_PROCESS")

    # --- Telephony ------------------------------------------------------
    twilio_account_sid: str = Field(default="", alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str = Field(default="", alias="TWILIO_AUTH_TOKEN")
    twilio_from_number: str = Field(default="", alias="TWILIO_FROM_NUMBER")
    # Refuse webhooks that Twilio did not sign. Only turn this off locally.
    verify_twilio_signature: bool = Field(default=True, alias="VERIFY_TWILIO_SIGNATURE")
    # sms provider: "twilio" sends, "log" records the message and sends nothing.
    sms_provider: str = Field(default="log", alias="SMS_PROVIDER")

    # --- Reading a photographed menu (PRD §14) --------------------------
    # Empty means no reader: the dashboard hides the button rather than
    # offering one that fails. Nothing a reader proposes is ever saved
    # without the owner confirming it -- see menu_reader.
    menu_reader: str = Field(default="", alias="MENU_READER")
    menu_reader_model: str = Field(default="", alias="MENU_READER_MODEL")
    menu_reader_timeout: float = Field(default=90.0, alias="MENU_READER_TIMEOUT")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    public_hostname: str = Field(default="", alias="PUBLIC_HOSTNAME")


settings = Settings()
