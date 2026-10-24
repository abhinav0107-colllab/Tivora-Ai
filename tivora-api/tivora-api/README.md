# Tivora API

FastAPI backend for Tivora AI: logins (Supabase), employees, consent, outbound calls through Sarvam Voice Agents,
call logs, leads, bookings, signed webhooks and usage.

## Run it on your laptop
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q                                   # 39 tests should pass
SUPABASE_JWT_SECRET=dev-secret-dev-secret-dev-secret-123 WEBHOOK_TOKEN_SECRET=anything \
  uvicorn app.main:app_factory --factory --reload     # http://localhost:8000/docs
```

## Deploy (follow in order)

### 1. Supabase (database + logins)
1. supabase.com → New project. Save the database password you choose.
2. Project Settings → Database → Connection string. Choose the **Session pooler** URI (works on Render). Put your password in place of `[YOUR-PASSWORD]`. This is `DATABASE_URL`.
3. Project Settings → API (or JWT Keys). Copy the **JWT secret** into `SUPABASE_JWT_SECRET`.
   If your project shows only asymmetric signing keys, set `SUPABASE_JWKS_URL` to `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` instead.
4. Authentication → Providers: keep Email on. Decide whether to require email confirmation.

### 2. GitHub
Create a private repo, upload this folder (do not upload `.env`).

### 3. Render (hosting)
1. render.com → New → Web Service → connect the repo. It reads `render.yaml`.
2. Fill the environment variables from `.env.example`. `WEBHOOK_TOKEN_SECRET` and `ADMIN_KEY` are generated for you.
3. Set `PUBLIC_BASE_URL` to your Render URL after the first deploy, then redeploy.
4. Open `https://YOUR-SERVICE.onrender.com/health`. It should show `{"ok":true}`.
5. In Supabase → SQL editor, run `supabase_rls.sql` once (the API creates the tables on first start).

Free Render services sleep when idle, and a sleeping service can miss Sarvam's call-finished webhook.
Use a paid instance, or a keep-alive ping, before real calls.

### 4. Sarvam
1. Create the agent in the Sarvam dashboard; note its **app id** and **version**.
2. Complete KYC, rent or connect a number, and note the **connection id** and number.
3. Put API key, org id, workspace id, connection id and number into the Render environment.
4. In Tivora, set `sarvam_app_id` and `sarvam_app_version` on the employee (PATCH /v1/agents/{id}).

## Safety rules built in
- The business is decided on the server from the login, never from the browser. Other businesses' records return 404.
- No outbound call without a recorded, unrevoked consent for that number, and only 9 AM to 9 PM IST.
- A call that fails to start is stored as failed and returns an error, never a success.
- Tool actions are validated and idempotent. A failed tool returns `ok: false`.
- Webhook targets must be public https addresses (blocks private-network targets). Deliveries are HMAC-signed, retried, and logged with redacted errors.
- Sarvam's published webhook has no signature, so Tivora puts a per-call secret token in the webhook URL.

## Not done yet
- DND/NCPR scrubbing and DLT template checks (needs your telecom provider's setup).
- Bulk campaigns, inbound deployments, billing invoices, database migrations (Alembic).
- The Supabase asymmetric-key (JWKS) login path is written but not tested against a real project.
- The Sarvam request shape follows their published docs but has not been run against their live API.
