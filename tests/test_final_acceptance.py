# -*- coding: utf-8 -*-
"""
Phase 9 — Final Integration, Acceptance Testing & Hardening Test Suite.

Authoritative verification for:
1. End-to-end Citizen AI Assistant MCP Workflow (Discovery -> OTP -> Validate -> Upload -> Submit -> Status).
2. End-to-end Officer AI Assistant MCP Workflow (Auth -> Queue -> Checklist -> Approve -> Reject -> History).
3. Statutory 15-Day Automatic Deemed Approval Acceptance (Timestamps, Concurrency, Audit trail).
4. Portal-to-MCP Cross-Interface Data Consistency & Audit Integrity.
5. Security Regression & Shared-Kiosk Privacy Protection.
"""

import os
import sys
import json
import base64
import unittest
from datetime import datetime, timedelta, date

# Ensure app directory is in Python path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import services
import mcp_citizen
import mcp_officer
import app as portal_app


class TestFinalAcceptanceWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal_app.app.config["TESTING"] = True
        cls.client = portal_app.app.test_client()

    def setUp(self):
        self.created_app_ids = []
        self.created_mobiles = []
        self._purge_test_data()

    def tearDown(self):
        self._purge_test_data()

    def _purge_test_data(self):
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM audit_log WHERE application_id IN (SELECT id FROM applications WHERE mobile LIKE '98765099%' OR application_no LIKE 'SSP26ACC%');")
        cur.execute("DELETE FROM applications WHERE mobile LIKE '98765099%' OR application_no LIKE 'SSP26ACC%';")
        cur.execute("DELETE FROM otps WHERE mobile LIKE '98765099%';")
        cur.execute("DELETE FROM portal_users WHERE mobile LIKE '98765099%';")
        for aid in getattr(self, "created_app_ids", []):
            cur.execute("DELETE FROM audit_log WHERE application_id = %s;", (aid,))
            cur.execute("DELETE FROM applications WHERE id = %s;", (aid,))
        conn.commit()
        cur.close()
        conn.close()

    # -------------------------------------------------------------------------
    # 1. Independent Citizen AI Assistant Acceptance
    # -------------------------------------------------------------------------

    def test_complete_citizen_mcp_assistant_lifecycle(self):
        """
        Verify the 11-step citizen lifecycle through the Citizen MCP server tools
        and confirm real-time visibility in the web portal.
        """
        mobile = "9876509901"
        self.created_mobiles.append(mobile)

        # 1. Discover scheme information
        scheme = mcp_citizen.get_scheme_information()
        self.assertIn("Old Age Pension", scheme["scheme_name"])
        self.assertIn("250", scheme["monthly_pension"])
        self.assertEqual(scheme["eligibility_rules"]["minimum_age"], 60)

        # 2. Discover supported blocks
        blocks = mcp_citizen.get_supported_blocks()
        self.assertIn("Sonari", blocks["ai_supported_blocks"])
        self.assertIn("Rajapara", blocks["all_portal_blocks"])

        # 3. Start application workflow
        start_res = mcp_citizen.start_application(mobile=mobile)
        self.assertTrue(start_res["success"])
        self.assertEqual(start_res["status"], "otp_sent")

        # Retrieve generated OTP directly from database
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY created_at DESC LIMIT 1;", (mobile,))
        otp_row = cur.fetchone()
        self.assertIsNotNone(otp_row)
        otp_code = otp_row[0]
        cur.close()
        conn.close()

        # 4. Verify mobile OTP
        otp_res = mcp_citizen.verify_mobile_otp(mobile=mobile, otp=otp_code)
        self.assertTrue(otp_res["verified"])
        session_token = otp_res["session_token"]
        self.assertIsNotNone(session_token)

        # 5. Validate application data
        valid_res = mcp_citizen.validate_application(
            session_token=session_token,
            applicant_name="Smt Tarulata Phukan",
            dob="10/04/1958",
            gender="Female",
            marital_status="Married",
            husband_name="Sri Bhaben Phukan",
            village="Namti Chariali",
            block="Sonari",
            bank_account="987612345678",
            ifsc="SBIN0001234"
        )
        self.assertTrue(valid_res["valid"])

        # 6. Upload age-proof document with PDF magic bytes
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Title (Age Proof) >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        upload_res = mcp_citizen.upload_age_proof(
            session_token=session_token,
            filename="age_proof_tarulata.pdf",
            file_base64=pdf_b64
        )
        self.assertTrue(upload_res["success"])
        doc_ref = upload_res["doc_reference"]

        # 7. Submit application requiring explicit applicant consent
        no_consent = mcp_citizen.submit_application(
            session_token=session_token,
            applicant_name="Smt Tarulata Phukan",
            dob="10/04/1958",
            gender="Female",
            marital_status="Married",
            husband_name="Sri Bhaben Phukan",
            village="Namti Chariali",
            block="Sonari",
            bank_account="987612345678",
            ifsc="SBIN0001234",
            doc_reference=doc_ref,
            declaration_confirmed=False
        )
        self.assertFalse(no_consent["success"])
        self.assertIn("confirmation is mandatory", no_consent["error"])

        # Confirmed submission
        submit_res = mcp_citizen.submit_application(
            session_token=session_token,
            applicant_name="Smt Tarulata Phukan",
            dob="10/04/1958",
            gender="Female",
            marital_status="Married",
            husband_name="Sri Bhaben Phukan",
            village="Namti Chariali",
            block="Sonari",
            bank_account="987612345678",
            ifsc="SBIN0001234",
            doc_reference=doc_ref,
            declaration_confirmed=True
        )
        self.assertTrue(submit_res["success"])
        app_no = submit_res["application_no"]
        self.assertTrue(app_no.startswith("SSP26"))
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE application_no = %s;", (app_no,))
        row_id = cur.fetchone()
        if row_id:
            self.created_app_ids.append(row_id[0])
        cur.close()
        conn.close()

        # 8. Retrieve application status via MCP tool
        status_mcp = mcp_citizen.get_application_status(
            session_token=session_token,
            application_no=app_no
        )
        self.assertTrue(status_mcp["found"])
        self.assertEqual(status_mcp["status"], "PENDING")
        self.assertEqual(status_mcp["application_no"], app_no)

        # 9. Verify consistency with the original web portal
        resp = self.client.post("/status", data={
            "mobile": mobile,
            "password": "10041958"
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        content = resp.get_data(as_text=True)
        self.assertIn(app_no, content)
        self.assertIn("PENDING", content)
        self.assertIn("Tarulata Phukan", content)

    # -------------------------------------------------------------------------
    # 2. Independent Officer AI Assistant Acceptance
    # -------------------------------------------------------------------------

    def test_complete_officer_mcp_assistant_workflow(self):
        """
        Verify the officer workflow through the Officer MCP server tools:
        authentication, block isolation, evidence checklist, approval with confirmation,
        rejection with reason, and persistent audit trail.
        """
        # Create 2 pending applications: one in Sonari and one in Rajapara
        app_sonari = services.submit_application(
            "9876509902",
            {
                "applicant_name": "Sri Mohan Gogoi",
                "dob": "01/01/1955",
                "gender": "Male",
                "marital_status": "Married",
                "husband_name": "",
                "husband_employer": "",
                "village": "Sonari Gaon",
                "block": "Sonari",
                "bank_account": "111122223333",
                "ifsc": "SBIN0001234"
            },
            doc_path="age_proof_test.pdf",
            actor="test_officer_acc"
        )
        self.assertTrue(app_sonari["success"])
        self.created_app_ids.append(app_sonari["application_id"])
        self.created_mobiles.append("9876509902")

        app_rajapara = services.submit_application(
            "9876509903",
            {
                "applicant_name": "Sri Biren Nath",
                "dob": "05/05/1952",
                "gender": "Male",
                "marital_status": "Married",
                "husband_name": "",
                "husband_employer": "",
                "village": "Rajapara Grant",
                "block": "Rajapara",
                "bank_account": "444455556666",
                "ifsc": "SBIN0001234"
            },
            doc_path="age_proof_test.pdf",
            actor="test_officer_acc"
        )
        self.assertTrue(app_rajapara["success"])
        self.created_app_ids.append(app_rajapara["application_id"])
        self.created_mobiles.append("9876509903")

        # 1. Officer authentication
        auth_sonari = mcp_officer.authenticate_officer("officer_sonari", "change-me-before-deploying")
        self.assertIn("officer_token", auth_sonari)
        token_sonari = auth_sonari["officer_token"]

        auth_rajapara = mcp_officer.authenticate_officer("officer_rajapara", "change-me-before-deploying")
        token_rajapara = auth_rajapara["officer_token"]

        # 2. Block Isolation: Sonari officer retrieves pending applications
        pending_sonari = mcp_officer.get_pending_applications(officer_token=token_sonari)
        sonari_nos = [a["application_no"] for a in pending_sonari["applications"]]
        self.assertIn(app_sonari["application_no"], sonari_nos)
        self.assertNotIn(app_rajapara["application_no"], sonari_nos)

        # 3. Inspect application details & verify bank account masking
        details = mcp_officer.get_application_details(app_sonari["application_no"], officer_token=token_sonari)
        self.assertTrue(details["success"])
        self.assertEqual(details["application"]["bank_account_masked"], "XXXXXX3333")
        self.assertNotIn("111122223333", str(details))

        # 4. Review authoritative compliance checklist
        checklist = mcp_officer.get_review_checklist(app_sonari["application_no"], officer_token=token_sonari)
        self.assertTrue(checklist["success"])
        self.assertEqual(checklist["overall_readiness"], "READY_FOR_DECISION")
        self.assertTrue(checklist["is_statutorily_eligible"])

        # 5. Attempt approval without human confirmation -> blocked
        unconfirmed = mcp_officer.approve_application(
            app_sonari["application_no"],
            reason="Verified documents meet standard eligibility criteria",
            officer_confirmed=False,
            officer_token=token_sonari
        )
        self.assertIn("error", unconfirmed)
        self.assertIn("Explicit officer confirmation required", unconfirmed["error"])

        # 6. Confirmed Approval
        approved = mcp_officer.approve_application(
            app_sonari["application_no"],
            reason="Age proof verified against electoral roll. Applicant eligible.",
            officer_confirmed=True,
            officer_token=token_sonari
        )
        self.assertTrue(approved["success"])
        self.assertEqual(approved["status"], "APPROVED")

        # 7. Rejection with valid reason
        rejected = mcp_officer.reject_application(
            app_rajapara["application_no"],
            reason="Applicant provided incomplete identification documentation.",
            officer_confirmed=True,
            officer_token=token_rajapara
        )
        self.assertTrue(rejected["success"])
        self.assertEqual(rejected["status"], "REJECTED")

        # 8. Decision History Verification
        history = mcp_officer.get_decision_history(app_sonari["application_no"], officer_token=token_sonari)
        self.assertTrue(history["success"])
        actions = [entry["decision"] for entry in history["history"]]
        self.assertIn("APPROVED", actions)

    # -------------------------------------------------------------------------
    # 3. Statutory 15-Day Automatic Deemed Approval Acceptance
    # -------------------------------------------------------------------------

    def test_statutory_deemed_approval_acceptance(self):
        """
        Verify the statutory 15-day deemed approval rule:
        - Applications >= 15 days overdue transition to DEEMED_APPROVED.
        - Underage applications are skipped and never deemed approved.
        - Completed decisions (APPROVED/REJECTED) are never overwritten.
        """
        conn = services.get_db()
        cur = conn.cursor()

        # 1. Overdue eligible application (16 days old, age 65)
        cur.execute("""
            INSERT INTO applications (
                application_no, applicant_name, mobile, dob, gender, marital_status,
                village, block, bank_account, ifsc, doc_path, status, submitted_at
            ) VALUES (
                'SSP26ACC0001', 'Eligible Overdue', '9876509904', '1961-01-01',
                'Female', 'Widowed', 'Village A', 'Sonari', '1234567890', 'SBIN0001234',
                'doc.pdf', 'PENDING', NOW() - INTERVAL '16 days'
            ) RETURNING id;
        """)
        overdue_id = cur.fetchone()[0]
        self.created_app_ids.append(overdue_id)
        self.created_mobiles.append("9876509904")

        # 2. Overdue underage application (16 days old, age 50)
        cur.execute("""
            INSERT INTO applications (
                application_no, applicant_name, mobile, dob, gender, marital_status,
                village, block, bank_account, ifsc, doc_path, status, submitted_at
            ) VALUES (
                'SSP26ACC0002', 'Underage Overdue', '9876509905', '1976-01-01',
                'Female', 'Married', 'Village B', 'Sonari', '1234567891', 'SBIN0001234',
                'doc.pdf', 'PENDING', NOW() - INTERVAL '16 days'
            ) RETURNING id;
        """)
        underage_id = cur.fetchone()[0]
        self.created_app_ids.append(underage_id)
        self.created_mobiles.append("9876509905")

        # 3. Overdue already decided application (16 days old, REJECTED by officer)
        cur.execute("""
            INSERT INTO applications (
                application_no, applicant_name, mobile, dob, gender, marital_status,
                village, block, bank_account, ifsc, doc_path, status, submitted_at, decided_at, decided_by
            ) VALUES (
                'SSP26ACC0003', 'Already Rejected', '9876509906', '1961-01-01',
                'Male', 'Married', 'Village C', 'Sonari', '1234567892', 'SBIN0001234',
                'doc.pdf', 'REJECTED', NOW() - INTERVAL '16 days', NOW() - INTERVAL '2 days', 'officer_sonari'
            ) RETURNING id;
        """)
        decided_id = cur.fetchone()[0]
        self.created_app_ids.append(decided_id)
        self.created_mobiles.append("9876509906")

        conn.commit()
        cur.close()
        conn.close()

        # Run deemed approval processing cycle
        result = services.process_deemed_approvals(batch_size=50, actor="system:rtps-acceptance")
        self.assertTrue(result["success"])

        # Verify statuses
        conn = services.get_db()
        cur = conn.cursor()

        cur.execute("SELECT status, decided_by FROM applications WHERE id = %s;", (overdue_id,))
        row_overdue = cur.fetchone()
        self.assertEqual(row_overdue[0], "DEEMED_APPROVED")
        self.assertEqual(row_overdue[1], "RTPS-AUTO")

        cur.execute("SELECT status FROM applications WHERE id = %s;", (underage_id,))
        row_underage = cur.fetchone()
        self.assertEqual(row_underage[0], "PENDING")  # Skipped because ineligible

        cur.execute("SELECT status FROM applications WHERE id = %s;", (decided_id,))
        row_decided = cur.fetchone()
        self.assertEqual(row_decided[0], "REJECTED")  # Never overwritten

        # Audit log traceability
        cur.execute("SELECT action, actor, note FROM audit_log WHERE application_id = %s;", (overdue_id,))
        audit_entry = cur.fetchone()
        self.assertIsNotNone(audit_entry)
        self.assertEqual(audit_entry[0], "DEEMED_APPROVED")
        self.assertIn("Right to Public Services Act", audit_entry[2])

        cur.close()
        conn.close()

    # -------------------------------------------------------------------------
    # 4. Security & Privacy Hardening Acceptance
    # -------------------------------------------------------------------------

    def test_shared_kiosk_privacy_and_anti_cache_headers(self):
        """
        Verify that citizen kiosk endpoints enforce strict anti-cache headers
        and that session logout completely invalidates stored session data.
        """
        endpoints = ["/apply", "/status"]
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, 200)
            cc = resp.headers.get("Cache-Control", "")
            self.assertIn("no-store", cc)
            self.assertIn("no-cache", cc)

        # Session logout purging
        with self.client.session_transaction() as sess:
            sess["mobile"] = "9876500001"
            sess["verified"] = True

        logout_resp = self.client.get("/logout", follow_redirects=True)
        self.assertEqual(logout_resp.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertNotIn("mobile", sess)
            self.assertNotIn("verified", sess)


if __name__ == "__main__":
    unittest.main()
