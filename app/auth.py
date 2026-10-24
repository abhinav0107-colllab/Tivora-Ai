"""Verifies the Supabase login token and finds the user's business on the server."""
from dataclasses import dataclass
import jwt
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session
from .db import get_db
from .models import Org, Member


@dataclass
class Ctx:
    user_id: str
    org_id: str
    email: str | None


def decode_token(token: str, s) -> dict:
    try:
        if s.supabase_jwks_url:
            key = jwt.PyJWKClient(s.supabase_jwks_url).get_signing_key_from_jwt(token).key
            algs = ["ES256", "RS256"]
        elif s.supabase_jwt_secret:
            key, algs = s.supabase_jwt_secret, ["HS256"]
        else:
            raise HTTPException(503, "Login is not configured on the server.")
        return jwt.decode(token, key, algorithms=algs, audience="authenticated")
    except HTTPException:
        raise
    except jwt.PyJWTError:
        raise HTTPException(401, "Your session is invalid or expired. Sign in again.")


def current_ctx(request: Request, authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Ctx:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Sign in to continue.")
    claims = decode_token(authorization[7:].strip(), request.app.state.settings)
    user_id = str(claims.get("sub") or "")
    if not user_id:
        raise HTTPException(401, "Your session is invalid. Sign in again.")
    email = claims.get("email")
    m = db.get(Member, user_id)
    if not m:  # first sign-in: create this person's business
        org = Org(name=(email or "My business").split("@")[0][:60], credits_paise=request.app.state.settings.starter_credits_paise)
        db.add(org)
        db.flush()
        m = Member(user_id=user_id, org_id=org.id, email=email)
        db.add(m)
        db.commit()
    return Ctx(user_id=user_id, org_id=m.org_id, email=m.email)
