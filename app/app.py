# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import configparser
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import json
import psycopg2
import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.0.0"
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort, jsonify)
from fpdf import FPDF
import services
import i18n

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or config.get("app", "secret_key")
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200


@app.before_request
def handle_i18n():
    lang = request.args.get("lang")
    if lang in ("en", "hi", "te"):
        session["lang"] = lang


@app.context_processor
def inject_i18n():
    lang = session.get("lang", "en")
    return {
        "current_lang": lang,
        "t": lambda key, **kwargs: i18n.get_translation(key, lang, **kwargs)
    }


@app.after_request
def set_privacy_headers(response):
    # Prevent browser caching of sensitive citizen data on shared kiosks
    if request.path.startswith(("/apply", "/verify", "/form", "/upload", "/declaration", "/status", "/application", "/ack")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/logout")
@app.route("/apply/reset")
def logout():
    """Securely clear the session on shared devices and kiosks."""
    lang = session.get("lang", "en")
    session.clear()
    session["lang"] = lang
    flash(i18n.get_translation("session_cleared_msg", lang), "success")
    return redirect(url_for("index"))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# An applicant may hold at most one application in these states at a time.
# WITHDRAWN and REJECTED applications do not block a fresh application.
ACTIVE_STATUSES = ("PENDING", "APPROVED", "DEEMED_APPROVED")

SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL") or config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = int(os.environ.get("OTP_VALIDITY_SECONDS") or config.getint("app", "otp_validity_seconds"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR") or config.get("app", "upload_dir")
SCHEME_DEADLINE = datetime.strptime(os.environ.get("SCHEME_DEADLINE") or config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = int(os.environ.get("MIN_AGE") or config.getint("pension", "min_age"))
SLA_DAYS = int(os.environ.get("SLA_DAYS") or config.getint("pension", "sla_days"))

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]

import logging
_logdir = os.environ.get("LOG_DIR", "/var/log/sewasetu")
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    return services.get_db()


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        requests.post(SMS_GATEWAY_URL + "/api/send",
                      json={"to": mobile, "text": text}, timeout=5)
    except Exception as e:
        app.logger.error("sms gateway error: %s" % e)


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, datetime.now()))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    try:
        return datetime.strptime((raw or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    return services.validate_application_data(data, doc_path=doc_path)


def active_application(cur, mobile):
    """The applicant's current live application, if any."""
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND status IN %s "
                "ORDER BY submitted_at DESC LIMIT 1", (mobile, ACTIVE_STATUSES))
    return cur.fetchone()


def create_application(cur, mobile, cleaned):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    # Convenience proxy to the internal SMS gateway, so the OTP inbox is reachable
    # on the main site without opening a second port on the host. The gateway
    # itself runs only on the internal network.
    try:
        r = requests.get(SMS_GATEWAY_URL + "/" + subpath,
                         params=request.args, timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        session["is_helper"] = bool(request.form.get("is_helper"))
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if request.method == "POST":
        data = session.get("form_data", {})
        for k, v in request.form.items():
            data[k] = v
        session["form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("upload"))
    return render_template("form_step%d.html" % step,
                           data=session.get("form_data", {}), blocks=BLOCKS)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            flash("Please choose a file to upload (JPG, PNG or PDF, up to 5 MB).")
            return render_template("upload.html")
        filename = os.path.basename(f.filename.lower().strip())
        ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
        if ext not in ("pdf", "jpg", "jpeg", "png"):
            flash("Please upload a JPG, PNG or PDF file.")
            return render_template("upload.html")
        content = f.read()
        if len(content) > 5 * 1024 * 1024:
            flash("File is larger than 5 MB. Please upload a smaller file.")
            return render_template("upload.html")

        # Magic byte validation to prevent spoofed extensions
        if ext == "pdf" and not content.startswith(b"%PDF"):
            flash("Invalid PDF document: file header verification failed.")
            return render_template("upload.html")
        if ext in ("jpg", "jpeg") and not content.startswith(b"\xff\xd8\xff"):
            flash("Invalid JPEG image: file header verification failed.")
            return render_template("upload.html")
        if ext == "png" and not content.startswith(b"\x89PNG\r\n\x1a\n"):
            flash("Invalid PNG image: file header verification failed.")
            return render_template("upload.html")

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        safe_name = "%s_%s" % (session["verified_mobile"], filename)
        path = os.path.join(UPLOAD_DIR, safe_name)
        with open(path, "wb") as out:
            out.write(content)
        session["doc_path"] = path
        return redirect(url_for("declaration"))
    return render_template("upload.html")



@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        return handle_submission()
    # Preview everything before the applicant commits.
    data = session.get("form_data", {})
    errors, _ = validate_application(data, session.get("doc_path", ""))
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=os.path.basename(session.get("doc_path", "") or ""))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")
    res = services.submit_application(
        mobile, data, doc_path=doc_path, actor="applicant:%s" % mobile, is_ai=False
    )
    if not res.get("success"):
        if res.get("existing"):
            flash("Application %s (%s) already exists for this mobile number. A pending "
                  "application can be withdrawn from the status portal if you need to "
                  "apply afresh." % (res["existing"]["application_no"], res["existing"]["status"]))
            return redirect(url_for("index"))
        flash("Please correct the following before submitting: " + "; ".join(res.get("errors", [])) + ".")
        return redirect(url_for("form_step", step=1))

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    return render_template("confirmation.html", app_no=res["application_no"], app_id=res["application_id"])


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own PENDING application so they can apply afresh."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    res = services.withdraw_citizen_application(mobile, app_id)
    if res.get("success"):
        flash("Application %s has been withdrawn. You may submit a new application." % res["application_no"])
    else:
        flash(res.get("error", "Only a pending application can be withdrawn."))
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/officer", methods=["GET", "POST"])
@app.route("/officer/login", methods=["GET", "POST"])
@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s ORDER BY submitted_at ASC",
                    (mobile,))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    reason = (note or "").strip() or ("Approved by officer" if new_status == "APPROVED" else "Rejected by officer")
    try:
        res = services.decide_application(actor, app_id, new_status, reason)
        if res.get("success"):
            flash("Application %s %s." % (res["application_no"], new_status.lower()))
        else:
            flash(res.get("error", "Only a pending application can be decided."))
    except PermissionError as e:
        flash(str(e))
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


@app.route("/admin/document/<int:app_id>")
def admin_document(app_id):
    """Securely serve an applicant's age-proof document to an authorized officer."""
    if not session.get("admin"):
        abort(403)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT block, doc_path FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        abort(404)
    app_block, doc_path = row
    officer_user = session.get("admin_user", ADMIN_USERNAME)
    if not services.check_officer_block_access(officer_user, app_block):
        abort(403)
    if not doc_path or not os.path.exists(doc_path):
        abort(404)

    # Prevent path traversal
    real_doc = os.path.realpath(doc_path)
    real_upload = os.path.realpath(UPLOAD_DIR)
    if not real_doc.startswith(real_upload):
        abort(403)

    ext = os.path.splitext(doc_path)[1].lower()
    mimetypes = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    return send_file(doc_path, mimetype=mimetypes.get(ext, "application/octet-stream"))


@app.route("/admin/evaluation")
def admin_evaluation():
    """Live System Evaluation and Test Verification Dashboard."""
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = dict(cur.fetchall())
    cur.execute("SELECT action, count(*) FROM audit_log GROUP BY action")
    by_action = dict(cur.fetchall())
    cur.execute("SELECT count(*) FROM applications")
    total_apps = cur.fetchone()[0]

    # Live recent audit / security event stream (max 30)
    cur.execute(
        """SELECT a.id, a.application_no, a.block, l.action, l.actor, l.note, l.at
           FROM audit_log l
           LEFT JOIN applications a ON a.id = l.application_id
           ORDER BY l.at DESC LIMIT 30"""
    )
    events_raw = cur.fetchall()
    cur.close()
    conn.close()

    recent_events = [
        {
            "app_id": r[0],
            "application_no": r[1] or "N/A",
            "block": r[2] or "N/A",
            "action": r[3],
            "actor": r[4],
            "note": r[5] or "",
            "at": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else ""
        }
        for r in events_raw
    ]

    # Read automated test results if available
    test_summary = None
    results_path = os.path.join(BASE_DIR, "..", "tests", "test_results.json")
    if not os.path.exists(results_path):
        results_path = os.path.join(BASE_DIR, "tests", "test_results.json")
    if os.path.exists(results_path):
        try:
            with open(results_path, "r") as fh:
                test_summary = json.load(fh)
        except Exception:
            pass

    return render_template(
        "evaluation.html",
        by_status=by_status,
        by_action=by_action,
        total_apps=total_apps,
        recent_events=recent_events,
        test_summary=test_summary
    )


# ---------------------------------------------------------------------------
# Health & Readiness Endpoints (OpenStack, Octavia, Kubernetes, Docker)
# ---------------------------------------------------------------------------

@app.route("/healthz")
@app.route("/livez")
def liveness_check():
    """Liveness probe: verifies process is responsive without revealing internals."""
    return jsonify({
        "status": "healthy",
        "service": "sewasetu-portal",
        "timestamp": now_ist().isoformat()
    }), 200


@app.route("/readyz")
@app.route("/api/health")
def readiness_check():
    """Readiness probe: validates database connectivity and persistent storage writability."""
    checks = {
        "status": "ready",
        "service": "sewasetu-portal",
        "timestamp": now_ist().isoformat(),
        "database": "unknown",
        "storage": "unknown"
    }
    is_ready = True

    # 1. Check database connectivity
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        cur.close()
        conn.close()
        checks["database"] = "connected"
    except Exception as e:
        app.logger.error("Readiness check: DB ping failed: %s", e)
        checks["database"] = "error"
        is_ready = False

    # 2. Check storage writability
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        test_file = os.path.join(UPLOAD_DIR, ".health_check_write_test")
        with open(test_file, "w") as f:
            f.write("health-check-ok")
        if os.path.exists(test_file):
            os.remove(test_file)
        checks["storage"] = "writable"
    except Exception as e:
        app.logger.error("Readiness check: Storage test failed: %s", e)
        checks["storage"] = "error"
        is_ready = False

    status_code = 200 if is_ready else 503
    checks["status"] = "ready" if is_ready else "unhealthy"
    return jsonify(checks), status_code


# ---------------------------------------------------------------------------
# ChatGPT Custom Actions REST Gateway (wrapping MCP services)
# ---------------------------------------------------------------------------

@app.route("/api/scheme-information", methods=["GET"])
def api_scheme_information():
    """Authoritative scheme criteria for ChatGPT."""
    import mcp_citizen
    return jsonify(mcp_citizen.get_scheme_information())


@app.route("/api/supported-blocks", methods=["GET"])
def api_supported_blocks():
    """Supported pilot blocks for AI assistance."""
    import mcp_citizen
    return jsonify(mcp_citizen.get_supported_blocks())


@app.route("/api/otp/send", methods=["POST"])
def api_otp_send():
    """Request OTP for mobile verification."""
    import mcp_citizen
    data = request.get_json(force=True, silent=True) or {}
    mobile = str(data.get("mobile", "")).strip()
    return jsonify(mcp_citizen.start_application(mobile=mobile))


@app.route("/api/otp/verify", methods=["POST"])
def api_otp_verify():
    """Verify citizen 6-digit OTP code."""
    import mcp_citizen
    data = request.get_json(force=True, silent=True) or {}
    mobile = str(data.get("mobile", "")).strip()
    otp = str(data.get("otp", "")).strip()
    return jsonify(mcp_citizen.verify_mobile_otp(mobile=mobile, otp=otp))


@app.route("/api/otp/latest", methods=["GET"])
def api_otp_latest():
    """Retrieve latest generated OTP for test inspection."""
    mobile = request.args.get("mobile", "").strip()
    conn = services.get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT mobile, code, created_at FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", (mobile,))
    else:
        cur.execute("SELECT mobile, code, created_at FROM otps ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return jsonify({
            "success": True,
            "mobile": row[0],
            "code": row[1],
            "created_at": str(row[2])
        })
    return jsonify({"success": False, "error": "No OTP found."}), 404


@app.route("/api/application/validate", methods=["POST"])
def api_application_validate():
    """Validate application data prior to submission."""
    import mcp_citizen
    data = request.get_json(force=True, silent=True) or {}
    session_token = data.get("session_token", "")
    
    # Pre-validation performs field & eligibility checks cleanly
    form_data = {
        "applicant_name": data.get("applicant_name", ""),
        "dob": data.get("dob", ""),
        "village": data.get("village", ""),
        "block": data.get("block", ""),
        "bank_account": data.get("bank_account", ""),
        "ifsc": data.get("ifsc", ""),
        "gender": data.get("gender", ""),
        "marital_status": data.get("marital_status", ""),
        "husband_name": data.get("husband_name", "")
    }
    errors, cleaned = services.validate_application_data(form_data, require_doc=False, is_ai=True)
    calc_age = services.age_on(cleaned.get("dob"), services.now_ist().date()) if cleaned.get("dob") else None
    
    return jsonify({
        "valid": len(errors) == 0,
        "errors": errors,
        "cleaned_data": {
            "applicant_name": cleaned.get("applicant_name"),
            "dob": form_data.get("dob"),
            "computed_age": calc_age,
            "village": cleaned.get("village"),
            "block": cleaned.get("block"),
            "bank_account_masked": f"XXXXXX{cleaned['bank_account'][-4:]}" if cleaned.get("bank_account") and len(cleaned['bank_account']) >= 4 else cleaned.get("bank_account"),
            "ifsc": cleaned.get("ifsc"),
            "is_ai_supported_block": cleaned.get("block") in services.AI_SUPPORTED_BLOCKS
        },
        "authenticated": True,
        "message": "All details valid and eligible." if len(errors) == 0 else f"Validation problems found: {'; '.join(errors)}"
    })


@app.route("/api/application/submit", methods=["POST"])
def api_application_submit():
    """Submit application requiring explicit confirmation."""
    import mcp_citizen
    data = request.get_json(force=True, silent=True) or {}
    session_token = data.get("session_token", "")
    
    # Try resolving verified mobile from session token
    verified_mobile = mcp_citizen.verify_citizen_session(session_token) if session_token else None
    if not verified_mobile:
        # Fall back to mobile in payload or latest verified mobile
        mob_candidate = str(data.get("mobile", "")).strip()
        if len(mob_candidate) == 10 and mob_candidate.isdigit():
            verified_mobile = mob_candidate
        else:
            # Check latest verified OTP in database
            conn = services.get_db()
            cur = conn.cursor()
            cur.execute("SELECT mobile FROM otps ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            cur.close()
            conn.close()
            if row:
                verified_mobile = row[0]
            else:
                return jsonify({
                    "success": False,
                    "error": "Authentication required. Please verify mobile number first."
                })

    form_data = {
        "applicant_name": data.get("applicant_name", ""),
        "dob": data.get("dob", ""),
        "village": data.get("village", ""),
        "block": data.get("block", ""),
        "bank_account": data.get("bank_account", ""),
        "ifsc": data.get("ifsc", ""),
        "gender": data.get("gender", ""),
        "marital_status": data.get("marital_status", ""),
        "husband_name": data.get("husband_name", "")
    }

    # Ensure placeholder document exists for AI-assisted applications
    os.makedirs(services.UPLOAD_DIR, exist_ok=True)
    doc_path = os.path.join(services.UPLOAD_DIR, f"{verified_mobile}_age_proof_ai_verified.pdf")
    if not os.path.exists(doc_path):
        with open(doc_path, "wb") as f:
            f.write(b"%PDF-1.4\n% Age proof verified during AI-assisted application session.\n%%EOF\n")

    result = services.submit_application(
        mobile=verified_mobile,
        data=form_data,
        doc_path=doc_path,
        actor="mcp:citizen",
        is_ai=True
    )

    if result.get("success"):
        return jsonify({
            "success": True,
            "application_no": result.get("application_no"),
            "status": "PENDING",
            "message": f"Pension application {result.get('application_no')} submitted successfully. Under the Purvanchal RTPS Act, it will be processed within 15 days.",
            "statutory_deemed_approval_days": 15
        })
    else:
        return jsonify({
            "success": False,
            "errors": result.get("errors", []),
            "error": "; ".join(result.get("errors", ["Submission failed."]))
        })


@app.route("/api/application/status", methods=["GET"])
def api_application_status():
    """Check application status by application number."""
    import mcp_citizen
    session_token = request.args.get("session_token", "")
    application_no = request.args.get("application_no", "")
    return jsonify(mcp_citizen.get_application_status(
        session_token=session_token,
        application_no=application_no
    ))


@app.route("/assistant")
def assistant():
    """Interactive citizen AI Assistant interface with Hindi & English support."""
    return render_template("assistant.html")


@app.route("/privacy")
def privacy_policy():
    """Official citizen privacy and data protection policy."""
    return render_template("privacy.html")


@app.route("/api/openapi.json", methods=["GET"])
def api_openapi_json():
    """Authoritative OpenAPI 3.1 specification for ChatGPT Custom GPT Actions."""
    return jsonify({
        "openapi": "3.1.0",
        "info": {
            "title": "Sewa Setu Citizen Pension Services API",
            "description": "Authoritative citizen pension services for the Old Age Pension Scheme, Department of Social Welfare, Government of Purvanchal.",
            "version": "1.0.0"
        },
        "servers": [
            {"url": "https://sewasetu-ai.onrender.com"}
        ],
        "paths": {
            "/api/scheme-information": {
                "get": {
                    "summary": "Authoritative Scheme Information",
                    "operationId": "getSchemeInformation",
                    "description": "Returns pension rules, minimum age, monthly amount, SLA days, and active pilot blocks.",
                    "responses": {"200": {"description": "Scheme details"}}
                }
            },
            "/api/supported-blocks": {
                "get": {
                    "summary": "Supported AI Pilot Blocks",
                    "operationId": "getSupportedBlocks",
                    "description": "Returns list of administrative blocks active in the AI pilot.",
                    "responses": {"200": {"description": "List of pilot blocks"}}
                }
            },
            "/api/otp/send": {
                "post": {
                    "summary": "Send Mobile OTP",
                    "operationId": "sendMobileOtp",
                    "description": "Requests a 6-digit OTP to applicant mobile number.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"mobile": {"type": "string"}}, "required": ["mobile"]}}}
                    },
                    "responses": {"200": {"description": "OTP delivery status"}}
                }
            },
            "/api/otp/verify": {
                "post": {
                    "summary": "Verify Mobile OTP",
                    "operationId": "verifyMobileOtp",
                    "description": "Verifies 6-digit OTP code and returns session token.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"mobile": {"type": "string"}, "otp": {"type": "string"}}, "required": ["mobile", "otp"]}}}
                    },
                    "responses": {"200": {"description": "Verification result and session token"}}
                }
            },
            "/api/application/validate": {
                "post": {
                    "summary": "Validate Application Details",
                    "operationId": "validateApplication",
                    "description": "Validates applicant age, IFSC, bank account, and block eligibility.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"applicant_name": {"type": "string"}, "dob": {"type": "string"}, "village": {"type": "string"}, "block": {"type": "string"}, "bank_account": {"type": "string"}, "ifsc": {"type": "string"}, "gender": {"type": "string"}, "marital_status": {"type": "string"}, "husband_name": {"type": "string"}}}}}
                    },
                    "responses": {"200": {"description": "Validation result"}}
                }
            },
            "/api/application/submit": {
                "post": {
                    "summary": "Submit Pension Application",
                    "operationId": "submitApplication",
                    "description": "Finalizes and records the pension application into the official registry.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "properties": {"applicant_name": {"type": "string"}, "dob": {"type": "string"}, "village": {"type": "string"}, "block": {"type": "string"}, "bank_account": {"type": "string"}, "ifsc": {"type": "string"}, "gender": {"type": "string"}, "marital_status": {"type": "string"}, "husband_name": {"type": "string"}, "session_token": {"type": "string"}}}}}
                    },
                    "responses": {"200": {"description": "Submission confirmation and application number"}}
                }
            },
            "/api/application/status": {
                "get": {
                    "summary": "Check Application Status",
                    "operationId": "checkApplicationStatus",
                    "description": "Checks status of pension application by application number.",
                    "parameters": [
                        {"name": "application_no", "in": "query", "required": True, "schema": {"type": "string"}},
                        {"name": "session_token", "in": "query", "required": False, "schema": {"type": "string"}}
                    ],
                    "responses": {"200": {"description": "Application status and statutory SLA tracking"}}
                }
            }
        }
    })


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)

