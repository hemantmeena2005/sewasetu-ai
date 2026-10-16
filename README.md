# Sewa Setu — Old Age Pension Portal & AI Assistant

Department of Social Welfare, Government of Purvanchal. Maintained in-house.

[![Tests](https://img.shields.io/badge/tests-63%20passed%2C%200%20failed-success.svg)](#automated-test-suite--evaluations)
[![SLA](https://img.shields.io/badge/statutory%20SLA-15--day%20deemed%20approval-blue.svg)](#statutory-15-day-deemed-approval)
[![Security](https://img.shields.io/badge/security-IDOR%20%26%20prompt%20injection%20shielded-brightgreen.svg)](#security--privacy)

---

## 🌐 Live Production Deployment

- **Public Portal URL:** [https://sewasetu-ai.onrender.com](https://sewasetu-ai.onrender.com)
- **Public Citizen MCP Endpoint:** `https://sewasetu-ai.onrender.com/mcp/citizen`
- **Public Officer MCP Endpoint:** `https://sewasetu-ai.onrender.com/mcp/officer`
- **Evaluation Dashboard:** [https://sewasetu-ai.onrender.com/evaluation](https://sewasetu-ai.onrender.com/evaluation) (or `/evals`)
- **Departmental Officer Login:** [https://sewasetu-ai.onrender.com/admin](https://sewasetu-ai.onrender.com/admin) (`admin` / `admin123`)
- **Status / Login Portal:** [https://sewasetu-ai.onrender.com/status](https://sewasetu-ai.onrender.com/status)

---

## 🚀 Running Locally

```bash
ADMIN_PASSWORD=admin123 docker compose up --build
```

- **Citizen Web Portal:** `http://localhost:8000`
- **Citizen Status / Login:** `http://localhost:8000/status` (Login via Mobile OTP or Application Number + DOB `DDMMYYYY`)
- **Departmental Officer Login:** `http://localhost:8000/admin` (Username: `admin`, Password: `ADMIN_PASSWORD`)
- **Interactive Evaluation Console:** `http://localhost:8000/evaluation`
- **Simulated SMS Gateway Console:** `http://localhost:8000/__gateway/` — OTPs and notifications appear here in real time.
- **PostgreSQL Database:** Seeded automatically with demo data on first boot.

---

## ✨ Key Features & Capabilities

### 1. Dual Model Context Protocol (MCP) Streamable Servers
- **Citizen MCP (`/mcp/citizen`):** Enables AI agents to discover scheme guidelines, dispatch & verify mobile OTPs, perform structured pre-validation, upload age-proof documents (Base64), and submit applications.
  - *Tools:* `get_scheme_information`, `get_supported_blocks`, `start_application`, `verify_mobile_otp`, `validate_application`, `upload_age_proof`, `submit_application`, `get_application_status`.
- **Officer MCP (`/mcp/officer`):** Enables authorized departmental assistants to inspect pending queues with strict block isolation, run eligibility checklists, and record human-confirmed approvals/rejections with audit logging.
  - *Tools:* `authenticate_officer`, `get_pending_applications`, `get_application_details`, `get_review_checklist`, `approve_application`, `reject_application`, `get_decision_history`.
  - *Reference:* [docs/OFFICER_MCP.md](docs/OFFICER_MCP.md)

### 2. Full-Page Interactive Evaluation Console (`/evaluation`)
- Independent, full-screen telemetry dashboard powered by **Chart.js** (no portal navbar interference).
- **Interactive Bar Chart:** Test suite distribution and 100% pass rates across all 7 suites (63 tests).
- **Interactive Donut Chart:** Live PostgreSQL application pipeline (`APPROVED`, `DEEMED_APPROVED`, `PENDING`, `REJECTED`).
- **Real-Time Audit Stream:** Searchable, filterable client-side table showing the latest 30 tamper-evident database transactions.
- **JSON Telemetry Export:** One-click download/clipboard copy of complete evaluation metrics.

### 3. Mobile-First Responsive Design & Modern UI
- **Mobile Hamburger Toggle Bar:** Ultra-compact 56px government top bar with smooth expandable slide-down navigation drawer.
- **Chrome Tab Favicon:** Sharp official State Emblem icon in vector SVG, multi-resolution PNGs (`16x16`, `32x32`), Apple Touch Icon (`180x180`), and `/favicon.ico`.
- **LG-Style Floating AI Chatbot:** Spacious 420px floating assistant card with quick-action prompt pills, clear close buttons (`×`), and seamless iframe integration without double scrollbars.
- **Accessible & Multilingual:** Complete English, हिन्दी, and తెలుగు localization with iOS input zoom prevention (`16px` font thresholds) and responsive table horizontal scrolling.

### 4. Statutory 15-Day Deemed Approval Engine
- Enforces statutory SLA under the Purvanchal Right to Public Services (RTPS) Act 2024.
- Runs concurrency-safe batch processing via `SELECT ... FOR UPDATE SKIP LOCKED` (`app/scripts/deemed_approval.py`).
- Automatic age re-validation ensures ineligible applicants are never deemed approved.

### 5. Security & Privacy Safeguards
- **Zero-Data Shared Kiosk Isolation:** Strict `Cache-Control: no-store, no-cache, must-revalidate` headers and automatic session purging on logout.
- **IDOR Protection:** Cross-citizen dossier snooping blocked.
- **Malicious Upload Blocking:** Neutralizes path traversal and executable extensions (`.exe`, `.sh`, `.php`, `.py`).
- **Prompt Injection Defense:** Strict message sanitization and boundary filtering.

---

## 🧪 Automated Test Suite & Evaluations

The project includes **63 automated test cases** covering every layer of the architecture:

| Test Suite | Tests | Status | Focus Area |
|---|:---:|:---:|---|
| `test_shared_services.py` | 11 | PASSED | Validations, age thresholds (60+), advisory locks, application sequence generator |
| `test_citizen_mcp.py` | 6 | PASSED | Streamable HTTP transport, scheme discovery, OTP dispatch/verify, document upload |
| `test_citizen_ux_privacy.py` | 10 | PASSED | Trilingual i18n, kiosk anti-cache headers, consecutive session purge, login by OTP |
| `test_officer_mcp.py` | 13 | PASSED | Block-level tenancy isolation, review checklist, human confirmation guard, audit trail |
| `test_automatic_approval.py` | 4 | PASSED | 15-day statutory SLA, SKIP LOCKED concurrency, DBT transfer idempotency |
| `test_e2e_security_resilience.py` | 15 | PASSED | IDOR shields, path traversal, prompt injection filtering, SMS gateway failover |
| `test_final_acceptance.py` | 4 | PASSED | Complete multi-agent citizen journey through officer review & deemed approval |
| **Total** | **63** | **100%** | **All Tests Passing (Duration: ~3.1s)** |

To run the full evaluation suite inside Docker:
```bash
docker exec sewasetu-ai-app-1 python run_tests.py
```

---

## 🏛️ Architecture & Project Structure

```
sewasetu-ai/
├── app/
│   ├── app.py                  # Core Flask portal, OAuth 2.1 server & evaluation routes
│   ├── mcp_citizen.py          # FastAPI Citizen Model Context Protocol (MCP) server
│   ├── mcp_officer.py          # FastAPI Officer Model Context Protocol (MCP) server
│   ├── services.py             # Shared business logic, DB advisory locks & validations
│   ├── i18n.py                 # Localization dictionaries (English, Hindi, Telugu)
│   ├── static/                 # Brand assets, favicon.svg, PNG icons & chatbot avatars
│   ├── templates/              # Jinja2 templates (evaluation, assistant, admin, etc.)
│   └── scripts/                # Cron scripts (15-day deemed approval batch runner)
├── tests/                      # 63 automated unit, integration, and security tests
├── deploy/                     # OpenStack Heat, Terraform, Cloud-Init & Nginx configurations
├── docs/                       # Officer MCP & OpenStack deployment architecture guides
├── render.yaml                 # Render cloud infrastructure-as-code specification
└── run_tests.py                # Automated evaluation runner & test_results.json generator
```

---

## 🔒 OAuth 2.1 Authorization Server

Sewa Setu implements an RFC-compliant OAuth 2.1 server for third-party client integrations:
- **Discovery Endpoint:** `/.well-known/oauth-authorization-server` (RFC 8414)
- **Dynamic Registration:** `/oauth/register` (RFC 7591)
- **Authorize & Token:** `/oauth/authorize` (PKCE S256) & `/oauth/token` (RS256 JWTs)
- **Token Revocation:** `/oauth/revoke` (RFC 7009)
- **JWKS Endpoint:** `/.well-known/jwks.json`
