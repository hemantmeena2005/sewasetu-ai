# Sewa Setu — Officer MCP Server & Review Workflow

The Officer Model Context Protocol (MCP) server allows an authorized block officer to use an independent AI assistant (such as Claude Desktop, ChatGPT, or custom agent runtimes) to inspect pending pension applications, review eligibility criteria, and submit approval or rejection decisions in strict accordance with Purvanchal state policy.

The existing PostgreSQL database and Flask web portal remain the authoritative system of record.

---

## 1. Connection & Transport Specification

- **Server Endpoint:** `http://localhost:8000/mcp/officer/` (or `${PUBLIC_BASE_URL}/mcp/officer/`)
- **Transport:** Remote Streamable HTTP (Official Model Context Protocol Specification)
- **Protocol Version:** `2024-11-05`
- **Supported Capabilities:** Tools, Resources, Prompts

### Connecting an Independent AI Assistant

To connect an MCP-compatible client (e.g., Claude Desktop, cursor, or custom Python/TypeScript client), configure the server entry as follows:

#### `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "sewasetu-officer": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-streamable-http",
        "http://localhost:8000/mcp/officer/"
      ]
    }
  }
}
```

Or connect directly via any Streamable HTTP client passing the HTTP header:
```http
Authorization: Bearer <officer_token>
```

---

## 2. Authentication & Authorization Architecture

- **Cryptographic Tokens:** Sessions are authenticated via RS256 JSON Web Tokens (JWT) signed using the portal's RSA key pair (`oauth_keys` table in PostgreSQL).
- **Identity Derivation:** Permissions and authorized blocks are strictly derived server-side from the token claims (`sub`, `role`, `scope`). Client-supplied block arguments are never trusted as proof of authorization.
- **Block-Level Isolation:** Block officers are strictly restricted to applications from their assigned administrative block (e.g., `officer_sonari` can only view and decide applications for the `Sonari` block). Attempts to access records across block boundaries are denied with a `403 Forbidden` error.
- **Role Enforcement:** Tokens with `role="citizen"` or invalid scopes are immediately rejected with `401 Unauthorized`.
- **Human Confirmation Safeguard:** Decisions require explicit confirmation (`officer_confirmed=true`). The AI assistant is forbidden from autonomously approving or rejecting applications without explicit human review.

---

## 3. Configuration & Environment Variables

| Variable | Description | Default / Example |
|---|---|---|
| `ADMIN_USERNAME` | Master administrator username | `admin` |
| `ADMIN_PASSWORD` | Departmental master password | Set via environment |
| `OFFICER_DEFAULT_PASSWORD` | Fallback departmental password for testing | Same as `ADMIN_PASSWORD` |
| `OFFICER_<BLOCK>_PASSWORD` | Block-specific officer password override | e.g. `OFFICER_SONARI_PASSWORD` |
| `PUBLIC_BASE_URL` | Base public URL for JWT issuer and discovery | `http://localhost:8000` |

---

## 4. Available MCP Tools

### 1. `authenticate_officer`
Authenticate departmental credentials and obtain an authorized session token.
- **Inputs:**
  - `username` (string, required): Officer departmental username (e.g. `officer_sonari`, `admin`).
  - `password` (string, required): Departmental password.
- **Outputs:**
  - `success` (boolean)
  - `officer_token` (string): Signed JWT bearer token.
  - `authorized_blocks` (array of strings): Jurisdictional blocks.
  - `expires_in_seconds` (integer): Token TTL (default: 28,800s / 8 hours).

---

### 2. `get_pending_applications`
Retrieve pending applications strictly within the officer's jurisdiction.
- **Inputs:**
  - `officer_token` (string, optional if sent via Authorization header)
  - `page` (integer, default: `1`)
  - `limit` (integer, default: `20`, max: `100`)
  - `sort_by` (string, default: `"submitted_at"`): Options: `"submitted_at"`, `"applicant_name"`, `"application_no"`, `"block"`.
  - `sort_order` (string, default: `"asc"`): Options: `"asc"`, `"desc"`.
  - `status_filter` (string, default: `"PENDING"`).
  - `date_from` (string, optional): Format `YYYY-MM-DD`.
  - `date_to` (string, optional): Format `YYYY-MM-DD`.
  - `block_filter` (string, optional): Must be within officer's authorized blocks.
- **Outputs:**
  - `total_count` (integer), `page` (integer), `total_pages` (integer).
  - `applications` (array): Each item includes `id`, `application_no`, `applicant_name`, `age`, `gender`, `village`, `block`, `submitted_at`, `status`, `days_pending`, `is_overdue`. Bank account numbers are omitted from list views.

---

### 3. `get_application_details`
Retrieve detailed application data for an authorized officer.
- **Inputs:**
  - `application_id` (integer or string, required): Application ID or `application_no` (e.g., `SSP260105NAMD`).
  - `officer_token` (string, optional).
- **Outputs:**
  - `application` (object):
    - Personal: `applicant_name`, `dob`, `age`, `gender`, `marital_status`, `village`, `block`.
    - Protected DBT Details: `bank_account_masked` (`XXXXXX1234`), `ifsc`.
    - Verification Status: `document` (`attached`, `filename`, `verified`). Raw file paths and public URLs are not exposed.
    - Lifecycle: `status`, `submitted_at`, `decided_at`, `decided_by`.
    - Audit: Complete chronological history of actions and notes.

---

### 4. `get_review_checklist`
Return structured checklist of statutory criteria and evidence.
- **Inputs:**
  - `application_id` (integer or string, required).
  - `officer_token` (string, optional).
- **Outputs:**
  - `overall_readiness`: `"READY_FOR_DECISION"` or `"DEFICIENT"`.
  - `is_statutorily_eligible`: `true` or `false`.
  - `checklist`: Array of items verifying:
    1. **Age Requirement (>= 60 years):** Calculated age vs threshold.
    2. **Administrative Jurisdiction:** Verified village and block.
    3. **Age Proof Documentation:** Attachment verified.
    4. **DBT Bank Account:** Format and IFSC verified.
    5. **SLA Compliance:** Elapsed time vs 15-day statutory deadline.
  - `outstanding_issues`: Array of strings describing any missing or deficient criteria.

---

### 5. `approve_application`
Approve an eligible pending application.
- **Inputs:**
  - `application_id` (integer or string, required).
  - `reason` (string, required): Departmental remarks and justification.
  - `officer_confirmed` (boolean, required): **Must be `true`**. Enforces human officer decision.
  - `officer_token` (string, optional).
- **Outputs:**
  - `success`: `true`
  - `application_no`: Approved application number.
  - `status`: `"APPROVED"`
  - `decided_by`: Authenticated officer username.
  - `decided_at`: ISO timestamp.
  - `reason`: Recorded audit justification.

---

### 6. `reject_application`
Reject an application with a mandatory recorded reason.
- **Inputs:**
  - `application_id` (integer or string, required).
  - `reason` (string, required): Mandatory statutory reason for rejection.
  - `officer_confirmed` (boolean, required): **Must be `true`**.
  - `officer_token` (string, optional).
- **Outputs:**
  - `success`: `true`
  - `application_no`: Application number.
  - `status`: `"REJECTED"`
  - `decided_by`: Authenticated officer username.
  - `reason`: Recorded rejection reason.

---

### 7. `get_decision_history`
Retrieve decision history and audit trails.
- **Inputs:**
  - `application_id` (integer or string, optional): When specified, retrieves full audit history for that specific application.
  - `limit` (integer, default: `50`).
  - `officer_token` (string, optional).
- **Outputs:**
  - `history`: Chronological array of decisions, timestamps, actors, and reasons.

---

## 5. Standard Error Handling

All tools return standard JSON error responses with meaningful descriptions:

| Error Type | Status Code / Message | Cause |
|---|---|---|
| **Unauthorized** | `"Unauthorized: Valid officer token required."` | Missing or expired token, or token has citizen role |
| **Forbidden** | `"Forbidden: Officer does not have permission to view application from block '...'"` | Attempting to access records from an unauthorized block |
| **Confirmation Required** | `"Action aborted: Explicit officer confirmation required (officer_confirmed must be true)."` | Automated agent called approve/reject without confirmation |
| **Mandatory Reason** | `"Rejection reason is mandatory according to departmental policy."` | Missing reason string in rejection request |
| **Invalid State** | `"Cannot decide application: current status is 'APPROVED'."` | Application has already been decided or deemed approved |
| **Underage Block** | `"Cannot approve application: applicant is 36 years of age (minimum required is 60)."` | Statutory minimum age violation enforced by backend |
