from typing import Literal
from pydantic import BaseModel, Field, field_validator

Lang = Literal["te-en", "hi-en", "te", "hi", "ta", "kn", "ml", "mr", "en"]
TOOL_NAMES = ("save_lead", "book_appointment", "transfer_to_human")


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    business: str = Field(min_length=1, max_length=120)
    lang: Lang
    direction: Literal["Outbound", "Inbound"] = "Outbound"
    tier: Literal["standard", "premium"] = "standard"
    greeting: str = Field(min_length=1, max_length=600)
    instructions: str = Field(min_length=1, max_length=6000)
    tools: dict[str, bool] = {}
    transfer_number: str = Field(default="", max_length=20)
    spend_cap_paise: int = Field(default=50000, ge=0, le=10_000_000)
    sarvam_app_id: str | None = Field(default=None, max_length=120)
    sarvam_app_version: int | None = Field(default=None, ge=1)

    @field_validator("tools")
    @classmethod
    def only_known_tools(cls, v):
        return {k: bool(v.get(k, False)) for k in TOOL_NAMES}


class AgentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    business: str | None = Field(default=None, min_length=1, max_length=120)
    lang: Lang | None = None
    direction: Literal["Outbound", "Inbound"] | None = None
    tier: Literal["standard", "premium"] | None = None
    greeting: str | None = Field(default=None, min_length=1, max_length=600)
    instructions: str | None = Field(default=None, min_length=1, max_length=6000)
    tools: dict[str, bool] | None = None
    transfer_number: str | None = Field(default=None, max_length=20)
    spend_cap_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    sarvam_app_id: str | None = Field(default=None, max_length=120)
    sarvam_app_version: int | None = Field(default=None, ge=1)


class ConsentIn(BaseModel):
    phone: str
    source: str = Field(min_length=3, max_length=200, description="How the person agreed, e.g. 'website enquiry form, 12 Oct'")


class OutboundIn(BaseModel):
    agent_id: str
    phone: str


class ToolIn(BaseModel):
    idempotency_key: str = Field(min_length=6, max_length=120)
    call_id: str | None = None
    args: dict = {}


class WebhookIn(BaseModel):
    url: str = Field(max_length=500)
    rotate_secret: bool = False


class CreditsIn(BaseModel):
    paise: int = Field(gt=0, le=100_000_000)
