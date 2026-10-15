# -*- coding: utf-8 -*-
"""
Tests for Sewa Setu Citizen MCP Server (Model Context Protocol).

Validates all 8 citizen tools:
1. get_scheme_information
2. get_supported_blocks
3. start_application
4. verify_mobile_otp
5. validate_application
6. upload_age_proof
7. submit_application
8. get_application_status

Plus End-to-End cross-channel verification with portal and database.
"""

import os
import sys
import base64
import unittest
from datetime import datetime, date

# Ensure app directory is in Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import services
import oauth
import mcp_citizen


class TestCitizenMCPTools(unittest.TestCase):
    """Unit and integration tests for citizen MCP server tools."""

    @classmethod
    def setUpClass(cls):
        cls.conn = services.get_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.test_mobile = "9988776655"
        self._cleanup(self.test_mobile)

    def tearDown(self):
        self._cleanup(self.test_mobile)

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
    # Tool 1: get_scheme_information
    # -----------------------------------------------------------------------
    def test_tool_get_scheme_information(self):
        info = mcp_citizen.get_scheme_information()
        self.assertIn("Old Age Pension Scheme", info["scheme_name"])
        self.assertEqual(info["monthly_pension"], "₹250 per month")
        self.assertEqual(info["eligibility_rules"]["minimum_age"], 60)
        self.assertEqual(info["statutory_sla"]["processing_days"], 15)
        self.assertIn("Sonari", info["eligibility_rules"]["ai_assisted_blocks_pilot"])
        self.assertIn("required_documents", info)

    # -----------------------------------------------------------------------
    # Tool 2: get_supported_blocks
    # -----------------------------------------------------------------------
    def test_tool_get_supported_blocks(self):
        blocks_info = mcp_citizen.get_supported_blocks()
        self.assertIn("Sonari", blocks_info["ai_supported_blocks"])
        self.assertEqual(len(blocks_info["all_portal_blocks"]), 6)
        self.assertIn("guidance", blocks_info)

    # -----------------------------------------------------------------------
    # Tool 3 & 4: start_application & verify_mobile_otp
    # -----------------------------------------------------------------------
    def test_tool_start_and_verify_otp(self):
        # Invalid mobile
        bad_start = mcp_citizen.start_application(mobile="12345")
        self.assertFalse(bad_start["success"])

        # Valid mobile start
        start_res = mcp_citizen.start_application(mobile=self.test_mobile)
        self.assertTrue(start_res["success"])
        self.assertEqual(start_res["status"], "otp_sent")
        self.assertEqual(start_res["mobile_masked"], "XXXXXX6655")

        # Read generated OTP from database
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT code FROM otps WHERE mobile = %s ORDER BY id DESC LIMIT 1", (self.test_mobile,))
        otp_row = cur.fetchone()
        self.assertIsNotNone(otp_row)
        otp_code = otp_row[0]
        cur.close()
        conn.close()

        # Invalid OTP attempt
        verify_fail = mcp_citizen.verify_mobile_otp(mobile=self.test_mobile, otp="000000")
        self.assertFalse(verify_fail["verified"])

        # Correct OTP verification
        verify_ok = mcp_citizen.verify_mobile_otp(mobile=self.test_mobile, otp=otp_code)
        self.assertTrue(verify_ok["verified"])
        self.assertIn("session_token", verify_ok)
        self.assertEqual(verify_ok["mobile"], self.test_mobile)

        # Verify session token validity
        verified_mobile = mcp_citizen.verify_citizen_session(verify_ok["session_token"])
        self.assertEqual(verified_mobile, self.test_mobile)

    # -----------------------------------------------------------------------
    # Tool 5: validate_application
    # -----------------------------------------------------------------------
    def test_tool_validate_application(self):
        session_token = mcp_citizen.create_citizen_session_token(self.test_mobile)

        # Ineligible age (50 years old)
        res_young = mcp_citizen.validate_application(
            session_token=session_token,
            applicant_name="Bina Gogoi",
            dob="10/05/1976",
            village="Lakhipur",
            block="Sonari",
            bank_account="123456789012",
            ifsc="SBIN0003077"
        )
        self.assertFalse(res_young["valid"])
        self.assertTrue(any("60 years of age or above" in e for e in res_young["errors"]))

        # Non-AI block (Rajapara)
        res_non_ai_block = mcp_citizen.validate_application(
            session_token=session_token,
            applicant_name="Bina Gogoi",
            dob="10/05/1950",
            village="Lakhipur",
            block="Rajapara",
            bank_account="123456789012",
            ifsc="SBIN0003077"
        )
        self.assertFalse(res_non_ai_block["valid"])
        self.assertTrue(any("AI-assisted applications are currently available only for" in e for e in res_non_ai_block["errors"]))

        # Eligible 76-year old in Sonari
        res_valid = mcp_citizen.validate_application(
            session_token=session_token,
            applicant_name="Bina Gogoi",
            dob="10/05/1950",
            village="Lakhipur",
            block="Sonari",
            bank_account="123456789012",
            ifsc="SBIN0003077"
        )
        self.assertTrue(res_valid["valid"], res_valid.get("errors"))
        self.assertEqual(res_valid["cleaned_data"]["block"], "Sonari")
        self.assertEqual(res_valid["cleaned_data"]["bank_account_masked"], "XXXXXX9012")

    # -----------------------------------------------------------------------
    # Tool 6: upload_age_proof
    # -----------------------------------------------------------------------
    def test_tool_upload_age_proof(self):
        session_token = mcp_citizen.create_citizen_session_token(self.test_mobile)

        # Disallowed file format
        bad_ext = mcp_citizen.upload_age_proof(
            session_token=session_token,
            filename="malicious.exe",
            file_base64=base64.b64encode(b"not-valid").decode()
        )
        self.assertFalse(bad_ext["success"])
        self.assertIn("Unsupported file format", bad_ext["error"])

        # Corrupted PDF magic bytes
        fake_pdf = mcp_citizen.upload_age_proof(
            session_token=session_token,
            filename="proof.pdf",
            file_base64=base64.b64encode(b"NOT_A_PDF_CONTENT").decode()
        )
        self.assertFalse(fake_pdf["success"])
        self.assertIn("magic header mismatch", fake_pdf["error"])

        # Valid PDF upload
        valid_pdf_content = b"%PDF-1.4 mock pdf content for testing age proof document"
        upload_ok = mcp_citizen.upload_age_proof(
            session_token=session_token,
            filename="aadhaar_card.pdf",
            file_base64=base64.b64encode(valid_pdf_content).decode()
        )
        self.assertTrue(upload_ok["success"], upload_ok.get("error"))
        self.assertIn("doc_reference", upload_ok)
        self.assertTrue(upload_ok["doc_reference"].startswith("doc_"))

    # -----------------------------------------------------------------------
    # Tool 7 & 8: submit_application & get_application_status (End-to-End)
    # -----------------------------------------------------------------------
    def test_tool_submit_and_get_status_end_to_end(self):
        session_token = mcp_citizen.create_citizen_session_token(self.test_mobile)

        # 1. Upload valid document
        valid_pdf = b"%PDF-1.4 verified document bytes"
        upload_res = mcp_citizen.upload_age_proof(
            session_token=session_token,
            filename="age_proof.pdf",
            file_base64=base64.b64encode(valid_pdf).decode()
        )
        doc_ref = upload_res["doc_reference"]

        # 2. Submit without declaration confirmation -> must fail
        submit_no_consent = mcp_citizen.submit_application(
            session_token=session_token,
            applicant_name="Bimala Barua",
            dob="15/08/1954",
            village="Kathoni",
            block="Sonari",
            bank_account="987654321012",
            ifsc="SBIN0003077",
            doc_reference=doc_ref,
            declaration_confirmed=False
        )
        self.assertFalse(submit_no_consent["success"])
        self.assertIn("confirmation is mandatory", submit_no_consent["error"])

        # 3. Submit with explicit confirmation -> must succeed
        submit_ok = mcp_citizen.submit_application(
            session_token=session_token,
            applicant_name="Bimala Barua",
            dob="15/08/1954",
            village="Kathoni",
            block="Sonari",
            bank_account="987654321012",
            ifsc="SBIN0003077",
            doc_reference=doc_ref,
            declaration_confirmed=True,
            gender="Female",
            marital_status="Widowed"
        )
        self.assertTrue(submit_ok["success"], submit_ok.get("errors"))
        app_no = submit_ok["application_no"]
        self.assertTrue(app_no.startswith("SSP"))
        self.assertEqual(submit_ok["status"], "PENDING")

        # 4. Immediate status query via MCP
        status_res = mcp_citizen.get_application_status(
            session_token=session_token,
            application_no=app_no
        )
        self.assertTrue(status_res["success"])
        self.assertTrue(status_res["found"])
        self.assertEqual(status_res["application_no"], app_no)
        self.assertEqual(status_res["applicant_name"], "Bimala Barua")
        self.assertEqual(status_res["status"], "PENDING")
        self.assertEqual(status_res["block"], "Sonari")
        self.assertEqual(status_res["bank_account_masked"], "XXXXXX1012")

        # 5. Cross-Channel Parity Check: Check directly in existing database & portal
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, status, block FROM applications WHERE application_no = %s",
            (app_no,)
        )
        db_app = cur.fetchone()
        self.assertIsNotNone(db_app, "Application submitted via MCP must exist immediately in the database!")
        self.assertEqual(db_app[1], app_no)
        self.assertEqual(db_app[2], "Bimala Barua")
        self.assertEqual(db_app[3], self.test_mobile)
        self.assertEqual(db_app[4], "PENDING")
        self.assertEqual(db_app[5], "Sonari")

        # Verify portal user account created for citizen status portal login
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (self.test_mobile,))
        portal_user = cur.fetchone()
        self.assertIsNotNone(portal_user)
        expected_pass_hash = services.hash_password("15081954")  # DOB: 15/08/1954 -> 15081954
        self.assertEqual(portal_user[0], expected_pass_hash)

        # Verify audit log trail
        cur.execute("SELECT action, actor, note FROM audit_log WHERE application_id = %s", (db_app[0],))
        audit_rows = cur.fetchall()
        self.assertTrue(any(r[0] == "SUBMIT" and "applicant:" in r[1] for r in audit_rows))
        cur.close()
        conn.close()

        # 6. Deduplication check: repeated submission attempt fails
        repeat_submit = mcp_citizen.submit_application(
            session_token=session_token,
            applicant_name="Bimala Barua",
            dob="15/08/1954",
            village="Kathoni",
            block="Sonari",
            bank_account="987654321012",
            ifsc="SBIN0003077",
            doc_reference=doc_ref,
            declaration_confirmed=True
        )
        self.assertFalse(repeat_submit["success"])
        self.assertTrue(any("already exists" in e for e in repeat_submit.get("errors", [])))


if __name__ == "__main__":
    unittest.main()
