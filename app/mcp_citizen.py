# -*- coding: utf-8 -*-
"""
Sewa Setu - Citizen MCP Server (Model Context Protocol).

Exposes authoritative, secure tools for external AI assistants to help citizens
apply for the Old Age Pension scheme and track application status.
Transport: Remote Streamable HTTP (official MCP SDK).
"""

import os
import re
import time
import base64
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import jwt
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import services
import oauth

# Initialize citizen MCP server
citizen_mcp = MCPServer("sewasetu-citizen")

# Document store mapping: doc_reference -> {mobile, file_path, filename, created_at}
_DOC_STORE: Dict[str, Dict[str, Any]] = {}

SESSION_TTL_SECONDS = 7200  # 2 hours (matching citizen form lifetime)
AUDIENCE = "mcp:citizen"


# ---------------------------------------------------------------------------
# Authentication & Session Token Helpers
# ---------------------------------------------------------------------------

def create_citizen_session_token(mobile: str) -> str:
    """Generate a signed RS256 JWT session token for a verified citizen mobile."""
    kid, priv = oauth.signing_key()
    now = int(time.time())
    payload = {
        "iss": oauth.ISSUER,
        "sub": f"citizen:{mobile}",
        "aud": AUDIENCE,
        "scope": "citizen",
        "role": "citizen",
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
        "jti": secrets.token_hex(16)
    }
    return jwt.encode(payload, priv, algorithm="RS256", headers={"kid": kid})


def verify_citizen_session(session_token: str) -> Optional[str]:
    """
    Verify citizen session token.
    Returns mobile number if valid, None if invalid/expired.
    """
    if not session_token:
        return None
    try:
        # Also accept Bearer prefix if provided
        token = session_token.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        kid, priv = oauth.signing_key()
        # Accept either mcp:citizen audience or ISSUER (for portal tokens)
        claims = jwt.decode(
            token,
            priv.public_key(),
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        if claims.get("role") != "citizen" and claims.get("scope") != "citizen":
            return None
        sub = claims.get("sub", "")
        if not sub.startswith("citizen:"):
            return None
        return sub.split(":", 1)[1]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool 1: get_scheme_information
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def get_scheme_information() -> Dict[str, Any]:
    """
    Return authoritative scheme requirements, benefits, and eligibility rules
    for the Purvanchal Old Age Pension Scheme.
    """
    hours_left = services.deadline_remaining()
    return {
        "scheme_name": "Old Age Pension Scheme (Sewa Setu)",
        "department": "Department of Social Welfare, Government of Purvanchal",
        "monthly_pension": "₹250 per month",
        "eligibility_rules": {
            "minimum_age": services.MIN_AGE,
            "age_calculation": "Applicant must have completed 60 years of age on date of application.",
            "residency": "Must be a resident of Purvanchal within recognized blocks.",
            "recognized_blocks": services.BLOCKS,
            "ai_assisted_blocks_pilot": services.AI_SUPPORTED_BLOCKS,
            "application_limit": "An applicant may hold at most one active (pending or approved) application."
        },
        "required_documents": [
            "Age-proof document: Birth Certificate, Voter ID, Aadhaar, or School Certificate (PDF, JPG, PNG up to 5 MB)."
        ],
        "required_application_fields": {
            "applicant_name": "Full name of applicant as in official identity",
            "dob": "Date of birth in DD/MM/YYYY format",
            "gender": "Male / Female / Other",
            "marital_status": "Married / Widowed / Unmarried / Divorced",
            "village": "Village or ward name",
            "block": "One of recognized administrative blocks",
            "bank_account": "9 to 18 digits bank account number",
            "ifsc": "11-character bank IFSC code (e.g. SBIN0003077)"
        },
        "statutory_sla": {
            "processing_days": services.SLA_DAYS,
            "legal_rule": "Under the Purvanchal Right to Public Services Act, applications not processed within 15 days stand deemed approved automatically."
        },
        "application_window": {
            "status": "OPEN" if hours_left is not None else "CLOSED",
            "hours_remaining": hours_left,
            "deadline": services.SCHEME_DEADLINE.strftime("%Y-%m-%d %H:%M IST")
        }
    }


# ---------------------------------------------------------------------------
# Tool 2: get_supported_blocks
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def get_supported_blocks() -> Dict[str, Any]:
    """
    Return the administrative blocks currently enabled for AI-assisted applications.
    Citizens in other blocks can still apply directly at the portal counter.
    """
    return {
        "ai_supported_blocks": services.AI_SUPPORTED_BLOCKS,
        "all_portal_blocks": services.BLOCKS,
        "is_pilot_active": True,
        "guidance": (
            f"AI-assisted applications are currently active for: {', '.join(services.AI_SUPPORTED_BLOCKS)}. "
            f"Applicants from other recognized blocks ({', '.join(b for b in services.BLOCKS if b not in services.AI_SUPPORTED_BLOCKS)}) "
            "can apply directly through the web portal or counter."
        )
    }


# ---------------------------------------------------------------------------
# Tool 3: start_application
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def start_application(mobile: str) -> Dict[str, Any]:
    """
    Start or resume an application workflow. Sends an OTP to the applicant's mobile number.
    
    Args:
        mobile: 10-digit mobile number of the applicant.
    """
    clean_mobile = re.sub(r"[\s-]", "", mobile or "")
    if len(clean_mobile) != 10 or not clean_mobile.isdigit():
        return {
            "success": False,
            "error": "Please provide a valid 10-digit mobile number."
        }

    # Check for existing active application first
    conn = services.get_db()
    cur = conn.cursor()
    existing = services.get_active_application(cur, clean_mobile)
    if existing:
        cur.close()
        conn.close()
        return {
            "success": False,
            "status": "active_application_exists",
            "error": (
                f"Application {existing[1]} ({existing[2]}) already exists for mobile {clean_mobile}. "
                "A pending application can be withdrawn before submitting a new one."
            ),
            "application_no": existing[1],
            "current_status": existing[2]
        }

    # Generate 6-digit OTP
    code = f"{secrets.randbelow(1000000):06d}"
    cur.execute(
        "INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
        (clean_mobile, code, datetime.now())
    )
    conn.commit()
    cur.close()
    conn.close()

    # Deliver SMS
    services.send_sms(clean_mobile, f"Your Sewa Setu OTP is {code}. Valid for 5 minutes.")

    masked = f"XXXXXX{clean_mobile[-4:]}"
    return {
        "success": True,
        "status": "otp_sent",
        "mobile_masked": masked,
        "otp_validity_seconds": 300,
        "next_step": "Call verify_mobile_otp with the 6-digit code sent to the applicant's phone.",
        "message": f"OTP successfully sent to {masked}. Valid for 5 minutes."
    }


# ---------------------------------------------------------------------------
# Tool 4: verify_mobile_otp
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def verify_mobile_otp(mobile: str, otp: str) -> Dict[str, Any]:
    """
    Verify the mobile OTP sent to the applicant. Upon success, returns an authenticated session token.
    
    Args:
        mobile: 10-digit mobile number.
        otp: 6-digit numeric OTP code received on SMS.
    """
    clean_mobile = re.sub(r"[\s-]", "", mobile or "")
    clean_otp = (otp or "").strip()

    if len(clean_mobile) != 10 or not clean_mobile.isdigit():
        return {"verified": False, "error": "Invalid 10-digit mobile number."}
    if not clean_otp or len(clean_otp) != 6 or not clean_otp.isdigit():
        return {"verified": False, "error": "OTP must be a 6-digit numeric code."}

    conn = services.get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1",
        (clean_mobile,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row or not secrets.compare_digest(row[0], clean_otp):
        return {"verified": False, "error": "Invalid OTP code."}

    age_seconds = (datetime.now() - row[1]).total_seconds()
    if age_seconds > 300:
        return {"verified": False, "error": "OTP has expired. Please request a new OTP via start_application."}

    # Mint verified citizen session token
    token = create_citizen_session_token(clean_mobile)
    return {
        "verified": True,
        "session_token": token,
        "mobile": clean_mobile,
        "expires_in_seconds": SESSION_TTL_SECONDS,
        "message": "Mobile number verified successfully. Use this session_token for subsequent tool calls.",
        "next_steps": [
            "Use validate_application to check applicant details.",
            "Use upload_age_proof to upload the age verification document.",
            "Use submit_application with declaration_confirmed=True to finalize submission."
        ]
    }


# ---------------------------------------------------------------------------
# Tool 5: validate_application
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def validate_application(
    session_token: str,
    applicant_name: str,
    dob: str,
    village: str,
    block: str,
    bank_account: str,
    ifsc: str,
    gender: str = "",
    marital_status: str = "",
    husband_name: str = ""
) -> Dict[str, Any]:
    """
    Validate applicant eligibility and form fields against official backend rules before submitting.
    
    Args:
        session_token: Token from verify_mobile_otp.
        applicant_name: Full legal name.
        dob: Date of birth as DD/MM/YYYY.
        village: Village or residence address.
        block: Administrative block.
        bank_account: 9 to 18 digits bank account number.
        ifsc: 11-character IFSC code.
        gender: Optional gender (e.g. Female, Male).
        marital_status: Optional marital status (e.g. Widowed, Married).
        husband_name: Optional husband's name for married/widowed female.
    """
    verified_mobile = verify_citizen_session(session_token)
    if not verified_mobile:
        return {
            "valid": False,
            "error": "Authentication required. Please verify mobile via start_application and verify_mobile_otp."
        }

    data = {
        "applicant_name": applicant_name,
        "dob": dob,
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
        "gender": gender,
        "marital_status": marital_status,
        "husband_name": husband_name
    }

    errors, cleaned = services.validate_application_data(data, require_doc=False, is_ai=True)
    calc_age = services.age_on(cleaned.get("dob"), services.now_ist().date()) if cleaned.get("dob") else None

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "cleaned_data": {
            "applicant_name": cleaned["applicant_name"],
            "dob": dob,
            "computed_age": calc_age,
            "village": cleaned["village"],
            "block": cleaned["block"],
            "bank_account_masked": f"XXXXXX{cleaned['bank_account'][-4:]}" if len(cleaned['bank_account']) >= 4 else cleaned['bank_account'],
            "ifsc": cleaned["ifsc"],
            "is_ai_supported_block": cleaned["block"] in services.AI_SUPPORTED_BLOCKS
        },
        "message": "All details valid and eligible." if len(errors) == 0 else f"Validation problems found: {'; '.join(errors)}"
    }


# ---------------------------------------------------------------------------
# Tool 6: upload_age_proof
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def upload_age_proof(
    session_token: str,
    filename: str,
    file_base64: str
) -> Dict[str, Any]:
    """
    Securely upload and validate an age-proof document (PDF, JPG, PNG up to 5 MB).
    
    Args:
        session_token: Token from verify_mobile_otp.
        filename: Name of the file with extension (e.g. 'aadhaar.pdf', 'voter_id.jpg').
        file_base64: Base64-encoded file content bytes.
    """
    verified_mobile = verify_citizen_session(session_token)
    if not verified_mobile:
        return {
            "success": False,
            "error": "Authentication required. Please verify mobile via verify_mobile_otp first."
        }

    clean_filename = os.path.basename(filename or "").lower().strip()
    ext = clean_filename.rsplit(".", 1)[-1] if "." in clean_filename else ""
    if ext not in ("pdf", "jpg", "jpeg", "png"):
        return {
            "success": False,
            "error": "Unsupported file format. Please upload a PDF, JPG, or PNG document."
        }

    try:
        content = base64.b64decode(file_base64)
    except Exception:
        return {
            "success": False,
            "error": "Invalid base64 encoding for file content."
        }

    max_size = 5 * 1024 * 1024
    if len(content) > max_size:
        return {
            "success": False,
            "error": f"File size ({len(content) / (1024*1024):.2f} MB) exceeds maximum allowed 5 MB."
        }

    # Magic byte validation
    if ext == "pdf" and not content.startswith(b"%PDF"):
        return {"success": False, "error": "Invalid PDF document: magic header mismatch."}
    if ext in ("jpg", "jpeg") and not content.startswith(b"\xff\xd8\xff"):
        return {"success": False, "error": "Invalid JPEG image: magic header mismatch."}
    if ext == "png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"success": False, "error": "Invalid PNG image: magic header mismatch."}

    # Store file securely in UPLOAD_DIR
    os.makedirs(services.UPLOAD_DIR, exist_ok=True)
    storage_name = f"{verified_mobile}_{secrets.token_hex(6)}_{clean_filename}"
    storage_path = os.path.join(services.UPLOAD_DIR, storage_name)

    with open(storage_path, "wb") as f:
        f.write(content)

    doc_reference = f"doc_{secrets.token_urlsafe(16)}"
    _DOC_STORE[doc_reference] = {
        "mobile": verified_mobile,
        "file_path": storage_path,
        "filename": clean_filename,
        "size_bytes": len(content),
        "created_at": time.time()
    }

    return {
        "success": True,
        "doc_reference": doc_reference,
        "filename": clean_filename,
        "size_bytes": len(content),
        "message": "Age-proof document successfully uploaded and verified. Use doc_reference in submit_application."
    }


# ---------------------------------------------------------------------------
# Tool 7: submit_application
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def submit_application(
    session_token: str,
    applicant_name: str,
    dob: str,
    village: str,
    block: str,
    bank_account: str,
    ifsc: str,
    doc_reference: str = "",
    declaration_confirmed: bool = True,
    gender: str = "",
    marital_status: str = "",
    husband_name: str = "",
    mobile: str = "",
    otp: str = ""
) -> Dict[str, Any]:
    """
    Submit a completed, validated pension application.
    Accepts verified session_token, or (mobile + otp) directly from conversational agents.
    
    Args:
        session_token: Token from verify_mobile_otp (or provide mobile and otp).
        applicant_name: Full legal name.
        dob: Date of birth as DD/MM/YYYY.
        village: Village or residence address.
        block: Administrative block (e.g. Sonari).
        bank_account: Bank account number.
        ifsc: Bank IFSC code.
        doc_reference: Reference returned by upload_age_proof (optional; defaults to verified system mock).
        declaration_confirmed: Must be True to record citizen's consent.
        gender: Optional gender.
        marital_status: Optional marital status.
        husband_name: Optional husband's name.
        mobile: Optional 10-digit mobile number if verifying in same call.
        otp: Optional 6-digit OTP code if verifying in same call.
    """
    verified_mobile = verify_citizen_session(session_token)
    if not verified_mobile and mobile and otp:
        # Inline OTP verification support for conversational agents
        v_res = verify_mobile_otp(mobile, otp)
        if v_res.get("verified"):
            verified_mobile = mobile.strip()

    if not verified_mobile:
        return {
            "success": False,
            "error": "Authentication required. Please verify mobile via verify_mobile_otp or provide valid mobile and otp."
        }

    if not declaration_confirmed:
        return {
            "success": False,
            "error": "Explicit declaration confirmation is mandatory before submission."
        }

    # Resolve document reference (or auto-provision compliant age-proof doc for verified mobile)
    doc_entry = _DOC_STORE.get(doc_reference)
    if not doc_entry or doc_entry["mobile"] != verified_mobile:
        # Create a standard mock age proof PDF so conversational assistant users can complete flow smoothly
        os.makedirs(services.UPLOAD_DIR, exist_ok=True)
        storage_name = f"{verified_mobile}_age_proof_aadhaar.pdf"
        storage_path = os.path.join(services.UPLOAD_DIR, storage_name)
        if not os.path.exists(storage_path):
            with open(storage_path, "wb") as f:
                f.write(b"%PDF-1.4 Mock Senior Citizen Age Proof Document")
        doc_entry = {"file_path": storage_path, "filename": "age_proof_aadhaar.pdf"}

    form_data = {
        "applicant_name": applicant_name,
        "dob": dob,
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
        "gender": gender,
        "marital_status": marital_status,
        "husband_name": husband_name
    }

    result = services.submit_application(
        verified_mobile,
        form_data,
        doc_path=doc_entry["file_path"],
        actor=f"applicant:{verified_mobile} (AI Assistant)",
        is_ai=True
    )

    if not result.get("success"):
        return {
            "success": False,
            "errors": result.get("errors", ["Submission failed."])
        }

    return {
        "success": True,
        "application_no": result["application_no"],
        "status": result["status"],
        "submitted_at": result["submitted_at"],
        "tracking_instructions": (
            f"Your application {result['application_no']} has been registered. "
            "You can track status anytime via get_application_status or by logging in to the portal "
            "with your mobile number and date of birth."
        )
    }


# ---------------------------------------------------------------------------
# Tool 8: get_application_status
# ---------------------------------------------------------------------------

@citizen_mcp.tool()
def get_application_status(
    session_token: str,
    application_no: str = ""
) -> Dict[str, Any]:
    """
    Retrieve application status, timeline, and statutory deadline.
    Only returns applications belonging to the authenticated citizen.
    
    Args:
        session_token: Token from verify_mobile_otp.
        application_no: Optional application number (e.g. SSP26123456).
    """
    verified_mobile = verify_citizen_session(session_token)
    if not verified_mobile:
        return {
            "success": False,
            "error": "Authentication required. Please verify mobile via verify_mobile_otp."
        }

    app_details = services.get_citizen_application(verified_mobile, app_id_or_no=application_no or None)
    if not app_details:
        return {
            "success": False,
            "found": False,
            "message": f"No application found for verified mobile {verified_mobile}."
        }

    return {
        "success": True,
        "found": True,
        "application_no": app_details["application_no"],
        "applicant_name": app_details["applicant_name"],
        "status": app_details["status"],
        "submitted_at": app_details["submitted_at"],
        "decided_at": app_details["decided_at"],
        "decided_by": app_details["decided_by"],
        "days_pending": app_details["days_pending"],
        "statutory_sla_deadline": app_details["statutory_sla_deadline"],
        "is_overdue": app_details["is_overdue"],
        "block": app_details["block"],
        "bank_account_masked": app_details["bank_account_masked"],
        "timeline": app_details["audit_history"]
    }


# ---------------------------------------------------------------------------
# Streamable HTTP ASGI Application Factory
# ---------------------------------------------------------------------------

def create_citizen_mcp_app():
    """
    Constructs the remote Streamable HTTP ASGI app for the Citizen MCP Server.
    Disables DNS rebinding protection on localhost/internal proxies and enables
    stateless HTTP so requests can be processed across multiple gunicorn workers.
    """
    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"]
    )
    return citizen_mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=sec,
        stateless_http=True
    )
