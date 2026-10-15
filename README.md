# Sewa Setu — Old Age Pension Portal

Department of Social Welfare, Government of Purvanchal. Maintained in-house.

## Running

```
ADMIN_PASSWORD=pick-something docker compose up --build
```

- Portal: http://localhost:8000
- Status portal: http://localhost:8000/status (mobile number + password; the
  password is the applicant's date of birth as DDMMYYYY)
- Departmental login: http://localhost:8000/admin (user `admin`, password from
  `ADMIN_PASSWORD`)
- SMS gateway console (simulated telecom): http://localhost:8000/__gateway/ —
  OTPs and notifications appear here once "delivered" (1–4 s carrier latency).
  The gateway's inbox is also readable as JSON at
  `/__gateway/api/messages?to=<mobile>`.
- Database: Postgres, loaded from `seed/seed.sql` on first boot (~100k applications).

## Deploying on a server

Set `PUBLIC_BASE_URL` to the address people will use (for example
`https://sewasetu.example.in`). It is stamped into sign-in tokens and the discovery documents
described below, so a wrong value means other applications cannot find the sign-in page. Put
the portal behind HTTPS; some applications refuse to sign in over plain HTTP.

```
PUBLIC_BASE_URL=https://sewasetu.example.in ADMIN_PASSWORD=pick-something docker compose up --build -d
```

## What the portal does

Citizen flow: mobile → OTP → personal details → address → bank details →
age-proof upload → preview and declaration → application number. A nightly
job (`scripts/deemed_approval.py`, via cron in the `scheduler` container)
deems approved any application still pending after the 15-day statutory SLA.

Rules enforced at submission, in `validate_application()` in `app/app.py`:
applicant is 60 years of age or above; required fields and an age-proof
document are present; the scheme window is open; and the mobile number holds
no other active (pending / approved / deemed-approved) application. A pending
application can be withdrawn from the status portal, after which the same
mobile may apply again.

Departmental users see a dashboard, list and filter applications, approve or
reject pending ones, and see the audit trail per application.

## Layout

```
app/          Flask application, templates, config, nightly job
seed/         seed.sql — production data snapshot
smsgw/        simulated SMS gateway
```

## Letting other applications act for a citizen or an officer

Sewa Setu is also an **OAuth 2.1 authorization server**. A third-party application can act
for a citizen (scope `citizen`) or for a block officer (scope `officer`) once that person
signs in on the portal and approves it. Citizens sign in by mobile and OTP, officers with the
departmental login. The application never handles the OTP or the password.

- Discovery: `/.well-known/oauth-authorization-server` (RFC 8414)
- Registration: `/oauth/register` (RFC 7591), or a client ID metadata document URL as `client_id`
- Authorization: `/oauth/authorize` (authorization code with PKCE S256; the `resource` the
  application asks for becomes the token's audience, RFC 8707)
- Tokens: `/oauth/token`. Access tokens are RS256 JWTs. A sign-in is a **session with an
  absolute end**: 2 hours for a citizen, 8 hours for an officer, counted from the consent.
  Access tokens are short (20 / 30 minutes) and refresh tokens rotate within the session but
  never extend it; after it ends the person signs in again. Portals are used on shared phones
  and kiosks.
- Sign-out: `/oauth/revoke` (RFC 7009). People can also end any sign-in from the status
  portal (**Connected applications**) or, for officers, the admin dashboard.
- Keys: `/.well-known/jwks.json`

A service that receives one of these tokens verifies it with the JWKS, checks `iss` is this
portal, checks `aud` is its own canonical URL, and reads `scope`, `role` and `sub`
(`citizen:<mobile>` or `officer:<username>`).

Long-lived **program tokens** for unattended use (role officer) are issued by an
administrator at `/admin/tokens`.

---

## Model Context Protocol (MCP) Integration

Sewa Setu exposes two official MCP servers using **Streamable HTTP (Server-Sent Events)**:

1. **Citizen MCP Server (`/mcp/citizen`)**:
   - Allows external AI assistants to discover authoritative scheme details, verify mobile OTPs, and submit pension applications with explicit consent.
   - Tools: `get_scheme_information`, `get_supported_blocks`, `start_application`, `verify_mobile_otp`, `validate_application`, `upload_age_proof`, `submit_application`, `get_application_status`.
2. **Officer MCP Server (`/mcp/officer`)**:
   - Allows authorized block officers to review pending applications, inspect eligibility checklists, and record decisions with strict block-level isolation and mandatory confirmation (`officer_confirmed=True`).
   - Tools: `authenticate_officer`, `get_pending_applications`, `get_application_details`, `get_review_checklist`, `approve_application`, `reject_application`, `get_decision_history`.
   - Documentation: [docs/OFFICER_MCP.md](docs/OFFICER_MCP.md).

---

## OpenStack Production Deployment

For complete infrastructure specifications, Heat Orchestration Templates, Terraform modules, and operational guides, see:
👉 **[docs/OPENSTACK_DEPLOYMENT.md](docs/OPENSTACK_DEPLOYMENT.md)**

### Key Deployment Artifacts
- **OpenStack Heat Template**: `deploy/openstack/heat-sewasetu.yaml`
- **Terraform OpenStack Module**: `deploy/openstack/main.tf` and `variables.tf`
- **Cloud-Init Bootstrapper**: `deploy/openstack/cloud-init.yaml`
- **Hardened Nginx Reverse Proxy**: `deploy/nginx/nginx.conf`
- **Production Compose**: `docker-compose.prod.yml`
- **Environment Template**: `.env.example`

### Health & Monitoring Endpoints
- **Liveness Probe**: `GET /healthz` (HTTP 200 `{"status": "healthy"}`)
- **Readiness Probe**: `GET /readyz` (Deep PostgreSQL connectivity & storage write check)
- **Evaluation Dashboard**: `GET /admin/evaluation` (Live security evaluation and test metrics)

### Operations & Maintenance
```bash
# Apply database migrations
python scripts/migrate.py

# Create database backup with SHA-256 checksum
bash scripts/backup_db.sh

# Restore database from backup
bash scripts/restore_db.sh /var/sewasetu/backups/<backup_file>.sql.gz

# Run production smoke test suite
python scripts/verify_production.py --base-url http://localhost:8000

# Run full regression test suite (58/58 tests)
python run_tests.py
```
