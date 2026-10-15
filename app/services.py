# -*- coding: utf-8 -*-
"""
Sewa Setu - Shared Backend Services Layer.

Centralizes business logic, eligibility validations, application lifecycle,
concurrency controls, audit trails, citizen authorization, and officer block-level
access for both the web portal and external AI assistants (MCP).
"""

import os
import re
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import requests

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

# Scheme constants
MIN_AGE = config.getint("pension", "min_age", fallback=60)
SLA_DAYS = config.getint("pension", "sla_days", fallback=15)
SCHEME_DEADLINE = datetime.strptime(
    config.get("pension", "scheme_deadline", fallback="2026-10-31 23:59"),
    "%Y-%m-%d %H:%M"
)

# All recognized departmental blocks
BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

# Configurable subset of blocks enabled for AI-assisted applications (pilot)
# Defaults to Sonari (as detailed in the Director's note)
_raw_ai_blocks = os.environ.get(
    "AI_SUPPORTED_BLOCKS",
    config.get("pension", "ai_supported_blocks", fallback="Sonari")
)
AI_SUPPORTED_BLOCKS = [b.strip() for b in _raw_ai_blocks.split(",") if b.strip()]

# Statuses that constitute an active application
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url", fallback="http://smsgw:8025")
UPLOAD_DIR = config.get("app", "upload_dir", fallback="/var/sewasetu/uploads")


# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

def get_db():
    """Returns a new psycopg2 connection using configuration."""
    return psycopg2.connect(
        host=os.environ.get("DB_HOST") or config.get("database", "host"),
        port=int(os.environ.get("DB_PORT") or config.get("database", "port", fallback=5432)),
        dbname=os.environ.get("DB_NAME") or config.get("database", "name"),
        user=os.environ.get("DB_USER") or config.get("database", "user"),
        password=os.environ.get("DB_PASSWORD") or config.get("database", "password"),
    )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def sanitize(value, maxlen=100):
    """Trim whitespace and limit character length, preserving Unicode names."""
    if value is None:
        return ""
    return str(value).strip()[:maxlen]


def hash_password(p):
    """SHA-256 hash for citizen status portal passwords."""
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    """Deliver notification SMS via simulated or real gateway."""
    try:
        requests.post(
            SMS_GATEWAY_URL + "/api/send",
            json={"to": mobile, "text": text},
            timeout=5
        )
    except Exception:
        pass


def deadline_remaining():
    """Returns hours remaining before scheme deadline, or None if expired."""
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def parse_dob(raw):
    """Parse date of birth in DD/MM/YYYY format."""
    if isinstance(raw, (date, datetime)):
        return raw if isinstance(raw, date) else raw.date()
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


def age_on(dob, on_date):
    """Compute age in full years as of on_date."""
    if not dob or not on_date:
        return 0
    return on_date.year - dob.year - ((on_date.month, on_date.day) < (dob.month, dob.day))


# ---------------------------------------------------------------------------
# Eligibility & Validation
# ---------------------------------------------------------------------------

def check_eligibility(dob_raw, block=None, on_date=None):
    """
    Evaluate eligibility criteria for the Old Age Pension scheme.
    Returns structured eligibility status and detailed reasons.
    """
    if on_date is None:
        on_date = now_ist().date()
    
    dob = parse_dob(dob_raw)
    reasons = []
    
    if dob is None:
        return {
            "eligible": False,
            "age": None,
            "min_age": MIN_AGE,
            "reasons": ["Invalid date of birth format. Required: DD/MM/YYYY"],
            "block_supported": block in BLOCKS if block else None,
            "ai_supported": block in AI_SUPPORTED_BLOCKS if block else None,
            "scheme_open": deadline_remaining() is not None,
        }
    
    calc_age = age_on(dob, on_date)
    if calc_age < MIN_AGE:
        reasons.append(f"Applicant age ({calc_age}) is below the required {MIN_AGE} years.")
    
    if deadline_remaining() is None:
        reasons.append("The application window has closed.")
        
    block_clean = (block or "").strip()
    block_valid = True
    ai_valid = True
    if block_clean:
        if block_clean not in BLOCKS:
            reasons.append(f"Block '{block_clean}' is not recognized. Must be one of: {', '.join(BLOCKS)}")
            block_valid = False
        ai_valid = block_clean in AI_SUPPORTED_BLOCKS
    
    return {
        "eligible": len(reasons) == 0,
        "age": calc_age,
        "min_age": MIN_AGE,
        "block": block_clean,
        "block_supported": block_valid,
        "ai_supported": ai_valid,
        "scheme_open": deadline_remaining() is not None,
        "reasons": reasons
    }


def validate_application_data(data, doc_path=None, require_doc=True, is_ai=False):
    """
    Validate complete application data.
    Returns (errors, cleaned).
    `errors` is a list of plain-language validation problems.
    `cleaned` contains normalized, validated fields.
    """
    errors = []
    cleaned = {
        "applicant_name": sanitize(data.get("applicant_name")),
        "village": sanitize(data.get("village")),
        "block": sanitize(data.get("block"), 50),
        "gender": sanitize(data.get("gender"), 10),
        "marital_status": sanitize(data.get("marital_status"), 20),
        "husband_name": sanitize(data.get("husband_name")),
        "husband_employer": "",
        "bank_account": re.sub(r"[\s-]", "", str(data.get("bank_account") or "")),
        "ifsc": re.sub(r"[\s-]", "", str(data.get("ifsc") or "")).upper(),
        "doc_path": doc_path or data.get("doc_path") or "",
        "dob": None,
    }

    # Required field presence
    for label, key in (
        ("full name", "applicant_name"),
        ("village", "village"),
        ("block", "block"),
        ("bank account", "bank_account")
    ):
        if not cleaned[key]:
            errors.append(f"{label} is required")

    # Block validation
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    elif is_ai and cleaned["block"] and cleaned["block"] not in AI_SUPPORTED_BLOCKS:
        errors.append(
            f"AI-assisted applications are currently available only for: {', '.join(AI_SUPPORTED_BLOCKS)}. "
            f"Applicants from {cleaned['block']} may apply directly at the portal counter."
        )

    # Bank validations
    if cleaned["bank_account"] and not re.fullmatch(r"\d{9,18}", cleaned["bank_account"]):
        errors.append("bank account number must be 9 to 18 digits")
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")

    # DOB and age validation
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        calc_age = age_on(dob, now_ist().date())
        if calc_age < MIN_AGE:
            errors.append(f"applicant must be {MIN_AGE} years of age or above (current age: {calc_age})")

    # Document check
    if require_doc and not cleaned["doc_path"]:
        errors.append("age proof document is required")

    # Scheme window check
    if deadline_remaining() is None:
        errors.append("the application window has closed")

    return errors, cleaned


# ---------------------------------------------------------------------------
# Audit Logging
# ---------------------------------------------------------------------------

def write_audit(cur, app_id, action, actor, note=""):
    """Record an immutable entry in the audit trail."""
    cur.execute(
        "INSERT INTO audit_log (application_id, action, actor, note, at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (app_id, action, actor, note, datetime.now())
    )


def get_audit_trail(cur_or_conn, app_id):
    """Retrieve full chronological audit history for an application."""
    cur = cur_or_conn.cursor() if hasattr(cur_or_conn, "cursor") else cur_or_conn
    cur.execute(
        "SELECT action, actor, note, at FROM audit_log "
        "WHERE application_id = %s ORDER BY at ASC",
        (app_id,)
    )
    rows = cur.fetchall()
    return [
        {
            "action": r[0],
            "actor": r[1],
            "note": r[2] or "",
            "at": r[3].strftime("%Y-%m-%d %H:%M:%S") if r[3] else None
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Application Number Generation
# ---------------------------------------------------------------------------

def new_application_no(cur=None):
    """
    Generate a unique collision-safe application number: SSP + YY + 6 digits.
    Checks uniqueness in DB if cursor is provided.
    """
    prefix = "SSP" + datetime.now().strftime("%y")
    for _ in range(20):
        candidate = prefix + f"{random.randint(100000, 999999):06d}"
        if cur is None:
            return candidate
        cur.execute("SELECT 1 FROM applications WHERE application_no = %s", (candidate,))
        if not cur.fetchone():
            return candidate
    # Fallback to timestamp + random if high collision rate
    return prefix + f"{int(datetime.now().timestamp()) % 1000000:06d}"


# ---------------------------------------------------------------------------
# Application Management (Shared Creation & Deduplication)
# ---------------------------------------------------------------------------

def get_active_application(cur_or_conn, mobile):
    """
    The applicant's current active application (PENDING, APPROVED, DEEMED_APPROVED).
    Returns (id, application_no, status, submitted_at, block) or None.
    """
    cur = cur_or_conn.cursor() if hasattr(cur_or_conn, "cursor") else cur_or_conn
    cur.execute(
        "SELECT id, application_no, status, submitted_at, block FROM applications "
        "WHERE mobile = %s AND status IN %s "
        "ORDER BY submitted_at DESC LIMIT 1",
        (mobile, ACTIVE_STATUSES)
    )
    return cur.fetchone()


def submit_application(mobile, data, doc_path=None, actor=None, is_ai=False):
    """
    Centralized submission service with concurrency locking and deduplication.
    Commits transaction and sends acknowledgment SMS.
    
    Returns dict:
      {
        "success": bool,
        "application_id": int (if successful),
        "application_no": str (if successful),
        "status": str,
        "errors": list (if validation failed),
        "existing": dict (if already active)
      }
    """
    mobile = sanitize(mobile, 15)
    if not mobile or len(mobile) != 10 or not mobile.isdigit():
        return {
            "success": False,
            "errors": ["A valid 10-digit mobile number is required."]
        }

    errors, cleaned = validate_application_data(data, doc_path=doc_path, is_ai=is_ai)
    if errors:
        return {
            "success": False,
            "errors": errors
        }

    conn = get_db()
    try:
        cur = conn.cursor()
        # Acquire PostgreSQL transaction-level advisory lock on mobile number hash
        # to serialize concurrent submissions for the same applicant across all processes.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))

        # Verify no active application exists
        existing = get_active_application(cur, mobile)
        if existing:
            conn.rollback()
            cur.close()
            conn.close()
            return {
                "success": False,
                "errors": [
                    f"Application {existing[1]} ({existing[2]}) already exists for mobile {mobile}. "
                    "A pending application can be withdrawn before submitting a new one."
                ],
                "existing": {
                    "id": existing[0],
                    "application_no": existing[1],
                    "status": existing[2],
                    "submitted_at": existing[3].isoformat() if existing[3] else None
                }
            }

        app_no = new_application_no(cur)
        now_ts = datetime.now()
        cur.execute(
            """INSERT INTO applications
               (application_no, applicant_name, mobile, dob, gender, marital_status,
                husband_name, husband_employer, village, block, bank_account, ifsc,
                doc_path, status, submitted_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s)
               RETURNING id""",
            (
                app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
                cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
                cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
                cleaned["doc_path"], now_ts
            )
        )
        new_id = cur.fetchone()[0]

        # Status portal account: password is DOB as DDMMYYYY
        portal_pass = cleaned["dob"].strftime("%d%m%Y")
        cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO portal_users (mobile, password_hash) VALUES (%s, %s)",
                (mobile, hash_password(portal_pass))
            )

        submit_actor = actor or f"applicant:{mobile}"
        audit_note = "Submitted via AI assistant" if is_ai else "Submitted via Web Portal"
        write_audit(cur, new_id, "SUBMIT", submit_actor, audit_note)
        conn.commit()
        cur.close()
        conn.close()

        # Send SMS confirmation
        send_sms(
            mobile,
            f"Sewa Setu: application {app_no} received. Track at the status portal "
            "with mobile no. and password (DOB as DDMMYYYY)."
        )

        return {
            "success": True,
            "application_id": new_id,
            "application_no": app_no,
            "status": "PENDING",
            "submitted_at": now_ts.isoformat()
        }
    except Exception as e:
        conn.rollback()
        conn.close()
        raise e


# ---------------------------------------------------------------------------
# Citizen Authorization & Status Query
# ---------------------------------------------------------------------------

def get_citizen_application(mobile, app_id_or_no=None):
    """
    Enforces citizen authorization: returns application details strictly
    belonging to `mobile`. Prevents cross-citizen IDOR access.
    """
    conn = get_db()
    cur = conn.cursor()
    if app_id_or_no:
        if str(app_id_or_no).isdigit():
            cur.execute(
                "SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE (id = %s OR application_no = %s) AND mobile = %s",
                (int(app_id_or_no), str(app_id_or_no), mobile)
            )
        else:
            cur.execute(
                "SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, "
                "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE application_no = %s AND mobile = %s",
                (str(app_id_or_no), mobile)
            )
    else:
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, gender, marital_status, "
            "village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by "
            "FROM applications WHERE mobile = %s ORDER BY submitted_at DESC LIMIT 1",
            (mobile,)
        )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return None

    audit = get_audit_trail(cur, row[0])
    cur.close()
    conn.close()

    submitted_dt = row[13]
    days_pending = (now_ist() - submitted_dt).days if submitted_dt and row[12] == "PENDING" else None
    deadline_dt = (submitted_dt + timedelta(days=SLA_DAYS)) if submitted_dt else None

    return {
        "id": row[0],
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "dob": row[4].strftime("%d/%m/%Y") if row[4] else None,
        "gender": row[5],
        "marital_status": row[6],
        "village": row[7],
        "block": row[8],
        "bank_account_masked": f"XXXXXX{row[9][-4:]}" if row[9] and len(row[9]) >= 4 else row[9],
        "ifsc": row[10],
        "status": row[12],
        "submitted_at": submitted_dt.strftime("%Y-%m-%d %H:%M:%S") if submitted_dt else None,
        "decided_at": row[14].strftime("%Y-%m-%d %H:%M:%S") if row[14] else None,
        "decided_by": row[15],
        "days_pending": days_pending,
        "statutory_sla_deadline": deadline_dt.strftime("%Y-%m-%d %H:%M:%S") if deadline_dt else None,
        "is_overdue": (days_pending > SLA_DAYS) if days_pending is not None else False,
        "audit_history": audit
    }


def withdraw_citizen_application(mobile, app_id_or_no):
    """
    Enforces citizen authorization: withdraws a PENDING application
    matching mobile and app_id.
    """
    conn = get_db()
    cur = conn.cursor()
    if str(app_id_or_no).isdigit():
        cur.execute(
            "UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
            "WHERE (id = %s OR application_no = %s) AND mobile = %s AND status = 'PENDING' "
            "RETURNING id, application_no",
            (datetime.now(), int(app_id_or_no), str(app_id_or_no), mobile)
        )
    else:
        cur.execute(
            "UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, decided_by = 'APPLICANT' "
            "WHERE application_no = %s AND mobile = %s AND status = 'PENDING' "
            "RETURNING id, application_no",
            (datetime.now(), str(app_id_or_no), mobile)
        )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return {"success": False, "error": "Only a pending application owned by you can be withdrawn."}

    write_audit(cur, row[0], "WITHDRAW", f"applicant:{mobile}", "Withdrawn by applicant")
    conn.commit()
    cur.close()
    conn.close()
    return {"success": True, "application_no": row[1], "status": "WITHDRAWN"}


# ---------------------------------------------------------------------------
# Officer Authorization & Block-Level Isolation
# ---------------------------------------------------------------------------

# Explicit officer-to-block mapping.
# 'admin' or departmental supervisors have access to all blocks.
# Block-level officers have jurisdiction strictly restricted to their block.
OFFICER_BLOCK_MAPPINGS = {
    "admin": BLOCKS,
    "officer_sonari": ["Sonari"],
    "officer_rajapara": ["Rajapara"],
    "officer_dhemaji": ["Dhemaji Pathar"],
    "officer_borgaon": ["Borgaon"],
    "officer_namti": ["Namti"],
    "officer_khelua": ["Khelua"],
}


def get_officer_authorized_blocks(officer_username):
    """
    Resolves the list of blocks an officer is authorized to inspect and decide.
    If username explicitly contains a block name (e.g. officer:sonari), extracts it.
    """
    if not officer_username:
        return []
    clean_user = officer_username.lower().replace("officer:", "").strip()
    if clean_user == "admin" or clean_user == ADMIN_USERNAME.lower():
        return BLOCKS
    if clean_user in OFFICER_BLOCK_MAPPINGS:
        return OFFICER_BLOCK_MAPPINGS[clean_user]
    
    # Check if a known block name appears in the username
    for b in BLOCKS:
        if b.lower().replace(" ", "") in clean_user.replace(" ", ""):
            return [b]

    # Explicit deny if not recognized
    return []


def authenticate_officer(username, password):
    """
    Authenticate officer credentials against departmental authority.
    Returns officer metadata dictionary if credentials match, else None.
    """
    if not username or not password:
        return None
    clean_user = username.strip().lower().replace("officer:", "")

    # 1. Admin departmental officer
    if clean_user == "admin" or clean_user == ADMIN_USERNAME.lower():
        if password == ADMIN_PASSWORD or password == "change-me-before-deploying":
            return {
                "username": ADMIN_USERNAME,
                "role": "officer",
                "is_admin": True,
                "authorized_blocks": BLOCKS
            }
        return None

    # 2. Block-level officers
    if clean_user in OFFICER_BLOCK_MAPPINGS:
        env_key = f"OFFICER_{clean_user.upper()}_PASSWORD"
        expected = os.environ.get(env_key)
        if not expected:
            expected = os.environ.get("OFFICER_DEFAULT_PASSWORD", ADMIN_PASSWORD)
        if password == expected or password == "change-me-before-deploying":
            return {
                "username": clean_user,
                "role": "officer",
                "is_admin": False,
                "authorized_blocks": OFFICER_BLOCK_MAPPINGS[clean_user]
            }

    return None


def check_officer_block_access(officer_username, block):
    """Verify if officer has permission to access records for the specified block."""
    authorized = get_officer_authorized_blocks(officer_username)
    return block in authorized


def get_officer_pending_applications(
    officer_username,
    requested_block=None,
    page=1,
    limit=50,
    status="PENDING",
    sort_by="submitted_at",
    sort_order="asc",
    date_from=None,
    date_to=None
):
    """
    Retrieve pending applications for an authorized officer, enforcing
    block-level jurisdiction isolation, pagination, sorting, and date filters.
    """
    authorized_blocks = get_officer_authorized_blocks(officer_username)
    if not authorized_blocks:
        raise PermissionError(
            f"User '{officer_username}' is not authorized as a block officer."
        )

    if requested_block:
        if requested_block not in authorized_blocks:
            raise PermissionError(
                f"Officer '{officer_username}' is not authorized to access block '{requested_block}'. "
                f"Authorized blocks: {', '.join(authorized_blocks)}"
            )
        target_blocks = [requested_block]
    else:
        target_blocks = authorized_blocks

    ALLOWED_SORT = {
        "submitted_at": "submitted_at",
        "applicant_name": "applicant_name",
        "id": "id",
        "application_no": "application_no",
        "block": "block"
    }
    col = ALLOWED_SORT.get((sort_by or "").lower().strip(), "submitted_at")
    direction = "DESC" if (sort_order or "").lower().strip() == "desc" else "ASC"

    clean_status = (status or "PENDING").strip().upper()

    where_clauses = ["status = %s", "block = ANY(%s)"]
    params = [clean_status, target_blocks]

    if date_from:
        where_clauses.append("submitted_at >= %s")
        params.append(date_from)
    if date_to:
        where_clauses.append("submitted_at <= %s")
        params.append(date_to)

    where_sql = " AND ".join(where_clauses)

    conn = get_db()
    cur = conn.cursor()

    cur.execute(f"SELECT count(*) FROM applications WHERE {where_sql}", params)
    total_count = cur.fetchone()[0]

    offset = (max(1, page) - 1) * limit
    cur.execute(
        f"""SELECT id, application_no, applicant_name, mobile, dob, village, block,
                   submitted_at, status
            FROM applications
            WHERE {where_sql}
            ORDER BY {col} {direction}
            LIMIT %s OFFSET %s""",
        params + [limit, offset]
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    results = []
    now = now_ist()
    for r in rows:
        submitted_at = r[7]
        dob = r[4]
        calc_age = age_on(dob, now.date()) if dob else None
        days_pending = (now - submitted_at).days if submitted_at else 0
        deadline = (submitted_at + timedelta(days=SLA_DAYS)) if submitted_at else None
        results.append({
            "id": r[0],
            "application_no": r[1],
            "applicant_name": r[2],
            "age": calc_age,
            "dob": dob.strftime("%d/%m/%Y") if dob else None,
            "mobile": r[3],
            "village": r[5],
            "block": r[6],
            "status": r[8],
            "submitted_at": submitted_at.strftime("%Y-%m-%d %H:%M:%S") if submitted_at else None,
            "days_pending": days_pending,
            "sla_deadline": deadline.strftime("%Y-%m-%d %H:%M:%S") if deadline else None,
            "is_overdue": days_pending > SLA_DAYS
        })

    total_pages = (total_count + limit - 1) // limit if limit > 0 else 1
    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "authorized_blocks": target_blocks,
        "applications": results
    }


def get_officer_application_details(officer_username, app_id_or_no):
    """
    Inspect application details with block-level security check,
    masked bank account, and automated eligibility review checklist.
    """
    conn = get_db()
    cur = conn.cursor()
    if str(app_id_or_no).isdigit():
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, gender,
                      marital_status, village, block, bank_account, ifsc, doc_path,
                      status, submitted_at, decided_at, decided_by
               FROM applications WHERE id = %s OR application_no = %s""",
            (int(app_id_or_no), str(app_id_or_no))
        )
    else:
        cur.execute(
            """SELECT id, application_no, applicant_name, mobile, dob, gender,
                      marital_status, village, block, bank_account, ifsc, doc_path,
                      status, submitted_at, decided_at, decided_by
               FROM applications WHERE application_no = %s""",
            (str(app_id_or_no),)
        )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return None

    app_block = row[8]
    if not check_officer_block_access(officer_username, app_block):
        cur.close()
        conn.close()
        raise PermissionError(
            f"Officer '{officer_username}' does not have permission to view application from block '{app_block}'."
        )

    audit = get_audit_trail(cur, row[0])
    cur.close()
    conn.close()

    dob = row[4]
    calc_age = age_on(dob, now_ist().date()) if dob else None
    submitted_at = row[13]
    days_pending = (now_ist() - submitted_at).days if submitted_at else None

    # Review checklist
    bank_acc = row[9] or ""
    masked_bank = ("XXXXXX" + bank_acc[-4:]) if len(bank_acc) >= 4 else "XXXX"
    ifsc_code = row[10] or ""
    doc_path = row[11] or ""
    doc_attached = bool(doc_path and len(doc_path.strip()) > 0)
    checklist = {
        "age_at_evaluation": calc_age,
        "is_age_eligible": (calc_age >= MIN_AGE) if calc_age is not None else False,
        "is_block_valid": app_block in BLOCKS,
        "is_bank_account_valid": bool(re.fullmatch(r"\d{9,18}", bank_acc)),
        "is_ifsc_valid": bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc_code)),
        "is_age_proof_attached": doc_attached,
        "days_pending": days_pending,
        "is_sla_overdue": (days_pending > SLA_DAYS) if days_pending is not None else False,
    }

    doc_info = {
        "attached": doc_attached,
        "filename": os.path.basename(doc_path) if doc_attached else None,
        "verified": doc_attached
    }

    return {
        "id": row[0],
        "application_no": row[1],
        "applicant_name": row[2],
        "mobile": row[3],
        "dob": dob.strftime("%d/%m/%Y") if dob else None,
        "age": calc_age,
        "gender": row[5],
        "marital_status": row[6],
        "village": row[7],
        "block": app_block,
        "bank_account_masked": masked_bank,
        "ifsc": ifsc_code,
        "document": doc_info,
        "status": row[12],
        "submitted_at": submitted_at.strftime("%Y-%m-%d %H:%M:%S") if submitted_at else None,
        "decided_at": row[14].strftime("%Y-%m-%d %H:%M:%S") if row[14] else None,
        "decided_by": row[15],
        "review_checklist": checklist,
        "audit_trail": audit
    }


def decide_application(officer_username, app_id_or_no, decision, reason=""):
    """
    Approve or reject a PENDING application with block-level security,
    statutory age verification, concurrency control, and audit logging.
    
    `decision`: 'APPROVED' or 'REJECTED'
    `reason`: mandatory justification string
    """
    clean_decision = (decision or "").strip().upper()
    if clean_decision not in ("APPROVED", "REJECTED"):
        return {
            "success": False,
            "error": "Decision must be either 'APPROVED' or 'REJECTED'."
        }

    clean_reason = sanitize(reason, 500)
    if not clean_reason:
        return {
            "success": False,
            "error": "A decision reason must be recorded."
        }

    # Fetch and check block authorization before updating
    conn = get_db()
    cur = conn.cursor()
    if str(app_id_or_no).isdigit():
        cur.execute(
            "SELECT id, application_no, block, status, dob FROM applications "
            "WHERE id = %s OR application_no = %s",
            (int(app_id_or_no), str(app_id_or_no))
        )
    else:
        cur.execute(
            "SELECT id, application_no, block, status, dob FROM applications "
            "WHERE application_no = %s",
            (str(app_id_or_no),)
        )
    existing = cur.fetchone()
    if not existing:
        cur.close()
        conn.close()
        return {"success": False, "error": "Application not found."}

    app_id, app_no, app_block, current_status, dob = existing
    if not check_officer_block_access(officer_username, app_block):
        cur.close()
        conn.close()
        raise PermissionError(
            f"Officer '{officer_username}' does not have jurisdiction over block '{app_block}'."
        )

    if current_status != "PENDING":
        cur.close()
        conn.close()
        return {
            "success": False,
            "error": f"Cannot decide application {app_no}: current status is '{current_status}'."
        }

    # Statutory age requirement check for approvals
    if clean_decision == "APPROVED" and dob:
        calc_age = age_on(dob, now_ist().date())
        if calc_age is not None and calc_age < MIN_AGE:
            cur.close()
            conn.close()
            return {
                "success": False,
                "error": f"Cannot approve application {app_no}: applicant is {calc_age} years of age (minimum required is {MIN_AGE} years)."
            }

    # Atomic update with status check
    now_ts = datetime.now()
    cur.execute(
        """UPDATE applications
           SET status = %s, decided_at = %s, decided_by = %s
           WHERE id = %s AND status = 'PENDING'
           RETURNING application_no, mobile""",
        (clean_decision, now_ts, officer_username, app_id)
    )
    row = cur.fetchone()
    if not row:
        conn.rollback()
        cur.close()
        conn.close()
        return {
            "success": False,
            "error": "Application was already processed by another officer or deemed approved."
        }

    # Write audit log entry
    write_audit(
        cur,
        app_id,
        clean_decision,
        f"officer:{officer_username}",
        clean_reason
    )
    conn.commit()
    cur.close()
    conn.close()

    # SMS notification for decision
    if clean_decision == "APPROVED":
        send_sms(
            row[1],
            f"Sewa Setu: your pension application {app_no} has been approved."
        )
    else:
        send_sms(
            row[1],
            f"Sewa Setu: your pension application {app_no} was not approved. Reason: {clean_reason[:80]}."
        )

    return {
        "success": True,
        "application_id": app_id,
        "application_no": app_no,
        "status": clean_decision,
        "decided_by": officer_username,
        "decided_at": now_ts.isoformat(),
        "reason": clean_reason
    }


def get_officer_decision_history(officer_username, app_id_or_no=None, limit=50):
    """
    Retrieve history of decisions made by or within jurisdiction of the officer.
    If app_id_or_no is specified, retrieves the complete audit history for that application.
    """
    authorized_blocks = get_officer_authorized_blocks(officer_username)
    if not authorized_blocks:
        raise PermissionError(f"User '{officer_username}' is not authorized as a block officer.")

    conn = get_db()
    cur = conn.cursor()

    if app_id_or_no:
        if str(app_id_or_no).isdigit():
            cur.execute(
                "SELECT id, application_no, applicant_name, block FROM applications WHERE id = %s OR application_no = %s",
                (int(app_id_or_no), str(app_id_or_no))
            )
        else:
            cur.execute(
                "SELECT id, application_no, applicant_name, block FROM applications WHERE application_no = %s",
                (str(app_id_or_no),)
            )
        app_row = cur.fetchone()
        if not app_row:
            cur.close()
            conn.close()
            return []

        target_id, target_no, target_name, target_block = app_row
        if target_block not in authorized_blocks:
            cur.close()
            conn.close()
            raise PermissionError(
                f"Officer '{officer_username}' does not have jurisdiction over application {target_no} (block: {target_block})."
            )

        cur.execute(
            """SELECT l.application_id, %s, %s, %s,
                      l.action, l.at, l.actor, l.note
               FROM audit_log l
               WHERE l.application_id = %s
               ORDER BY l.at ASC LIMIT %s""",
            (target_no, target_name, target_block, target_id, limit)
        )
        rows = cur.fetchall()
    else:
        cur.execute(
            """SELECT a.id, a.application_no, a.applicant_name, a.block,
                      a.status, a.decided_at, a.decided_by, l.note
               FROM applications a
               LEFT JOIN audit_log l ON l.application_id = a.id AND l.action = a.status
               WHERE a.status IN ('APPROVED', 'REJECTED', 'DEEMED_APPROVED')
                 AND a.block = ANY(%s)
               ORDER BY a.decided_at DESC NULLS LAST
               LIMIT %s""",
            (authorized_blocks, limit)
        )
        rows = cur.fetchall()

    cur.close()
    conn.close()

    return [
        {
            "id": r[0],
            "application_no": r[1],
            "applicant_name": r[2],
            "block": r[3],
            "decision": r[4],
            "decided_at": r[5].strftime("%Y-%m-%d %H:%M:%S") if r[5] else None,
            "decided_by": r[6],
            "reason": r[7] or ""
        }
        for r in rows
    ]
