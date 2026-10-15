# Sewa Setu — OpenStack Production Deployment Guide

This guide provides authoritative instructions for provisioning, configuring, and operating **Sewa Setu** on OpenStack cloud infrastructure.

---

## 1. Architecture & Service Overview

Sewa Setu runs on OpenStack with private networking, persistent block storage, hardened container orchestration, and isolated MCP endpoints:

```
                                  [ OpenStack Floating IP / DNS ]
                                                │
                                    (Ports 80 & 443 / TLS)
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │      Nginx Reverse Proxy & SSL Offload       │
                         │    (HSTS, Anti-Cache, Rate Limiting, SSE)    │
                         └──────────────────────┬───────────────────────┘
                                                │ (Internal Port 8000)
                                                ▼
                         ┌──────────────────────────────────────────────┐
                         │      Unified ASGI Service (main:app)         │
                         │    Gunicorn + Uvicorn Async Workers (4x)     │
                         ├──────────────────────┬───────────────────────┤
                         │ /                    │ /mcp/citizen          │ /mcp/officer
                         │ Citizen Web Portal   │ Citizen MCP Server    │ Officer MCP Server
                         └──────────┬───────────┴───────────┬───────────┘
                                    │                       │
               ┌────────────────────┴───────────────────────┴────────────────────┐
               │ Shared Business Logic & Concurrency Layer (app/services.py)     │
               └──────────┬─────────────────────────┬───────────────────────────┘
                          │                         │
                          ▼                         ▼
            ┌───────────────────────────┐  ┌────────────────────────────────────┐
            │   PostgreSQL 15 Cluster   │  │ RTPS 15-Day Deemed Approval Worker │
            │ (Internal Port 5432 Only) │  │  Persistent Daemon (app/scripts)   │
            └─────────────┬─────────────┘  └────────────────────────────────────┘
                          │
                          ▼
            ┌───────────────────────────┐
            │ Cinder Block Storage (50G)│
            │  /var/sewasetu/pgdata     │
            │  /var/sewasetu/uploads    │
            └───────────────────────────┘
```

### Services Deployed
1. **Citizen Pension Web Portal (`/`)**: WSGI Flask portal with WCAG AA compliance, multi-lingual support (English & Telugu), anti-cache headers, and OTP verification.
2. **Citizen MCP Server (`/mcp/citizen`)**: Streamable HTTP Server-Sent Events (SSE) endpoint exposing 7 citizen application tools for compatible external AI assistants.
3. **Officer MCP Server (`/mcp/officer`)**: Authenticated Streamable HTTP SSE endpoint exposing 6 officer decision & review tools with block-level data isolation.
4. **PostgreSQL 15 Database (`db:5432`)**: Persistent relational database stored on an OpenStack Cinder block volume. Deliberately kept private from the public network.
5. **RTPS Deemed Approval Daemon (`scheduler`)**: Persistent background worker running `python scripts/deemed_approval.py --daemon --interval 300` enforcing the statutory 15-day approval deadline under Section 5 of the Right to Public Services Act.
6. **Nginx Reverse Proxy (`proxy:80,443`)**: TLS termination (TLSv1.2/1.3), rate limiting, and zero-buffering proxying for MCP streaming.

---

## 2. OpenStack Prerequisites & Required Permissions

### OpenStack Services Required
- **Nova (Compute)**: Minimum `m1.medium` flavor (2 vCPU, 4GB RAM, 20GB root disk).
- **Neutron (Networking)**: Private network, subnet, router connected to public external network, and floating IP allocation.
- **Cinder (Block Storage)**: 50GB persistent volume (`high-iops` or standard).
- **Glance (Image Service)**: Ubuntu 22.04 LTS or Debian 12 base image.
- **Keystone (Identity)**: Project member permissions to create compute, network, and storage resources.
- **Barbican (Key Manager - Optional)**: Secure storage of production JWT and database secrets.
- **Swift (Object Storage - Optional)**: Nightly database backup archival.

---

## 3. Infrastructure Provisioning Instructions

### Option A: OpenStack Heat (Recommended)
1. Ensure your OpenStack environment variables are loaded:
   ```bash
   source openrc.sh
   ```
2. Deploy the stack using the provided Heat Orchestration Template:
   ```bash
   openstack stack create -t deploy/openstack/heat-sewasetu.yaml \
     --parameter key_name=my-ssh-key \
     --parameter public_network=public \
     --parameter storage_volume_size=50 \
     --parameter admin_password=ChangeMeProductionSecret2026 \
     --parameter jwt_secret_key=$(openssl rand -hex 32) \
     sewasetu-production
   ```
3. Monitor stack creation until complete:
   ```bash
   openstack stack show sewasetu-production
   openstack stack output show sewasetu-production citizen_portal_url
   ```

### Option B: Terraform OpenStack Provider
1. Initialize Terraform in `deploy/openstack/`:
   ```bash
   cd deploy/openstack
   terraform init
   ```
2. Apply the configuration:
   ```bash
   terraform apply \
     -var="keypair_name=my-ssh-key" \
     -var="public_network_id=<EXTERNAL_NET_UUID>"
   ```

---

## 4. Environment Variables Configuration

Copy `.env.example` to `.env.production` on the production server:
```bash
cp .env.example .env.production
chmod 600 .env.production
```

| Variable | Description | Production Example |
| :--- | :--- | :--- |
| `POSTGRES_DB` | PostgreSQL database name | `sewasetu` |
| `POSTGRES_USER` | PostgreSQL user | `sewasetu` |
| `POSTGRES_PASSWORD` | PostgreSQL password (min 32 chars) | `openssl rand -base64 32` |
| `DB_HOST` | Database container or host | `db` |
| `DB_PORT` | Database port | `5432` |
| `SECRET_KEY` | Flask session encryption key | `openssl rand -hex 32` |
| `JWT_SECRET_KEY` | MCP token signing secret | `openssl rand -hex 32` |
| `ADMIN_USERNAME` | Admin portal username | `admin` |
| `ADMIN_PASSWORD` | Departmental master password | `StrongSecretPassphrase2026!` |
| `PUBLIC_BASE_URL` | Canonical public HTTPS URL | `https://sewasetu.purvanchal.gov.in` |
| `UPLOAD_DIR` | Private document storage path | `/var/sewasetu/uploads` |
| `LOG_DIR` | Structured log storage path | `/var/log/sewasetu` |
| `SLA_DAYS` | Statutory approval period | `15` |
| `SCHEME_DEADLINE` | Application intake cutoff | `2026-10-31 23:59` |

---

## 5. Build and Deployment Commands

### On the Target OpenStack Compute Instance
```bash
# 1. Clone repository
git clone https://github.com/purvanchal-gov/sewasetu-ai.git /opt/sewasetu
cd /opt/sewasetu

# 2. Configure production environment
cp .env.example .env.production
nano .env.production

# 3. Mount persistent storage
mkdir -p /var/sewasetu/pgdata /var/sewasetu/uploads /var/sewasetu/backups /var/log/sewasetu
chmod 700 /var/sewasetu/uploads /var/sewasetu/pgdata

# 4. Start production services
docker compose -f docker-compose.prod.yml up --build -d

# 5. Apply database migrations
docker compose -f docker-compose.prod.yml exec -T app python /app/scripts/migrate.py

# 6. Run production verification smoke test suite
docker compose -f docker-compose.prod.yml exec -T app python /app/scripts/verify_production.py --base-url http://localhost:8000
```

---

## 6. MCP Endpoints & Independent AI Assistant Connection

Both MCP servers implement the official Model Context Protocol over **Streamable HTTP (Server-Sent Events)**:

### 1. Citizen MCP Server
- **Endpoint URL**: `https://<DOMAIN_OR_IP>/mcp/citizen/`
- **Authentication**: Stateless sessions initiated via `start_application` tool and validated with SMS OTP (`verify_mobile_otp`).
- **Tools Exposed**:
  - `get_scheme_information`: Authoritative eligibility criteria and pension rules.
  - `get_supported_blocks`: Initial blocks enabled for AI assistance.
  - `start_application`: Generates secure session and initiates OTP dispatch.
  - `verify_mobile_otp`: Validates 6-digit OTP code.
  - `validate_application`: Server-side schema and business rule validation.
  - `upload_age_proof`: Encrypted document upload with magic byte verification.
  - `submit_application`: Final submission requiring explicit applicant consent.
  - `get_application_status`: Real-time status lookup by application number.

### 2. Officer MCP Server
- **Endpoint URL**: `https://<DOMAIN_OR_IP>/mcp/officer/`
- **Authentication**: HTTP `Authorization: Bearer <officer_token>` header or `officer_token` parameter in tool calls. Issued via `authenticate_officer`.
- **Tools Exposed**:
  - `authenticate_officer`: Authenticates departmental officer credentials.
  - `get_pending_applications`: Retrieves pending applications within authorized block.
  - `get_application_details`: Retrieves application data with masked bank numbers.
  - `get_review_checklist`: Authoritative compliance checklist.
  - `approve_application`: Records approval (requires `officer_confirmed=True`).
  - `reject_application`: Records rejection (requires valid non-empty reason).
  - `get_decision_history`: Immutable block decision audit history.

---

## 7. Health Checks & Monitoring

The system exposes unauthenticated liveness and readiness endpoints:

| Endpoint | Probe Type | Description |
| :--- | :--- | :--- |
| `GET /healthz` | Liveness | Lightweight HTTP 200 response indicating process is running. |
| `GET /readyz` | Readiness | Deep health check validating PostgreSQL connection and upload storage writability. |
| `GET /api/health` | Readiness | Alias to `/readyz` for Kubernetes/Octavia health monitors. |

### Operational Logs
- Application structured logs: `/var/log/sewasetu/sewasetu-app.log`
- Background deemed approval logs: `/var/log/sewasetu/cron.log`
- Nginx access & security logs: `/var/log/nginx/access.log`

---

## 8. Backup & Disaster Recovery Procedures

### Automated Database Backup
Run manual backup or configure via cron:
```bash
/opt/sewasetu/scripts/backup_db.sh
```
- Backups are saved to `/var/sewasetu/backups/sewasetu_backup_<TIMESTAMP>.sql.gz`
- SHA-256 checksums are saved alongside every archive.
- Backups older than 14 days are automatically pruned.

### Database Restoration
```bash
/opt/sewasetu/scripts/restore_db.sh /var/sewasetu/backups/sewasetu_backup_20261015_120000.sql.gz
```

---

## 9. Current Environment Status & Blockers

During Phase 8 implementation:
- **Local Sandbox Verification**: Successfully built and tested all components, persistent storage, health probes (`/healthz`, `/readyz`), and smoke verification suite (`verify_production.py`: 15/15 passed).
- **OpenStack Cloud Access**: Direct OpenStack credentials (`OS_AUTH_URL`, `OS_USERNAME`, `clouds.yaml`) and CLI are **not provisioned in this local development environment**.
- **Ready for Deployment**: All deployment automation (Heat template `heat-sewasetu.yaml`, Terraform `main.tf`, `cloud-init.yaml`, `docker-compose.prod.yml`, `nginx.conf`, `migrate.py`, and `backup_db.sh`) is fully prepared and validated for immediate deployment on any target OpenStack tenant.
