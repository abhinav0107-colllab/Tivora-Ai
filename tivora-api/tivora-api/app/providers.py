"""Voice providers sit behind one interface so Tivora can switch provider later."""
from dataclasses import dataclass, field
from typing import Protocol
import httpx

SARVAM_LANG = {"te-en": "Telugu", "te": "Telugu", "hi-en": "Hindi", "hi": "Hindi", "ta": "Tamil", "kn": "Kannada",
               "ml": "Malayalam", "mr": "Marathi", "en": "English"}


class ProviderError(Exception):
    pass


@dataclass
class CompletionEvent:
    attempt_id: str
    status: str  # connected, no_answer, busy, failed
    duration_sec: float | None = None
    failure_reason: str | None = None
    variables: dict = field(default_factory=dict)
    transcript: list = field(default_factory=list)


class VoiceProvider(Protocol):
    name: str

    def start_outbound_call(self, *, agent, phone_e164: str, call_id: str, webhook_url: str) -> str: ...
    def parse_completion(self, payload: dict) -> CompletionEvent: ...


class SarvamProvider:
    name = "sarvam"

    def __init__(self, s, client: httpx.Client | None = None):
        self.s = s
        self.client = client or httpx.Client(timeout=20)

    def start_outbound_call(self, *, agent, phone_e164, call_id, webhook_url) -> str:
        s = self.s
        missing = [k for k in ("sarvam_api_key", "sarvam_org_id", "sarvam_workspace_id", "sarvam_connection_id", "sarvam_agent_phone") if not getattr(s, k)]
        if missing:
            raise ProviderError("Calling is not set up on the server yet.")
        if not agent.sarvam_app_id or not agent.sarvam_app_version:
            raise ProviderError("Link this employee to a Sarvam agent before calling.")
        url = f"https://apps.sarvam.ai/api/outbounds/v1/orgs/{s.sarvam_org_id}/workspaces/{s.sarvam_workspace_id}/outbounds"
        body = {
            "app_config": {
                "app_id": agent.sarvam_app_id,
                "app_version": agent.sarvam_app_version,
                "connection_config": {"connection_id": s.sarvam_connection_id, "agent_phone_number": s.sarvam_agent_phone},
                "agent_variables": {"business_name": agent.business, "agent_name": agent.name},
                "app_overrides": {"initial_bot_message": agent.greeting, "initial_language_name": SARVAM_LANG.get(agent.lang, "English")},
            },
            "user_config": {"user_phone_number": phone_e164},
            "webhook_config": {"url": webhook_url, "metadata": {"tivora_call_id": call_id}},
        }
        try:
            r = self.client.post(url, json=body, headers={"X-API-Key": s.sarvam_api_key, "Content-Type": "application/json"})
        except httpx.HTTPError:
            raise ProviderError("The voice provider could not be reached.")
        if r.status_code >= 400:
            raise ProviderError(f"The voice provider rejected the call (HTTP {r.status_code}).")
        attempt_id = (r.json() or {}).get("attempt_id")
        if not attempt_id:
            raise ProviderError("The voice provider did not confirm the call.")
        return attempt_id

    def parse_completion(self, payload: dict) -> CompletionEvent:
        status = payload.get("status")
        if status not in {"connected", "no_answer", "busy", "failed"} or not payload.get("attempt_id"):
            raise ValueError("Unrecognised webhook payload.")
        transcript = [{"role": "agent" if t.get("role") == "agent" else "caller", "text": str(t.get("en_text", ""))}
                      for t in (payload.get("interaction_transcript") or [])]
        return CompletionEvent(attempt_id=payload["attempt_id"], status=status, duration_sec=payload.get("duration"),
                               failure_reason=payload.get("failure_reason"), variables=payload.get("final_agent_variables") or {},
                               transcript=transcript)
