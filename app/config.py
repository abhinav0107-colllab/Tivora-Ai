"""Settings come from environment variables only. Never hard-code secrets."""
import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(v: str | None) -> bool:
    return str(v or "").lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./tivora.db"
    # Auth (Supabase). Use the JWT secret (HS256) or, for projects on asymmetric keys, the JWKS URL.
    supabase_jwt_secret: str = ""
    supabase_jwks_url: str = ""
    # Voice provider (Sarvam Voice Agents)
    sarvam_api_key: str = ""
    sarvam_org_id: str = ""
    sarvam_workspace_id: str = ""
    sarvam_connection_id: str = ""
    sarvam_agent_phone: str = ""
    # Public URL of this API, used to build the call-finished webhook URL
    public_base_url: str = "http://localhost:8000"
    webhook_token_secret: str = ""  # signs per-call provider webhook tokens
    admin_key: str = ""  # for adding credits by hand
    cors_origins: str = ""  # comma separated
    # Prices charged to customers, in paise per started minute (placeholders: set real prices)
    rate_standard_paise: int = 400
    rate_premium_paise: int = 600
    starter_credits_paise: int = 5000
    # Calling window (IST), TRAI allows promotional calls 9am-9pm
    call_window_start_hour: int = 9
    call_window_end_hour: int = 21
    outbound_per_minute_limit: int = 10
    webhook_retry_delays: tuple = (0, 5, 30)


@lru_cache
def get_settings() -> Settings:
    e = os.environ.get
    return Settings(
        database_url=e("DATABASE_URL", "sqlite:///./tivora.db"),
        supabase_jwt_secret=e("SUPABASE_JWT_SECRET", ""),
        supabase_jwks_url=e("SUPABASE_JWKS_URL", ""),
        sarvam_api_key=e("SARVAM_API_KEY", ""),
        sarvam_org_id=e("SARVAM_ORG_ID", ""),
        sarvam_workspace_id=e("SARVAM_WORKSPACE_ID", ""),
        sarvam_connection_id=e("SARVAM_CONNECTION_ID", ""),
        sarvam_agent_phone=e("SARVAM_AGENT_PHONE", ""),
        public_base_url=e("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        webhook_token_secret=e("WEBHOOK_TOKEN_SECRET", ""),
        admin_key=e("ADMIN_KEY", ""),
        cors_origins=e("CORS_ORIGINS", ""),
        rate_standard_paise=int(e("RATE_STANDARD_PAISE", "400")),
        rate_premium_paise=int(e("RATE_PREMIUM_PAISE", "600")),
    )
