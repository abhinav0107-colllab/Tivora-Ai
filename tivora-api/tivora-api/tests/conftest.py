import time
import jwt
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.providers import CompletionEvent, ProviderError, SarvamProvider

SECRET = "test-jwt-secret-with-enough-length-1234567890"


class FakeProvider:
    name = "sarvam"

    def __init__(self):
        self.calls, self.fail = [], False
        self._real = SarvamProvider(Settings())

    def start_outbound_call(self, *, agent, phone_e164, call_id, webhook_url):
        if self.fail:
            raise ProviderError("The voice provider could not be reached.")
        self.calls.append({"phone": phone_e164, "call_id": call_id, "url": webhook_url})
        return "attempt-" + call_id

    def parse_completion(self, payload):
        return self._real.parse_completion(payload)


def token(user="u1", email="a@x.com"):
    return jwt.encode({"sub": user, "email": email, "aud": "authenticated", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")


def H(user="u1", email=None):
    return {"Authorization": "Bearer " + token(user, email or f"{user}@x.com")}


AGENT = {"name": "Meera", "business": "Nest Realty", "lang": "hi-en", "direction": "Outbound", "greeting": "नमस्ते!",
         "instructions": "Qualify leads.", "tools": {"save_lead": True, "book_appointment": True}, "transfer_number": "",
         "sarvam_app_id": "app-1", "sarvam_app_version": 1}


@pytest.fixture
def env(tmp_path, monkeypatch):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db", supabase_jwt_secret=SECRET, webhook_token_secret="wh-secret",
                 public_base_url="https://api.test", admin_key="adm", webhook_retry_delays=(0, 0, 0))
    prov = FakeProvider()
    app = create_app(s, prov)
    from app import clock
    from datetime import datetime
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2026, 10, 22, 11, 0, tzinfo=clock.IST))  # inside calling window
    monkeypatch.setattr("app.validators.resolve_host", lambda h: ["93.184.216.34"])
    return TestClient(app), prov, app


@pytest.fixture
def client(env):
    return env[0]


def make_agent(c, user="u1", **over):
    r = c.post("/v1/agents", json={**AGENT, **over}, headers=H(user))
    assert r.status_code == 201, r.text
    return r.json()
