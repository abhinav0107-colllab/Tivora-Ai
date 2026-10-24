import hashlib
import hmac
import math
import secrets
import time
from datetime import timedelta
from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import clock
from .auth import Ctx, current_ctx
from .config import Settings, get_settings
from .db import Base, get_db, make_session_factory
from .models import Agent, Booking, Call, Consent, IdempotencyKey, Lead, Org, WebhookDelivery
from .providers import ProviderError, SarvamProvider
from .schemas import AgentIn, AgentPatch, ConsentIn, CreditsIn, OutboundIn, TOOL_NAMES, ToolIn, WebhookIn
from .validators import assert_public_https_url, normalize_phone
from .webhooks import deliver, queue_delivery

OUTCOMES = {"booked", "interested", "call_back", "not_interested", "escalated", "incomplete"}
router = APIRouter()


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def owned(db: Session, model, obj_id: str, org_id: str):
    """Every lookup of a customer record goes through here. Wrong business means 404, so ids don't leak."""
    obj = db.get(model, obj_id)
    if not obj or obj.org_id != org_id:
        raise HTTPException(404, "Not found.")
    return obj


def rate_for(s: Settings, tier: str) -> int:
    return s.rate_premium_paise if tier == "premium" else s.rate_standard_paise


def call_token(s: Settings, call_id: str) -> str:
    return hmac.new(s.webhook_token_secret.encode(), call_id.encode(), hashlib.sha256).hexdigest()[:40]


def month_start():
    n = clock.utcnow()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def agent_spend(db: Session, agent_id: str) -> int:
    return db.scalar(select(func.coalesce(func.sum(Call.cost_paise), 0)).where(Call.agent_id == agent_id, Call.created_at >= month_start())) or 0


def agent_out(a: Agent, spent: int = 0):
    return {"id": a.id, "name": a.name, "business": a.business, "lang": a.lang, "direction": a.direction, "tier": a.tier,
            "greeting": a.greeting, "instructions": a.instructions, "tools": a.tools, "transfer_number": a.transfer_number,
            "spend_cap_paise": a.spend_cap_paise, "spent_this_month_paise": spent, "status": a.status,
            "sarvam_app_id": a.sarvam_app_id, "sarvam_app_version": a.sarvam_app_version, "created_at": iso(a.created_at)}


def call_out(c: Call, full: bool = False):
    d = {"id": c.id, "agent_id": c.agent_id, "direction": c.direction, "phone": c.phone, "status": c.status, "outcome": c.outcome,
         "duration_sec": c.duration_sec, "summary": c.summary, "cost_paise": c.cost_paise, "failure_reason": c.failure_reason,
         "created_at": iso(c.created_at), "completed_at": iso(c.completed_at)}
    if full:
        d.update(fields=c.fields, transcript=c.transcript)
    return d


# ---------- health / me ----------
@router.get("/health")
def health():
    return {"ok": True}


@router.get("/v1/me")
def me(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    org = db.get(Org, ctx.org_id)
    return {"user_id": ctx.user_id, "email": ctx.email, "org": {"id": org.id, "name": org.name, "credits_paise": org.credits_paise}}


# ---------- agents ----------
@router.get("/v1/agents")
def list_agents(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    rows = db.scalars(select(Agent).where(Agent.org_id == ctx.org_id).order_by(Agent.created_at.desc())).all()
    return [agent_out(a, agent_spend(db, a.id)) for a in rows]


@router.post("/v1/agents", status_code=201)
def create_agent(body: AgentIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    a = Agent(org_id=ctx.org_id, **body.model_dump())
    db.add(a)
    db.commit()
    return agent_out(a)


@router.get("/v1/agents/{agent_id}")
def get_agent(agent_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    a = owned(db, Agent, agent_id, ctx.org_id)
    return agent_out(a, agent_spend(db, a.id))


@router.patch("/v1/agents/{agent_id}")
def patch_agent(agent_id: str, body: AgentPatch, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    a = owned(db, Agent, agent_id, ctx.org_id)
    data = body.model_dump(exclude_unset=True)
    if "tools" in data and data["tools"] is not None:
        data["tools"] = {k: bool(data["tools"].get(k, False)) for k in TOOL_NAMES}
    for k, v in data.items():
        setattr(a, k, v)
    db.commit()
    return agent_out(a, agent_spend(db, a.id))


@router.post("/v1/agents/{agent_id}/duplicate", status_code=201)
def duplicate_agent(agent_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    a = owned(db, Agent, agent_id, ctx.org_id)
    n = Agent(org_id=ctx.org_id, name=(a.name + " (copy)")[:80], business=a.business, lang=a.lang, direction=a.direction, tier=a.tier,
              greeting=a.greeting, instructions=a.instructions, tools=dict(a.tools or {}), transfer_number=a.transfer_number,
              spend_cap_paise=a.spend_cap_paise, sarvam_app_id=a.sarvam_app_id, sarvam_app_version=a.sarvam_app_version)
    db.add(n)
    db.commit()
    return agent_out(n)


@router.post("/v1/agents/{agent_id}/{action}")
def archive_or_restore(agent_id: str, action: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    if action not in {"archive", "restore"}:
        raise HTTPException(404, "Not found.")
    a = owned(db, Agent, agent_id, ctx.org_id)
    a.status = "archived" if action == "archive" else "active"
    db.commit()
    return agent_out(a)


# ---------- consent ----------
@router.post("/v1/consents", status_code=201)
def add_consent(body: ConsentIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "Enter a valid 10-digit Indian mobile number.")
    c = Consent(org_id=ctx.org_id, phone=phone, source=body.source)
    db.add(c)
    db.commit()
    return {"id": c.id, "phone": c.phone, "source": c.source, "revoked": c.revoked, "created_at": iso(c.created_at)}


@router.get("/v1/consents")
def list_consents(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db), limit: int = 50, offset: int = 0):
    rows = db.scalars(select(Consent).where(Consent.org_id == ctx.org_id).order_by(Consent.created_at.desc()).limit(min(limit, 100)).offset(offset)).all()
    return [{"id": c.id, "phone": c.phone, "source": c.source, "revoked": c.revoked, "created_at": iso(c.created_at)} for c in rows]


@router.delete("/v1/consents/{consent_id}")
def revoke_consent(consent_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    c = owned(db, Consent, consent_id, ctx.org_id)
    c.revoked = True
    db.commit()
    return {"id": c.id, "revoked": True}


# ---------- calls ----------
@router.post("/v1/calls/outbound", status_code=201)
def start_outbound(body: OutboundIn, request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    st = request.app.state
    s: Settings = st.settings
    agent = owned(db, Agent, body.agent_id, ctx.org_id)
    if agent.status != "active":
        raise HTTPException(409, "This employee is archived. Restore it first.")
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "Enter a valid 10-digit Indian mobile number.")
    if not s.webhook_token_secret:
        raise HTTPException(503, "Calling is not set up on the server yet.")
    now = time.time()
    recent = [t for t in st.rate.get(ctx.user_id, []) if now - t < 60]
    if len(recent) >= s.outbound_per_minute_limit:
        raise HTTPException(429, "Too many calls started in a minute. Wait and try again.")
    h = clock.now_ist().hour
    if not (s.call_window_start_hour <= h < s.call_window_end_hour):
        raise HTTPException(403, f"Calls are only allowed between {s.call_window_start_hour}:00 and {s.call_window_end_hour}:00 IST.")
    ok = db.scalar(select(Consent.id).where(Consent.org_id == ctx.org_id, Consent.phone == phone, Consent.revoked.is_(False)).limit(1))
    if not ok:
        raise HTTPException(403, "No consent is recorded for this number. Add the person's consent first.")
    rate = rate_for(s, agent.tier)
    org = db.get(Org, ctx.org_id)
    if org.credits_paise < rate:
        raise HTTPException(402, "Not enough credits to place this call.")
    if agent_spend(db, agent.id) + rate > agent.spend_cap_paise:
        raise HTTPException(402, "This employee has reached its monthly spend limit.")
    st.rate[ctx.user_id] = recent + [now]
    call = Call(org_id=ctx.org_id, agent_id=agent.id, phone=phone, provider=st.provider.name)
    db.add(call)
    db.commit()
    url = f"{s.public_base_url}/v1/provider-webhooks/{st.provider.name}/{call.id}/{call_token(s, call.id)}"
    try:
        call.provider_attempt_id = st.provider.start_outbound_call(agent=agent, phone_e164=phone, call_id=call.id, webhook_url=url)
    except ProviderError as e:
        call.status, call.outcome, call.failure_reason, call.completed_at = "failed", "failed", str(e)[:300], clock.utcnow()
        db.commit()
        raise HTTPException(502, str(e))  # never report a call that did not start
    db.commit()
    return call_out(call)


@router.get("/v1/calls")
def list_calls(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db), agent_id: str | None = None,
               outcome: str | None = None, limit: int = 50, offset: int = 0):
    q = select(Call).where(Call.org_id == ctx.org_id)
    if agent_id:
        q = q.where(Call.agent_id == agent_id)
    if outcome:
        q = q.where(Call.outcome == outcome)
    rows = db.scalars(q.order_by(Call.created_at.desc()).limit(min(limit, 100)).offset(offset)).all()
    return [call_out(c) for c in rows]


@router.get("/v1/calls/{call_id}")
def get_call(call_id: str, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    return call_out(owned(db, Call, call_id, ctx.org_id), full=True)


@router.get("/v1/leads")
def list_leads(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db), limit: int = 50, offset: int = 0):
    rows = db.scalars(select(Lead).where(Lead.org_id == ctx.org_id).order_by(Lead.created_at.desc()).limit(min(limit, 100)).offset(offset)).all()
    return [{"id": r.id, "call_id": r.call_id, "name": r.name, "phone": r.phone, "notes": r.notes, "created_at": iso(r.created_at)} for r in rows]


@router.get("/v1/bookings")
def list_bookings(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db), limit: int = 50, offset: int = 0):
    rows = db.scalars(select(Booking).where(Booking.org_id == ctx.org_id).order_by(Booking.created_at.desc()).limit(min(limit, 100)).offset(offset)).all()
    return [{"id": r.id, "call_id": r.call_id, "name": r.name, "when": r.when, "purpose": r.purpose, "created_at": iso(r.created_at)} for r in rows]


@router.get("/v1/usage")
def usage(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    org = db.get(Org, ctx.org_id)
    rows = db.execute(select(func.count(Call.id), func.coalesce(func.sum(Call.cost_paise), 0), func.coalesce(func.sum(Call.duration_sec), 0))
                      .where(Call.org_id == ctx.org_id, Call.created_at >= month_start())).one()
    return {"credits_paise": org.credits_paise, "month": {"calls": rows[0], "cost_paise": int(rows[1]), "minutes": round(float(rows[2]) / 60, 2)}}


# ---------- provider webhook (call finished) ----------
def derive_outcome(status: str, variables: dict, has_booking: bool, has_lead: bool) -> str:
    if status != "connected":
        return status
    if has_booking:
        return "booked"
    d = str(variables.get("disposition", "")).strip().lower().replace(" ", "_").replace("-", "_")
    if d in OUTCOMES:
        return d
    return "interested" if has_lead else "incomplete"


@router.post("/v1/provider-webhooks/{provider}/{call_id}/{token}")
def provider_webhook(provider: str, call_id: str, token: str, request: Request, bg: BackgroundTasks, payload: dict = Body(...), db: Session = Depends(get_db)):
    st = request.app.state
    s: Settings = st.settings
    if not s.webhook_token_secret or provider != st.provider.name or not hmac.compare_digest(token, call_token(s, call_id)):
        raise HTTPException(403, "Forbidden.")
    call = db.get(Call, call_id)
    if not call:
        raise HTTPException(404, "Not found.")
    try:
        ev = st.provider.parse_completion(payload)
    except ValueError:
        raise HTTPException(422, "Unrecognised payload.")
    if call.provider_attempt_id and ev.attempt_id != call.provider_attempt_id:
        raise HTTPException(409, "Attempt does not match this call.")
    if call.completed_at:
        return {"ok": True, "duplicate": True}
    call.provider_attempt_id = ev.attempt_id
    call.status = ev.status
    call.duration_sec = ev.duration_sec
    call.transcript = ev.transcript
    call.fields = {str(k)[:60]: str(v)[:300] for k, v in (ev.variables or {}).items()}
    call.failure_reason = (ev.failure_reason or "")[:300] or None
    agent = db.get(Agent, call.agent_id)
    if ev.status == "connected":
        minutes = max(1, math.ceil((ev.duration_sec or 0) / 60))
        call.cost_paise = minutes * rate_for(s, agent.tier)
    has_booking = db.scalar(select(Booking.id).where(Booking.call_id == call.id).limit(1)) is not None
    has_lead = db.scalar(select(Lead.id).where(Lead.call_id == call.id).limit(1)) is not None
    call.outcome = derive_outcome(ev.status, ev.variables or {}, has_booking, has_lead)
    call.completed_at = clock.utcnow()
    org = db.get(Org, call.org_id)
    org.credits_paise = max(0, org.credits_paise - call.cost_paise)
    db.commit()
    d = queue_delivery(db, org, "call.completed", {"call_id": call.id, "agent_id": call.agent_id, "status": call.status, "outcome": call.outcome,
                                                   "duration_sec": call.duration_sec, "fields": call.fields, "cost_paise": call.cost_paise}, call.id)
    if d:
        bg.add_task(deliver, st.SessionLocal, d.id, org.webhook_secret, s.webhook_retry_delays)
    return {"ok": True}


# ---------- tools the agent calls during a conversation ----------
def _fail(msg: str, code: int = 422):
    return JSONResponse({"ok": False, "message": msg}, status_code=code)


@router.post("/v1/org/tool-key")
def rotate_tool_key(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    key = "tvk_" + secrets.token_urlsafe(32)
    db.get(Org, ctx.org_id).tool_key_hash = hashlib.sha256(key.encode()).hexdigest()
    db.commit()
    return {"tool_key": key, "note": "Copy this now. It is shown once."}


@router.post("/v1/tools/{tool}")
def run_tool(tool: str, body: ToolIn, x_tool_key: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not x_tool_key:
        raise HTTPException(401, "Missing tool key.")
    org = db.scalar(select(Org).where(Org.tool_key_hash == hashlib.sha256(x_tool_key.encode()).hexdigest()))
    if not org:
        raise HTTPException(401, "Invalid tool key.")
    if tool not in TOOL_NAMES:
        raise HTTPException(404, "Unknown tool.")
    prev = db.scalar(select(IdempotencyKey).where(IdempotencyKey.org_id == org.id, IdempotencyKey.key == body.idempotency_key))
    if prev:
        return JSONResponse({**prev.response, "replayed": True}, status_code=200)
    call, agent = None, None
    if body.call_id:
        call = owned(db, Call, body.call_id, org.id)
        agent = db.get(Agent, call.agent_id)
        if not (agent.tools or {}).get(tool):
            return _fail("This tool is turned off for this employee.", 403)
    a = body.args or {}
    name = str(a.get("name", "")).strip()
    if tool == "save_lead":
        phone = normalize_phone(a.get("phone"))
        if len(name) < 2:
            return _fail("Caller name is missing.")
        if not phone:
            return _fail("Phone number is missing or not a valid 10-digit Indian mobile number.")
        db.add(Lead(org_id=org.id, call_id=call.id if call else None, name=name[:120], phone=phone, notes=str(a.get("notes", ""))[:2000]))
        res = {"ok": True, "message": "Lead saved."}
    elif tool == "book_appointment":
        when = str(a.get("when", "")).strip()
        if len(name) < 2:
            return _fail("Name for the booking is missing.")
        if len(when) < 3:
            return _fail("Day and time for the booking is missing.")
        db.add(Booking(org_id=org.id, call_id=call.id if call else None, name=name[:120], when=when[:120], purpose=str(a.get("purpose", ""))[:2000]))
        res = {"ok": True, "message": "Booking saved."}
    else:  # transfer_to_human
        if not agent or not agent.transfer_number:
            return _fail("No transfer number is set, so the call can't be handed over.")
        res = {"ok": True, "message": "Hand over requested.", "transfer_number": agent.transfer_number}
    db.add(IdempotencyKey(org_id=org.id, key=body.idempotency_key, tool=tool, response=res))
    db.commit()
    return res


# ---------- customer webhook settings ----------
@router.get("/v1/webhook")
def get_webhook(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    org = db.get(Org, ctx.org_id)
    return {"url": org.webhook_url, "has_secret": bool(org.webhook_secret)}


@router.put("/v1/webhook")
def put_webhook(body: WebhookIn, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    try:
        assert_public_https_url(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    org = db.get(Org, ctx.org_id)
    org.webhook_url = body.url
    out = {"url": org.webhook_url}
    if not org.webhook_secret or body.rotate_secret:
        org.webhook_secret = "whsec_" + secrets.token_hex(24)
        out["secret"] = org.webhook_secret  # shown this one time
    db.commit()
    return out


@router.post("/v1/webhook/test")
def test_webhook(request: Request, ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db)):
    org = db.get(Org, ctx.org_id)
    d = queue_delivery(db, org, "test", {"message": "Hello from Tivora"})
    if not d:
        raise HTTPException(409, "Save a webhook URL first.")
    deliver(request.app.state.SessionLocal, d.id, org.webhook_secret, (0, 0, 0), sleep=lambda s: None)
    db.refresh(d)
    return {"id": d.id, "status": d.status, "attempts": d.attempts, "last_status_code": d.last_status_code, "last_error": d.last_error}


@router.get("/v1/webhook/deliveries")
def deliveries(ctx: Ctx = Depends(current_ctx), db: Session = Depends(get_db), limit: int = 30):
    rows = db.scalars(select(WebhookDelivery).where(WebhookDelivery.org_id == ctx.org_id).order_by(WebhookDelivery.created_at.desc()).limit(min(limit, 100))).all()
    return [{"id": r.id, "event": r.event, "status": r.status, "attempts": r.attempts, "last_status_code": r.last_status_code,
             "last_error": r.last_error, "created_at": iso(r.created_at)} for r in rows]


# ---------- admin ----------
@router.post("/v1/admin/orgs/{org_id}/credits")
def add_credits(org_id: str, body: CreditsIn, request: Request, x_admin_key: str | None = Header(default=None), db: Session = Depends(get_db)):
    key = request.app.state.settings.admin_key
    if not key:
        raise HTTPException(404, "Not found.")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, key):
        raise HTTPException(403, "Forbidden.")
    org = db.get(Org, org_id)
    if not org:
        raise HTTPException(404, "Not found.")
    org.credits_paise += body.paise
    db.commit()
    return {"org_id": org.id, "credits_paise": org.credits_paise}


def create_app(settings: Settings | None = None, provider=None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="Tivora API", version="0.1.0")
    engine, SessionLocal = make_session_factory(s.database_url)
    Base.metadata.create_all(engine)
    app.state.settings, app.state.SessionLocal, app.state.engine = s, SessionLocal, engine
    app.state.provider = provider or SarvamProvider(s)
    app.state.rate = {}
    origins = [o.strip() for o in s.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)
    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
