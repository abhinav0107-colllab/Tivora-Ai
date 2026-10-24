import hashlib
import json
from datetime import datetime
import jwt
import pytest
from sqlalchemy import select
from app import clock
from app.models import Call, Org, WebhookDelivery
from app.webhooks import sign
from .conftest import H, SECRET, make_agent, token


# ---------- auth ----------
def test_requires_login(client):
    assert client.get("/v1/agents").status_code == 401
    assert client.get("/v1/agents", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_rejects_token_signed_with_wrong_secret(client):
    bad = jwt.encode({"sub": "x", "aud": "authenticated"}, "another-secret-another-secret-12345", algorithm="HS256")
    assert client.get("/v1/me", headers={"Authorization": "Bearer " + bad}).status_code == 401


def test_first_login_creates_business_with_starter_credits(client):
    r = client.get("/v1/me", headers=H("new"))
    assert r.status_code == 200 and r.json()["org"]["credits_paise"] == 5000


# ---------- tenant isolation ----------
def test_other_business_cannot_see_or_change_agent(client):
    a = make_agent(client, "u1")
    assert client.get(f"/v1/agents/{a['id']}", headers=H("u2")).status_code == 404
    assert client.patch(f"/v1/agents/{a['id']}", json={"name": "Hacked"}, headers=H("u2")).status_code == 404
    assert client.post(f"/v1/agents/{a['id']}/archive", headers=H("u2")).status_code == 404
    assert client.get("/v1/agents", headers=H("u2")).json() == []
    assert client.get(f"/v1/agents/{a['id']}", headers=H("u1")).json()["name"] == "Meera"


def test_browser_cannot_choose_tenant(client):
    r = client.post("/v1/agents", json={**__import__("tests.conftest", fromlist=["AGENT"]).AGENT, "org_id": "someone-else"}, headers=H("u1"))
    assert r.status_code == 201
    assert client.get("/v1/agents", headers=H("u2")).json() == []


# ---------- agents ----------
def test_agent_crud_duplicate_archive(client):
    a = make_agent(client)
    assert client.patch(f"/v1/agents/{a['id']}", json={"greeting": "Hello!"}, headers=H()).json()["greeting"] == "Hello!"
    d = client.post(f"/v1/agents/{a['id']}/duplicate", headers=H()).json()
    assert d["name"] == "Meera (copy)" and d["id"] != a["id"]
    assert client.post(f"/v1/agents/{a['id']}/archive", headers=H()).json()["status"] == "archived"
    assert client.post(f"/v1/agents/{a['id']}/restore", headers=H()).json()["status"] == "active"
    assert client.post("/v1/agents", json={"name": ""}, headers=H()).status_code == 422


def test_unknown_tools_are_dropped(client):
    a = make_agent(client, tools={"save_lead": True, "delete_everything": True})
    assert set(a["tools"]) == {"save_lead", "book_appointment", "transfer_to_human"}


# ---------- outbound calls ----------
def consent(c, phone="9876543210", user="u1"):
    r = c.post("/v1/consents", json={"phone": phone, "source": "website enquiry form"}, headers=H(user))
    assert r.status_code == 201
    return r.json()


def test_call_needs_consent(env):
    c, prov, _ = env
    a = make_agent(c)
    r = c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H())
    assert r.status_code == 403 and "consent" in r.json()["detail"].lower()
    assert prov.calls == []


def test_revoked_consent_blocks_call(env):
    c, prov, _ = env
    a = make_agent(c)
    cid = consent(c)["id"]
    c.delete(f"/v1/consents/{cid}", headers=H())
    assert c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H()).status_code == 403


def test_consent_is_per_business(env):
    c, _, _ = env
    a = make_agent(c, "u2")
    consent(c, user="u1")  # u1's consent must not unlock u2's call
    assert c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H("u2")).status_code == 403


def test_call_blocked_outside_window(env, monkeypatch):
    c, prov, _ = env
    a = make_agent(c)
    consent(c)
    monkeypatch.setattr(clock, "now_ist", lambda: datetime(2026, 10, 22, 22, 30, tzinfo=clock.IST))
    r = c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H())
    assert r.status_code == 403 and "IST" in r.json()["detail"] and prov.calls == []


def test_invalid_phone_rejected(env):
    c, _, _ = env
    a = make_agent(c)
    assert c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "12345"}, headers=H()).status_code == 422


def test_successful_call_start(env):
    c, prov, _ = env
    a = make_agent(c)
    consent(c)
    r = c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "+91 98765 43210"}, headers=H())
    assert r.status_code == 201 and r.json()["status"] == "queued"
    assert prov.calls[0]["phone"] == "+919876543210"
    assert prov.calls[0]["url"].startswith("https://api.test/v1/provider-webhooks/sarvam/")


def test_provider_failure_is_never_reported_as_success(env):
    c, prov, _ = env
    a = make_agent(c)
    consent(c)
    prov.fail = True
    r = c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H())
    assert r.status_code == 502
    calls = c.get("/v1/calls", headers=H()).json()
    assert calls[0]["status"] == "failed" and calls[0]["cost_paise"] == 0


def test_no_credits_blocks_call(env):
    c, prov, _ = env
    a = make_agent(c)
    consent(c)
    with c.app.state.SessionLocal() as db:
        db.query(Org).update({"credits_paise": 100})
        db.commit()
    assert c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H()).status_code == 402
    assert prov.calls == []


def test_spend_cap_blocks_call(env):
    c, prov, _ = env
    a = make_agent(c, spend_cap_paise=300)  # below one minute at 4 rupees
    consent(c)
    assert c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H()).status_code == 402


def test_rate_limit(env):
    c, prov, app = env
    app.state.settings = app.state.settings.__class__(**{**app.state.settings.__dict__, "outbound_per_minute_limit": 2})
    a = make_agent(c)
    consent(c)
    codes = [c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H()).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


# ---------- provider webhook ----------
def start_call(env):
    c, prov, _ = env
    a = make_agent(c)
    consent(c)
    r = c.post("/v1/calls/outbound", json={"agent_id": a["id"], "phone": "9876543210"}, headers=H())
    return r.json()["id"], prov.calls[-1]["url"].replace("https://api.test", "")


CONNECTED = {"attempt_id": None, "status": "connected", "channel_info": {}, "duration": 75.4, "interaction_id": "i1", "failure_reason": None,
             "final_agent_variables": {"disposition": "call back", "customer_name": "Ravi"}, "webhook_config": None,
             "interaction_transcript": [{"role": "agent", "en_text": "Hello"}, {"role": "user", "en_text": "Hi"}]}


def test_webhook_rejects_bad_token(env):
    c = env[0]
    call_id, path = start_call(env)
    bad = path.rsplit("/", 1)[0] + "/" + "0" * 40
    assert c.post(bad, json={**CONNECTED, "attempt_id": "attempt-" + call_id}).status_code == 403


def test_webhook_completes_call_and_charges(env):
    c = env[0]
    call_id, path = start_call(env)
    r = c.post(path, json={**CONNECTED, "attempt_id": "attempt-" + call_id})
    assert r.status_code == 200
    call = c.get(f"/v1/calls/{call_id}", headers=H()).json()
    assert call["status"] == "connected" and call["outcome"] == "call_back"
    assert call["cost_paise"] == 800  # 75s rounds up to 2 minutes at 4 rupees
    assert call["transcript"][1] == {"role": "caller", "text": "Hi"}
    assert c.get("/v1/me", headers=H()).json()["org"]["credits_paise"] == 5000 - 800


def test_duplicate_webhook_does_not_charge_twice(env):
    c = env[0]
    call_id, path = start_call(env)
    body = {**CONNECTED, "attempt_id": "attempt-" + call_id}
    c.post(path, json=body)
    assert c.post(path, json=body).json()["duplicate"] is True
    assert c.get("/v1/me", headers=H()).json()["org"]["credits_paise"] == 4200


def test_webhook_wrong_attempt_rejected(env):
    c = env[0]
    call_id, path = start_call(env)
    assert c.post(path, json={**CONNECTED, "attempt_id": "someone-elses"}).status_code == 409


def test_unanswered_call_costs_nothing(env):
    c = env[0]
    call_id, path = start_call(env)
    c.post(path, json={"attempt_id": "attempt-" + call_id, "status": "no_answer", "channel_info": {}, "duration": None})
    call = c.get(f"/v1/calls/{call_id}", headers=H()).json()
    assert call["outcome"] == "no_answer" and call["cost_paise"] == 0


def test_ndnc_failure_reason_is_kept(env):
    c = env[0]
    call_id, path = start_call(env)
    c.post(path, json={"attempt_id": "attempt-" + call_id, "status": "failed", "channel_info": {}, "failure_reason": "exotel: Phone number is registered under TRAI NDNC"})
    assert "NDNC" in c.get(f"/v1/calls/{call_id}", headers=H()).json()["failure_reason"]


# ---------- tools ----------
def tool_key(c, user="u1"):
    return c.post("/v1/org/tool-key", headers=H(user)).json()["tool_key"]


def call_with_agent(env):
    c = env[0]
    call_id, _ = start_call(env)
    return call_id, tool_key(c)


def test_tool_requires_key(client):
    assert client.post("/v1/tools/save_lead", json={"idempotency_key": "abcdef", "args": {}}).status_code == 401
    assert client.post("/v1/tools/save_lead", json={"idempotency_key": "abcdef", "args": {}}, headers={"X-Tool-Key": "tvk_nope"}).status_code == 401


def test_save_lead_validates_and_never_fakes_success(env):
    c = env[0]
    call_id, key = call_with_agent(env)
    hk = {"X-Tool-Key": key}
    r = c.post("/v1/tools/save_lead", json={"idempotency_key": "k-0001", "call_id": call_id, "args": {"name": "Ravi", "phone": "12345"}}, headers=hk)
    assert r.status_code == 422 and r.json()["ok"] is False
    assert c.get("/v1/leads", headers=H()).json() == []
    r = c.post("/v1/tools/save_lead", json={"idempotency_key": "k-0002", "call_id": call_id, "args": {"name": "Ravi", "phone": "98765 43210", "notes": "2BHK"}}, headers=hk)
    assert r.status_code == 200 and r.json()["ok"] is True
    leads = c.get("/v1/leads", headers=H()).json()
    assert len(leads) == 1 and leads[0]["phone"] == "+919876543210"


def test_tool_is_idempotent(env):
    c = env[0]
    call_id, key = call_with_agent(env)
    body = {"idempotency_key": "same-key-1", "call_id": call_id, "args": {"name": "Asha", "when": "Saturday 11 AM"}}
    r1 = c.post("/v1/tools/book_appointment", json=body, headers={"X-Tool-Key": key})
    r2 = c.post("/v1/tools/book_appointment", json=body, headers={"X-Tool-Key": key})
    assert r1.status_code == r2.status_code == 200 and r2.json()["replayed"] is True
    assert len(c.get("/v1/bookings", headers=H()).json()) == 1


def test_disabled_tool_is_refused(env):
    c = env[0]
    call_id, key = call_with_agent(env)
    r = c.post("/v1/tools/transfer_to_human", json={"idempotency_key": "tr-0001", "call_id": call_id, "args": {}}, headers={"X-Tool-Key": key})
    assert r.status_code == 403 and r.json()["ok"] is False


def test_tool_cannot_touch_another_businesses_call(env):
    c = env[0]
    call_id, _ = call_with_agent(env)
    other_key = tool_key(c, "u2")
    r = c.post("/v1/tools/save_lead", json={"idempotency_key": "x-000001", "call_id": call_id, "args": {"name": "Eve", "phone": "9876543210"}}, headers={"X-Tool-Key": other_key})
    assert r.status_code == 404


def test_booking_marks_outcome_booked(env):
    c = env[0]
    call_id, key = call_with_agent(env)
    c.post("/v1/tools/book_appointment", json={"idempotency_key": "bk-00001", "call_id": call_id, "args": {"name": "Asha", "when": "Sat 11 AM"}}, headers={"X-Tool-Key": key})
    _, path = None, None
    with c.app.state.SessionLocal() as db:
        attempt = db.get(Call, call_id).provider_attempt_id
    url = c.app.state.provider.calls[-1]["url"].replace("https://api.test", "")
    c.post(url, json={**CONNECTED, "attempt_id": attempt, "final_agent_variables": {}})
    assert c.get(f"/v1/calls/{call_id}", headers=H()).json()["outcome"] == "booked"


# ---------- customer webhooks ----------
def test_webhook_url_must_be_public_https(client):
    assert client.put("/v1/webhook", json={"url": "http://example.com/h"}, headers=H()).status_code == 422


def test_webhook_blocks_private_addresses(env, monkeypatch):
    c = env[0]
    monkeypatch.setattr("app.validators.resolve_host", lambda h: ["169.254.169.254"])
    r = c.put("/v1/webhook", json={"url": "https://metadata.internal/x"}, headers=H())
    assert r.status_code == 422 and "private" in r.json()["detail"]
    monkeypatch.setattr("app.validators.resolve_host", lambda h: ["10.0.0.5"])
    assert c.put("/v1/webhook", json={"url": "https://intranet.example/x"}, headers=H()).status_code == 422


def test_secret_shown_once(client):
    r1 = client.put("/v1/webhook", json={"url": "https://example.com/h"}, headers=H()).json()
    assert r1["secret"].startswith("whsec_")
    r2 = client.put("/v1/webhook", json={"url": "https://example.com/h2"}, headers=H()).json()
    assert "secret" not in r2
    assert client.get("/v1/webhook", headers=H()).json()["has_secret"] is True


def test_webhook_test_delivers_signed_request(env, monkeypatch):
    c = env[0]
    secret = c.put("/v1/webhook", json={"url": "https://example.com/h"}, headers=H()).json()["secret"]
    seen = {}

    class R:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        def post(self, url, content, headers):
            seen.update(url=url, body=content, headers=headers)
            return R()

    monkeypatch.setattr("app.webhooks.httpx.Client", FakeClient)
    res = c.post("/v1/webhook/test", headers=H()).json()
    assert res["status"] == "delivered" and res["attempts"] == 1
    t, v1 = [p.split("=")[1] for p in seen["headers"]["X-Tivora-Signature"].split(",")]
    assert v1 == sign(secret, int(t), seen["body"])


def test_webhook_retries_then_fails_with_redacted_error(env, monkeypatch):
    c = env[0]
    c.put("/v1/webhook", json={"url": "https://example.com/h?token=SECRET"}, headers=H())

    class R:
        status_code = 500

    class FakeClient:
        def __init__(self, *a, **k): pass
        def post(self, *a, **k): return R()

    monkeypatch.setattr("app.webhooks.httpx.Client", FakeClient)
    res = c.post("/v1/webhook/test", headers=H()).json()
    assert res["status"] == "failed" and res["attempts"] == 3 and "SECRET" not in json.dumps(res)


def test_call_completion_queues_customer_webhook(env, monkeypatch):
    c = env[0]
    c.put("/v1/webhook", json={"url": "https://example.com/h"}, headers=H())
    sent = []

    class R:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **k): pass
        def post(self, url, content, headers):
            sent.append(json.loads(content))
            return R()

    monkeypatch.setattr("app.webhooks.httpx.Client", FakeClient)
    call_id, path = start_call(env)
    c.post(path, json={**CONNECTED, "attempt_id": "attempt-" + call_id})
    assert sent and sent[0]["event"] == "call.completed" and sent[0]["data"]["call_id"] == call_id
    assert c.get("/v1/webhook/deliveries", headers=H()).json()[0]["status"] == "delivered"


# ---------- usage + admin ----------
def test_usage_summary(env):
    c = env[0]
    call_id, path = start_call(env)
    c.post(path, json={**CONNECTED, "attempt_id": "attempt-" + call_id})
    u = c.get("/v1/usage", headers=H()).json()
    assert u["month"]["calls"] == 1 and u["month"]["cost_paise"] == 800 and u["month"]["minutes"] == 1.26


def test_admin_credits_need_key(client):
    org_id = client.get("/v1/me", headers=H()).json()["org"]["id"]
    assert client.post(f"/v1/admin/orgs/{org_id}/credits", json={"paise": 1000}).status_code == 403
    r = client.post(f"/v1/admin/orgs/{org_id}/credits", json={"paise": 1000}, headers={"X-Admin-Key": "adm"})
    assert r.json()["credits_paise"] == 6000


# ---------- sarvam adapter (request shape follows Sarvam's published docs) ----------
def test_sarvam_request_shape():
    import httpx
    from app.config import Settings
    from app.providers import SarvamProvider
    sent = {}

    def handler(req: httpx.Request):
        sent.update(url=str(req.url), key=req.headers["x-api-key"], body=json.loads(req.content))
        return httpx.Response(200, json={"attempt_id": "abc"})

    s = Settings(sarvam_api_key="k", sarvam_org_id="o1", sarvam_workspace_id="w1", sarvam_connection_id="conn", sarvam_agent_phone="+918000000000")
    p = SarvamProvider(s, httpx.Client(transport=httpx.MockTransport(handler)))

    class A:
        sarvam_app_id, sarvam_app_version, business, name, greeting, lang = "app-1", 3, "Nest", "Meera", "Hi", "te-en"

    assert p.start_outbound_call(agent=A, phone_e164="+919876543210", call_id="c1", webhook_url="https://x/y") == "abc"
    assert sent["url"] == "https://apps.sarvam.ai/api/outbounds/v1/orgs/o1/workspaces/w1/outbounds" and sent["key"] == "k"
    b = sent["body"]
    assert b["app_config"]["app_id"] == "app-1" and b["app_config"]["app_version"] == 3
    assert b["app_config"]["connection_config"] == {"connection_id": "conn", "agent_phone_number": "+918000000000"}
    assert b["app_config"]["app_overrides"]["initial_language_name"] == "Telugu"
    assert b["user_config"] == {"user_phone_number": "+919876543210"} and b["webhook_config"]["metadata"] == {"tivora_call_id": "c1"}


def test_sarvam_error_never_leaks_key():
    import httpx
    from app.config import Settings
    from app.providers import ProviderError, SarvamProvider
    s = Settings(sarvam_api_key="SUPER-SECRET", sarvam_org_id="o", sarvam_workspace_id="w", sarvam_connection_id="c", sarvam_agent_phone="+91800")
    p = SarvamProvider(s, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="bad key SUPER-SECRET"))))

    class A:
        sarvam_app_id, sarvam_app_version, business, name, greeting, lang = "a", 1, "b", "n", "g", "en"

    with pytest.raises(ProviderError) as e:
        p.start_outbound_call(agent=A, phone_e164="+919876543210", call_id="c", webhook_url="https://x")
    assert "SUPER-SECRET" not in str(e.value)
