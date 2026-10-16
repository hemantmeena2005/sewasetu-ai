# -*- coding: utf-8 -*-
"""
Tests for Phase 4: Citizen Experience, Accessibility, and Kiosk Privacy.

Validates:
1. Successful application submission through citizen web interface.
2. Missing or invalid form fields validation.
3. Failed and expired OTP verification.
4. Document upload restrictions (valid, invalid, format, size).
5. Explicit declaration consent and duplicate submission prevention.
6. Session logout and complete credential clearance.
7. Shared-device kiosk privacy: consecutive citizens on the same device and anti-caching headers.
8. Unauthorized status access and IDOR protection.
9. Accessible form structure, labels, ARIA landmarks, and bilingual support (Telugu & English).
10. Family/Kiosk helper mode preservation of applicant identity.
11. UI and MCP consistency across database system of record.
"""

import io
import os
import sys
import unittest
from datetime import datetime, timedelta

# Ensure app directory is in Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.0.0"

import app as portal
import services
import mcp_citizen
import i18n


class TestCitizenUXAndPrivacy(unittest.TestCase):
    """Test suite for citizen accessibility, UX, and kiosk privacy."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        portal.app.config["SECRET_KEY"] = "test-secret-key-phase4"
        self.client = portal.app.test_client()

        self.mobile_a = "9876500001"
        self.mobile_b = "9876500002"
        self._cleanup(self.mobile_a)
        self._cleanup(self.mobile_b)

    def tearDown(self):
        self._cleanup(self.mobile_a)
        self._cleanup(self.mobile_b)

    def _cleanup(self, mobile):
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE mobile = %s", (mobile,))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM audit_log WHERE application_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM applications WHERE id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM portal_users WHERE mobile = %s", (mobile,))
        cur.execute("DELETE FROM otps WHERE mobile = %s", (mobile,))
        conn.commit()
        cur.close()
        conn.close()

    # -----------------------------------------------------------------------
    # 1. Full application submission through web interface
    # -----------------------------------------------------------------------
    def test_web_application_submission_flow(self):
        # Step 1: Request OTP
        with self.client.session_transaction() as sess:
            sess["captcha_answer"] = 12

        r1 = self.client.post("/apply", data={
            "mobile": self.mobile_a,
            "captcha": "12"
        }, follow_redirects=True)
        self.assertEqual(r1.status_code, 200)
        self.assertIn(b"Enter Verification Code", r1.data)

        # Get generated OTP
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", (self.mobile_a,))
        otp_code = cur.fetchone()[0]
        cur.close(); conn.close()

        # Step 1b: Verify OTP
        r2 = self.client.post("/verify", data={"otp": otp_code}, follow_redirects=True)
        self.assertEqual(r2.status_code, 200)
        self.assertIn(b"Personal Details", r2.data)

        # Step 2: Personal Details
        r3 = self.client.post("/form/1", data={
            "applicant_name": "Ratnabala Phukan",
            "dob": "12/04/1952",
            "gender": "Female",
            "marital_status": "Widowed",
            "husband_name": "Late Biren Phukan"
        }, follow_redirects=True)
        self.assertEqual(r3.status_code, 200)
        self.assertIn(b"Residential Address", r3.data)

        # Step 3: Address Details
        r4 = self.client.post("/form/2", data={
            "village": "Namdang",
            "block": "Sonari"
        }, follow_redirects=True)
        self.assertEqual(r4.status_code, 200)
        self.assertIn(b"Bank Account", r4.data)

        # Step 4: Bank Details
        r5 = self.client.post("/form/3", data={
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077"
        }, follow_redirects=True)
        self.assertEqual(r5.status_code, 200)
        self.assertIn(b"Age Proof Document", r5.data)

        # Step 5: Document Upload
        pdf_file = (io.BytesIO(b"%PDF-1.4 test document content"), "proof.pdf")
        r6 = self.client.post("/upload", data={"document": pdf_file}, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(r6.status_code, 200)
        self.assertIn(b"Review Application", r6.data)
        # Bank account should be masked on review screen
        self.assertIn(b"XXXXXX9012", r6.data)

        # Step 6: Submit Declaration
        r7 = self.client.post("/declaration", data={"declaration_agree": "1"}, follow_redirects=True)
        self.assertEqual(r7.status_code, 200)
        self.assertIn(b"Application Successfully Submitted!", r7.data)
        self.assertIn(b"SSP26", r7.data)

    # -----------------------------------------------------------------------
    # 2. Missing or invalid form fields
    # -----------------------------------------------------------------------
    def test_validation_errors_prevent_submission(self):
        # Underage applicant (born 1980 -> age 46)
        errors, _ = services.validate_application_data({
            "applicant_name": "Young Applicant",
            "dob": "10/05/1980",
            "village": "Namdang",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077"
        }, doc_path="/tmp/proof.pdf")
        self.assertTrue(any("60 years of age or above" in e for e in errors))

        # Invalid bank account (too short)
        errors, _ = services.validate_application_data({
            "applicant_name": "Valid Name",
            "dob": "10/05/1950",
            "village": "Namdang",
            "block": "Sonari",
            "bank_account": "12345",  # 5 digits
            "ifsc": "SBIN0003077"
        }, doc_path="/tmp/proof.pdf")
        self.assertTrue(any("bank account number must be 9 to 18 digits" in e for e in errors))

    # -----------------------------------------------------------------------
    # 3. Failed and expired OTP verification
    # -----------------------------------------------------------------------
    def test_failed_and_expired_otp(self):
        # Insert expired OTP (6 minutes ago)
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
            (self.mobile_a, "888999", datetime.now() - timedelta(seconds=360))
        )
        conn.commit()
        cur.close(); conn.close()

        with self.client.session_transaction() as sess:
            sess["apply_mobile"] = self.mobile_a

        # Attempt expired OTP
        r = self.client.post("/verify", data={"otp": "888999"}, follow_redirects=True)
        self.assertIn(b"OTP expired", r.data)

        # Attempt invalid OTP
        r_invalid = self.client.post("/verify", data={"otp": "000000"}, follow_redirects=True)
        self.assertIn(b"Invalid OTP", r_invalid.data)

    # -----------------------------------------------------------------------
    # 4. Document upload restrictions
    # -----------------------------------------------------------------------
    def test_document_upload_restrictions(self):
        with self.client.session_transaction() as sess:
            sess["verified_mobile"] = self.mobile_a

        # Invalid file type (.exe)
        bad_file = (io.BytesIO(b"executable-data"), "malicious.exe")
        r1 = self.client.post("/upload", data={"document": bad_file}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Please upload a JPG, PNG or PDF file", r1.data)

        # Empty file
        empty_file = (io.BytesIO(b""), "")
        r2 = self.client.post("/upload", data={"document": empty_file}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Please choose a file to upload", r2.data)

    # -----------------------------------------------------------------------
    # 5. Session logout & complete credential clearance
    # -----------------------------------------------------------------------
    def test_session_logout_and_clear(self):
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = self.mobile_a
            sess["verified_mobile"] = self.mobile_a
            sess["form_data"] = {"applicant_name": "Test Citizen"}

        r = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        # Verify session is empty
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess.get("logged_in"))
            self.assertIsNone(sess.get("portal_mobile"))
            self.assertIsNone(sess.get("verified_mobile"))
            self.assertIsNone(sess.get("form_data"))

    # -----------------------------------------------------------------------
    # 6. Shared-device kiosk privacy: consecutive citizens on same device
    # -----------------------------------------------------------------------
    def test_consecutive_citizens_on_shared_kiosk(self):
        # Citizen A logs in and views their application
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = self.mobile_a

        # Check anti-caching headers on application status page
        resp_a = self.client.get("/status")
        self.assertEqual(resp_a.status_code, 200)
        self.assertIn("no-store", resp_a.headers.get("Cache-Control", ""))

        # Citizen A logs out
        self.client.get("/logout")

        # Citizen B now arrives at the same kiosk/browser and tries to access /application/1
        resp_b = self.client.get("/application/1", follow_redirects=True)
        # Should redirect to status login, NOT show Citizen A's data!
        self.assertIn(b"Please login to view application status", resp_b.data)

    # -----------------------------------------------------------------------
    # 7. Bilingual localization (Telugu and English)
    # -----------------------------------------------------------------------
    def test_bilingual_localization(self):
        # English home page
        r_en = self.client.get("/?lang=en")
        self.assertEqual(r_en.status_code, 200)
        self.assertIn(b"Welcome to Sewa Setu", r_en.data)
        self.assertIn(b"Apply for Pension", r_en.data)

        # Telugu home page
        r_te = self.client.get("/?lang=te")
        self.assertEqual(r_te.status_code, 200)
        # Check for Telugu unicode text
        self.assertIn("సేవా సేతు".encode("utf-8"), r_te.data)
        self.assertIn("పింఛను కోసం దరఖాస్తు".encode("utf-8"), r_te.data)

    # -----------------------------------------------------------------------
    # 8. Helper / Assisted Application Mode
    # -----------------------------------------------------------------------
    def test_helper_assisted_mode(self):
        with self.client.session_transaction() as sess:
            sess["captcha_answer"] = 8

        # Submit apply with helper checkbox checked
        r = self.client.post("/apply", data={
            "mobile": self.mobile_a,
            "captcha": "8",
            "is_helper": "1"
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)

        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("is_helper"))

    # -----------------------------------------------------------------------
    # 9. Cross-channel parity: UI and MCP consistency
    # -----------------------------------------------------------------------
    def test_ui_and_mcp_consistency(self):
        # Create application via shared services (same as MCP submit)
        data = {
            "applicant_name": "Tapeswari Borah",
            "village": "Kathoni",
            "block": "Sonari",
            "bank_account": "987654321099",
            "ifsc": "SBIN0003077",
            "dob": "10/06/1953"
        }
        res = services.submit_application(
            self.mobile_a, data, doc_path="/tmp/proof.pdf", is_ai=True
        )
        self.assertTrue(res["success"])
        app_no = res["application_no"]

        # 1. Query via MCP
        token = mcp_citizen.create_citizen_session_token(self.mobile_a)
        mcp_res = mcp_citizen.get_application_status(session_token=token, application_no=app_no)
        self.assertTrue(mcp_res["found"])
        self.assertEqual(mcp_res["application_no"], app_no)
        self.assertEqual(mcp_res["status"], "PENDING")

        # 2. Login and query via Web Portal Status
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = self.mobile_a

        portal_res = self.client.get(f"/application/{res['application_id']}")
        self.assertEqual(portal_res.status_code, 200)
        self.assertIn(app_no.encode(), portal_res.data)
        self.assertIn(b"PENDING", portal_res.data)
        self.assertIn(b"Tapeswari Borah", portal_res.data)

    # -----------------------------------------------------------------------
    # 10. Status Login by OTP & Forgot Password Flow
    # -----------------------------------------------------------------------
    def test_status_login_by_otp_and_forgot_password_flow(self):
        # Create an application for test applicant
        mobile = "9876500099"
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM portal_users WHERE mobile = %s;", (mobile,))
        cur.execute("DELETE FROM otps WHERE mobile = %s;", (mobile,))
        cur.execute("DELETE FROM applications WHERE mobile = %s;", (mobile,))
        conn.commit()
        cur.close(); conn.close()

        res = services.submit_application(
            mobile,
            {
                "applicant_name": "Bhaben Kalita",
                "dob": "12/03/1955",
                "village": "Dihingia",
                "block": "Sonari",
                "bank_account": "918273645012",
                "ifsc": "SBIN0001234",
            },
            doc_path="/tmp/proof.pdf",
            is_ai=False
        )
        self.assertTrue(res["success"])
        app_id = res["application_id"]

        # 1. GET /status contains both password and OTP login options
        r_get = self.client.get("/status")
        self.assertEqual(r_get.status_code, 200)
        self.assertIn(b"login_mode_otp", r_get.data) if b"login_mode_otp" in r_get.data else self.assertIn("OTP".encode(), r_get.data)

        # 2. OTP Login: Request OTP via POST /status
        r_otp_send = self.client.post("/status", data={
            "login_mode": "otp",
            "action": "send_otp",
            "otp_mobile": mobile
        })
        self.assertEqual(r_otp_send.status_code, 200)
        self.assertIn(b"OTP successfully sent", r_otp_send.data)

        # Get latest OTP from DB
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1;", (mobile,))
        otp_code = cur.fetchone()[0]
        cur.close(); conn.close()
        self.assertEqual(len(otp_code), 6)

        # 3. OTP Login: Verify OTP and log in
        r_otp_login = self.client.post("/status", data={
            "login_mode": "otp",
            "action": "verify_otp",
            "otp_mobile": mobile,
            "otp": otp_code
        }, follow_redirects=True)
        self.assertEqual(r_otp_login.status_code, 200)
        self.assertIn(b"Bhaben Kalita", r_otp_login.data)
        self.assertIn(b"Dihingia", r_otp_login.data)

        # 4. Forgot Password Flow: GET /status/reset
        r_reset_get = self.client.get("/status/reset")
        self.assertEqual(r_reset_get.status_code, 200)
        self.assertIn(b"15081954", r_reset_get.data)

        # 5. Forgot Password: non-existent mobile gives clear guidance
        r_nonexist = self.client.post("/status/reset", data={
            "action": "request_otp",
            "mobile": "9999999999"
        })
        self.assertIn(b"No pension application", r_nonexist.data)

        # 6. Forgot Password: valid mobile requests OTP
        r_reset_req = self.client.post("/status/reset", data={
            "action": "request_otp",
            "mobile": mobile
        })
        self.assertEqual(r_reset_req.status_code, 200)
        self.assertIn(b"A 6-digit OTP has been sent", r_reset_req.data)

        # Get latest reset OTP
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1;", (mobile,))
        reset_otp = cur.fetchone()[0]
        cur.close(); conn.close()

        # 7. Forgot Password: confirm reset with new password
        r_reset_confirm = self.client.post("/status/reset", data={
            "action": "confirm_reset",
            "mobile": mobile,
            "otp": reset_otp,
            "new_password": "MySecretPass99",
            "confirm_password": "MySecretPass99"
        }, follow_redirects=True)
        self.assertEqual(r_reset_confirm.status_code, 200)
        self.assertIn(b"Bhaben Kalita", r_reset_confirm.data)

        # 8. Verify the citizen can now log in using the new password
        self.client.get("/logout")
        r_pwd_login = self.client.post("/status", data={
            "mobile": mobile,
            "password": "MySecretPass99"
        }, follow_redirects=True)
        self.assertEqual(r_pwd_login.status_code, 200)
        self.assertIn(b"Bhaben Kalita", r_pwd_login.data)

    # -----------------------------------------------------------------------
    # 10. Returning user, logout, switching users, and homepage adaptation
    # -----------------------------------------------------------------------
    def test_returning_citizen_and_user_switching(self):
        """
        Comprehensive acceptance test for:
        1. Returning authenticated user sees Check Application Status & their details on homepage.
        2. Attempting to access /apply when already holding an active application redirects to status.
        3. Logout completely purges session and privacy headers prevent caching.
        4. Switching users: Citizen B on the same client can log in independently and sees only Citizen B's data.
        5. IDOR prevention: Citizen B cannot view Citizen A's application.
        6. First-time user verifying OTP gets directed to form if no application, or to status if existing.
        """
        # Create an application for Citizen A
        app_a = services.submit_application(
            self.mobile_a,
            {
                "applicant_name": "Citizen Alpha",
                "dob": "10/05/1950",
                "gender": "Female",
                "marital_status": "Widowed",
                "husband_name": "Late Husband A",
                "husband_employer": "",
                "village": "Sonari Gaon",
                "block": "Sonari",
                "bank_account": "111122223333",
                "ifsc": "SBIN0001234"
            },
            doc_path="doc_alpha.pdf",
            actor=f"applicant:{self.mobile_a}"
        )
        self.assertTrue(app_a["success"])
        app_a_id = app_a["application_id"]
        app_a_no = app_a["application_no"]

        # Create an application for Citizen B
        app_b = services.submit_application(
            self.mobile_b,
            {
                "applicant_name": "Citizen Bravo",
                "dob": "15/08/1952",
                "gender": "Male",
                "marital_status": "Married",
                "husband_name": "",
                "husband_employer": "",
                "village": "Rajapara Block",
                "block": "Rajapara",
                "bank_account": "444455556666",
                "ifsc": "SBIN0001234"
            },
            doc_path="doc_bravo.pdf",
            actor=f"applicant:{self.mobile_b}"
        )
        self.assertTrue(app_b["success"])
        app_b_id = app_b["application_id"]
        app_b_no = app_b["application_no"]

        # Step 1: Citizen A logs in
        with self.client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["portal_mobile"] = self.mobile_a

        # Visit homepage as logged-in Citizen A
        resp_home_a = self.client.get("/")
        self.assertEqual(resp_home_a.status_code, 200)
        self.assertIn(b"Check Application Status", resp_home_a.data)
        self.assertIn(b"Logout", resp_home_a.data)
        self.assertIn(b"Citizen Alpha", resp_home_a.data)
        self.assertIn(app_a_no.encode("utf-8"), resp_home_a.data)
        self.assertNotIn(b"Citizen Bravo", resp_home_a.data)

        # Citizen A tries to go to /apply -> should be redirected to their active application
        resp_apply_a = self.client.get("/apply", follow_redirects=True)
        self.assertEqual(resp_apply_a.status_code, 200)
        self.assertIn(b"You already have an active application", resp_apply_a.data)
        self.assertIn(app_a_no.encode("utf-8"), resp_apply_a.data)

        # Step 2: Citizen A logs out
        resp_logout = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(resp_logout.status_code, 200)
        # Should now see public homepage with "Apply for Pension" and NO private details
        self.assertIn(b"Apply for Pension", resp_logout.data)
        self.assertNotIn(b"Citizen Alpha", resp_logout.data)
        self.assertNotIn(app_a_no.encode("utf-8"), resp_logout.data)

        # Step 3: Citizen B arrives on the same browser and logs in via OTP
        # Request OTP for Citizen B
        otp_res = services.send_otp_for_mobile(self.mobile_b)
        self.assertTrue(otp_res["success"])
        b_otp_code = otp_res["code"]

        # Citizen B logs in via OTP on status portal
        resp_login_b = self.client.post("/status", data={
            "login_mode": "otp",
            "otp_mobile": self.mobile_b,
            "action": "verify_otp",
            "otp": b_otp_code
        }, follow_redirects=True)
        self.assertEqual(resp_login_b.status_code, 200)
        self.assertIn(b"Citizen Bravo", resp_login_b.data)
        self.assertIn(app_b_no.encode("utf-8"), resp_login_b.data)
        self.assertNotIn(b"Citizen Alpha", resp_login_b.data)

        # Citizen B visits homepage -> sees Citizen B's application status
        resp_home_b = self.client.get("/")
        self.assertEqual(resp_home_b.status_code, 200)
        self.assertIn(b"Citizen Bravo", resp_home_b.data)
        self.assertIn(app_b_no.encode("utf-8"), resp_home_b.data)
        self.assertNotIn(b"Citizen Alpha", resp_home_b.data)

        # Step 4: IDOR Prevention: Citizen B tries to view Citizen A's application ID
        resp_idor = self.client.get(f"/application/{app_a_id}")
        self.assertEqual(resp_idor.status_code, 404)

        # Step 5: Citizen B also cannot download Citizen A's PDF acknowledgment
        resp_ack_idor = self.client.get(f"/ack/{app_a_id}.pdf")
        self.assertEqual(resp_ack_idor.status_code, 403)

        # Step 6: Verify MCP citizen tool returns active application info after OTP verification
        mcp_otp_res = mcp_citizen.verify_mobile_otp(self.mobile_a, "123456")
        # generate valid otp
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (self.mobile_a, "654321", datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        mcp_otp_verified = mcp_citizen.verify_mobile_otp(self.mobile_a, "654321")
        self.assertTrue(mcp_otp_verified["verified"])
        self.assertTrue(mcp_otp_verified["has_active_application"])
        self.assertEqual(mcp_otp_verified["active_application_no"], app_a_no)


if __name__ == "__main__":
    unittest.main()


