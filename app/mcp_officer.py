# -*- coding: utf-8 -*-
"""
Sewa Setu - Officer MCP Server (Model Context Protocol).

Exposes authoritative, secure tools for authorized departmental block officers
and their AI assistants to inspect pending pension applications, review eligibility
evidence, and record approval/rejection decisions according to legal rules.

Transport: Remote Streamable HTTP (official MCP SDK).
Security: Cryptographic RS256 JWT tokens, server-side block-level isolation,
audit trail logging, and explicit human confirmation safeguards.
"""

import os
import re
import time
import secrets
import contextvars
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Union, Tuple

import jwt
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import services
import oauth

# Initialize officer MCP server
officer_mcp = MCPServer("sewasetu-officer")

OFFICER_AUDIENCE = "mcp:officer"
OFFICER_SESSION_TTL = 28800  # 8 hours (standard working day)

# ContextVar for HTTP Authorization header support in ASGI middleware
current_officer_token: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_officer_token", default=None
)


# ---------------------------------------------------------------------------
# Token & Identity Helpers
# ---------------------------------------------------------------------------

def create_officer_session_token(officer_username: str, role: str = "officer", ttl: int = OFFICER_SESSION_TTL) -> str:
    """Generate a signed RS256 JWT session token for an authenticated officer."""
    kid, priv = oauth.signing_key()
    now = int(time.time())
    payload = {
        "iss": oauth.ISSUER,
        "sub": f"officer:{officer_username}",
        "aud": OFFICER_AUDIENCE,
        "scope": "officer",
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_hex(16)
    }
    return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid})


def verify_officer_session(token: str) -> Optional[Dict[str, Any]]:
    """
    Verify officer session token.
    Extracts identity strictly from token claims and resolves authorized blocks server-side.
    Returns officer info dict or None if invalid / expired / unauthorized.
    """
    if not token:
        return None
    try:
        t = token.strip()
        if t.lower().startswith("bearer "):
            t = t[7:].strip()

        kid, priv = oauth.signing_key()
        claims = jwt.decode(
            t,
            priv.public_key(),
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        # Enforce officer role and scope (reject citizen or arbitrary tokens)
        if claims.get("role") != "officer" and claims.get("scope") != "officer":
            return None
        sub = claims.get("sub", "")
        if not sub.startswith("officer:"):
            return None

        officer_username = sub.split(":", 1)[1]
        blocks = services.get_officer_authorized_blocks(officer_username)
        if not blocks:
            return None

        return {
            "username": officer_username,
            "role": "officer",
            "authorized_blocks": blocks,
            "claims": claims
        }
    except Exception:
        return None


def _resolve_officer(officer_token: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve and authenticate officer from explicit argument or request context."""
    token = officer_token or current_officer_token.get()
    if not token:
        return None, "Unauthorized: Valid officer authentication token is required."
    officer_info = verify_officer_session(token)
    if not officer_info:
        return None, "Unauthorized: Invalid or expired officer token. Please authenticate."
    return officer_info, None


# ---------------------------------------------------------------------------
# Tool: authenticate_officer (Sign-in helper)
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def authenticate_officer(username: str, password: str) -> Dict[str, Any]:
    """
    Authenticate departmental block officer credentials and obtain an authorized session token.

    Args:
        username: Officer departmental username (e.g. 'officer_sonari', 'admin')
        password: Secure departmental password

    Returns:
        Structured response with signed session token, authorized blocks, and expiry.
    """
    auth = services.authenticate_officer(username, password)
    if not auth:
        return {
            "success": False,
            "error": "Authentication failed: Invalid officer credentials."
        }

    officer_user = auth["username"]
    token = create_officer_session_token(officer_user)
    return {
        "success": True,
        "officer_token": token,
        "token_type": "Bearer",
        "officer_username": officer_user,
        "authorized_blocks": auth["authorized_blocks"],
        "expires_in_seconds": OFFICER_SESSION_TTL,
        "message": f"Successfully authenticated as officer '{officer_user}'. Authorized blocks: {', '.join(auth['authorized_blocks'])}."
    }


# ---------------------------------------------------------------------------
# Tool 1: get_pending_applications
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def get_pending_applications(
    officer_token: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
    sort_by: str = "submitted_at",
    sort_order: str = "asc",
    status_filter: str = "PENDING",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    block_filter: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve pending pension applications strictly within the authenticated officer's jurisdiction.

    Args:
        officer_token: Signed officer session token (or pass via Authorization header)
        page: Page number for pagination (default: 1)
        limit: Number of applications per page (default: 20, max: 100)
        sort_by: Sort column ('submitted_at', 'applicant_name', 'application_no', 'block')
        sort_order: Sort direction ('asc' or 'desc')
        status_filter: Filter by application status ('PENDING', 'APPROVED', 'REJECTED')
        date_from: Optional start submission date filter (YYYY-MM-DD)
        date_to: Optional end submission date filter (YYYY-MM-DD)
        block_filter: Optional administrative block filter (must be within officer's authorized blocks)

    Returns:
        Structured list of applications with pagination metadata and review timelines.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    officer_user = officer_info["username"]
    clamped_limit = min(max(1, limit), 100)
    clamped_page = max(1, page)

    try:
        res = services.get_officer_pending_applications(
            officer_username=officer_user,
            requested_block=block_filter,
            page=clamped_page,
            limit=clamped_limit,
            status=status_filter,
            sort_by=sort_by,
            sort_order=sort_order,
            date_from=date_from,
            date_to=date_to
        )
        return {
            "success": True,
            "officer": officer_user,
            "authorized_blocks": res["authorized_blocks"],
            "total_count": res["total"],
            "page": res["page"],
            "limit": res["limit"],
            "total_pages": res["total_pages"],
            "applications": res["applications"]
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Backend query error: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool 2: get_application_details
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def get_application_details(
    application_id: Union[int, str],
    officer_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve comprehensive review details for a specific pension application.
    Enforces block-level jurisdiction and protects sensitive bank and personal details.

    Args:
        application_id: Application ID or application number (e.g. 'SSP260105NAMD')
        officer_token: Signed officer session token

    Returns:
        Application details, applicant information, masked bank account, validation results,
        document status, and audit history.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    officer_user = officer_info["username"]

    try:
        details = services.get_officer_application_details(officer_user, application_id)
        if not details:
            return {"success": False, "error": f"Application '{application_id}' not found."}

        return {
            "success": True,
            "application": details
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Error retrieving application details: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool 3: get_review_checklist
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def get_review_checklist(
    application_id: Union[int, str],
    officer_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Return an authoritative, structured checklist of statutory eligibility criteria,
    supporting evidence, document verification, and outstanding issues for an application.

    Args:
        application_id: Application ID or application number
        officer_token: Signed officer session token

    Returns:
        Structured checklist items clearly distinguishing verified evidence from unverified claims.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    officer_user = officer_info["username"]

    try:
        details = services.get_officer_application_details(officer_user, application_id)
        if not details:
            return {"success": False, "error": f"Application '{application_id}' not found."}

        chk = details["review_checklist"]
        app_no = details["application_no"]
        block = details["block"]
        age = details.get("age")
        dob = details.get("dob")
        doc = details.get("document", {})

        checklist_items = [
            {
                "criterion": "Age Requirement (>= 60 years)",
                "status": "PASS" if chk.get("is_age_eligible") else "FAIL",
                "evidence": f"Calculated age: {age} years (DOB: {dob}). Statutory minimum is 60 years.",
                "verified_by": "System DOB calculation"
            },
            {
                "criterion": "Administrative Jurisdiction",
                "status": "PASS" if chk.get("is_block_valid") else "FAIL",
                "evidence": f"Village: {details.get('village')}, Block: {block} (Recognized block under officer jurisdiction).",
                "verified_by": "Block master database"
            },
            {
                "criterion": "Age Proof Documentation",
                "status": "PASS" if chk.get("is_age_proof_attached") else "FAIL",
                "evidence": f"Attached file: '{doc.get('filename')}'" if doc.get("attached") else "No age-proof document uploaded.",
                "verified_by": "Document store inspection"
            },
            {
                "criterion": "DBT Bank Account Verification",
                "status": "PASS" if (chk.get("is_bank_account_valid") and chk.get("is_ifsc_valid")) else "FAIL",
                "evidence": f"Account format valid ({details.get('bank_account_masked')}), IFSC: {details.get('ifsc')} format valid.",
                "verified_by": "RBI IFSC & DBT format validator"
            },
            {
                "criterion": "Statutory SLA Compliance",
                "status": "WARNING" if chk.get("is_sla_overdue") else "PASS",
                "evidence": f"{chk.get('days_pending')} days elapsed since submission (Statutory limit: {services.SLA_DAYS} days).",
                "verified_by": "Submission timestamp audit"
            }
        ]

        outstanding_issues = []
        if not chk.get("is_age_eligible"):
            outstanding_issues.append(f"Applicant is under 60 years of age (age: {age}).")
        if not chk.get("is_age_proof_attached"):
            outstanding_issues.append("Missing mandatory age-proof document.")
        if not chk.get("is_bank_account_valid"):
            outstanding_issues.append("Bank account format is invalid.")
        if not chk.get("is_ifsc_valid"):
            outstanding_issues.append("Bank IFSC code is invalid.")

        readiness = "READY_FOR_DECISION" if len(outstanding_issues) == 0 else "DEFICIENT"

        return {
            "success": True,
            "application_id": details["id"],
            "application_no": app_no,
            "applicant_name": details["applicant_name"],
            "block": block,
            "current_status": details["status"],
            "overall_readiness": readiness,
            "is_statutorily_eligible": chk.get("is_age_eligible", False),
            "checklist": checklist_items,
            "outstanding_issues": outstanding_issues,
            "guidance_for_officer": (
                "All statutory criteria satisfied. Application is ready for officer approval."
                if readiness == "READY_FOR_DECISION"
                else "Application has deficiencies. Review outstanding issues before proceeding."
            )
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Error generating review checklist: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool 4: approve_application
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def approve_application(
    application_id: Union[int, str],
    reason: str,
    officer_confirmed: bool = False,
    officer_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Approve an eligible pending pension application.
    Enforces role authorization, statutory eligibility, and explicit human confirmation.

    Args:
        application_id: Application ID or application number to approve
        reason: Mandatory departmental justification / review remarks
        officer_confirmed: MUST be True. The AI assistant must present review evidence
                           and obtain explicit confirmation from the human officer.
        officer_token: Signed officer session token

    Returns:
        Confirmation of approved status, timestamp, audit log, and application number.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    # Human Confirmation Safeguard
    if not officer_confirmed:
        return {
            "success": False,
            "error": (
                "Action aborted: Explicit officer confirmation required (officer_confirmed must be true). "
                "The AI assistant must present the review checklist and obtain explicit human confirmation "
                "before executing an approval."
            )
        }

    clean_reason = (reason or "").strip()
    if not clean_reason:
        clean_reason = "Approved following verification of age proof and eligibility criteria."

    officer_user = officer_info["username"]

    try:
        res = services.decide_application(
            officer_username=officer_user,
            app_id_or_no=application_id,
            decision="APPROVED",
            reason=clean_reason
        )
        if not res.get("success"):
            return {
                "success": False,
                "error": res.get("error", "Application could not be approved.")
            }

        return {
            "success": True,
            "application_id": res["application_id"],
            "application_no": res["application_no"],
            "status": "APPROVED",
            "decided_by": res["decided_by"],
            "decided_at": res["decided_at"],
            "reason": res["reason"],
            "message": f"Application {res['application_no']} successfully approved by officer '{officer_user}'."
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to approve application: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool 5: reject_application
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def reject_application(
    application_id: Union[int, str],
    reason: str,
    officer_confirmed: bool = False,
    officer_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Reject a pending pension application with a mandatory recorded reason.
    Enforces role authorization, status validation, and explicit human confirmation.

    Args:
        application_id: Application ID or application number to reject
        reason: Mandatory rejection reason explaining why the application is not approved
        officer_confirmed: MUST be True. Explicit human officer confirmation required.
        officer_token: Signed officer session token

    Returns:
        Confirmation of rejected status, recorded reason, and audit trail record.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    # Human Confirmation Safeguard
    if not officer_confirmed:
        return {
            "success": False,
            "error": (
                "Action aborted: Explicit officer confirmation required (officer_confirmed must be true). "
                "The AI assistant must present the findings and obtain explicit human confirmation "
                "before executing a rejection."
            )
        }

    clean_reason = (reason or "").strip()
    if not clean_reason:
        return {
            "success": False,
            "error": "Rejection reason is mandatory according to departmental policy."
        }

    officer_user = officer_info["username"]

    try:
        res = services.decide_application(
            officer_username=officer_user,
            app_id_or_no=application_id,
            decision="REJECTED",
            reason=clean_reason
        )
        if not res.get("success"):
            return {
                "success": False,
                "error": res.get("error", "Application could not be rejected.")
            }

        return {
            "success": True,
            "application_id": res["application_id"],
            "application_no": res["application_no"],
            "status": "REJECTED",
            "decided_by": res["decided_by"],
            "decided_at": res["decided_at"],
            "reason": res["reason"],
            "message": f"Application {res['application_no']} rejected. Reason: {clean_reason}."
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to reject application: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool 6: get_decision_history
# ---------------------------------------------------------------------------

@officer_mcp.tool()
def get_decision_history(
    application_id: Optional[Union[int, str]] = None,
    limit: int = 50,
    officer_token: Optional[str] = None
) -> Dict[str, Any]:
    """
    Retrieve authorized decision history and audit trail records.

    Args:
        application_id: Optional application ID or application number to inspect
        limit: Maximum number of audit records to return (default: 50)
        officer_token: Signed officer session token

    Returns:
        Chronological list of decisions, timestamps, responsible actors, and recorded reasons.
    """
    officer_info, err = _resolve_officer(officer_token)
    if err:
        return {"success": False, "error": err}

    officer_user = officer_info["username"]
    clamped_limit = min(max(1, limit), 200)

    try:
        history = services.get_officer_decision_history(
            officer_username=officer_user,
            app_id_or_no=application_id,
            limit=clamped_limit
        )
        return {
            "success": True,
            "officer": officer_user,
            "authorized_blocks": officer_info["authorized_blocks"],
            "record_count": len(history),
            "history": history
        }
    except PermissionError as pe:
        return {"success": False, "error": f"Forbidden: {str(pe)}"}
    except Exception as e:
        return {"success": False, "error": f"Error retrieving decision history: {str(e)}"}


# ---------------------------------------------------------------------------
# ASGI Middleware & Application Factory
# ---------------------------------------------------------------------------

class OfficerAuthMiddleware:
    """
    ASGI middleware that extracts Bearer token from the HTTP Authorization header
    and attaches it to current_officer_token ContextVar for the duration of the request.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth_header = None
            for key, val in scope.get("headers", []):
                if key.lower() == b"authorization":
                    auth_header = val.decode("latin1", errors="ignore")
                    break
            if auth_header and auth_header.lower().startswith("bearer "):
                tok = auth_header[7:].strip()
                token_reset = current_officer_token.set(tok)
                try:
                    await self.app(scope, receive, send)
                finally:
                    current_officer_token.reset(token_reset)
                return
        await self.app(scope, receive, send)


def create_officer_mcp_app():
    """Create and configure Starlette ASGI application for the Officer MCP server."""
    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"]
    )
    raw_app = officer_mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=sec,
        stateless_http=True
    )
    wrapped = OfficerAuthMiddleware(raw_app)
    wrapped.raw_app = raw_app
    return wrapped
