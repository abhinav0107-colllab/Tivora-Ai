"""Signed delivery of call results to the customer's own server, with retries and a delivery log."""
import hashlib
import hmac
import json
import time
import httpx
from sqlalchemy.orm import Session
from .clock import utcnow
from .models import WebhookDelivery
from .validators import assert_public_https_url, redact_error


def sign(secret: str, ts: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


def queue_delivery(db: Session, org, event: str, payload: dict, call_id: str | None = None) -> WebhookDelivery | None:
    if not org.webhook_url or not org.webhook_secret:
        return None
    d = WebhookDelivery(org_id=org.id, call_id=call_id, event=event, url=org.webhook_url, payload=payload)
    db.add(d)
    db.commit()
    return d


def deliver(session_factory, delivery_id: str, secret: str, delays=(0, 5, 30), sleep=time.sleep, client: httpx.Client | None = None):
    client = client or httpx.Client(timeout=10, follow_redirects=False)
    with session_factory() as db:
        d = db.get(WebhookDelivery, delivery_id)
        if not d or d.status == "delivered":
            return
        body = json.dumps({"event": d.event, "delivery_id": d.id, "data": d.payload}, separators=(",", ":"), sort_keys=True)
        for i, wait in enumerate(delays):
            if wait:
                sleep(wait)
            ts = int(time.time())
            d.attempts = i + 1
            try:
                assert_public_https_url(d.url)  # re-check at send time
                r = client.post(d.url, content=body, headers={"Content-Type": "application/json", "X-Tivora-Event": d.event,
                                                              "X-Tivora-Signature": f"t={ts},v1={sign(secret, ts, body)}"})
                d.last_status_code = r.status_code
                if 200 <= r.status_code < 300:
                    d.status, d.last_error, d.delivered_at = "delivered", None, utcnow()
                    db.commit()
                    return
                d.last_error = f"Your server answered HTTP {r.status_code}."
            except (httpx.HTTPError, ValueError) as e:
                d.last_error = redact_error(e)
            d.status = "pending" if i < len(delays) - 1 else "failed"
            db.commit()
