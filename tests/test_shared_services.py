# -*- coding: utf-8 -*-
"""
Tests for Sewa Setu Shared Backend Services.

Validates:
- Centralized eligibility validations and edge cases
- Application creation with collision-safe unique application numbers
- Concurrency control and duplicate submission prevention
- Citizen authorization and IDOR prevention
- Block-level officer authorization and review checklist
- Decision recording and audit history tracking
- AI pilot block constraints
"""

import os
import sys
import unittest
from datetime import datetime, date, timedelta

# Ensure app directory is in Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import services


class TestEligibilityAndValidation(unittest.TestCase):
    """Unit tests for scheme eligibility and form validations."""

    def test_parse_dob(self):
        d = services.parse_dob("15/04/1954")
        self.assertIsNotNone(d)
        self.assertEqual(d.year, 1954)
        self.assertEqual(d.month, 4)
        self.assertEqual(d.day, 15)

        self.assertIsNone(services.parse_dob("invalid-date"))
        self.assertIsNone(services.parse_dob(""))
        self.assertIsNone(services.parse_dob(None))

    def test_age_calculation(self):
        on_date = date(2026, 10, 15)
        # Born 1966-10-14 -> exactly 60 years and 1 day -> age 60
        self.assertEqual(services.age_on(date(1966, 10, 14), on_date), 60)
        # Born 1966-10-16 -> 59 years and 364 days -> age 59
        self.assertEqual(services.age_on(date(1966, 10, 16), on_date), 59)
        # Born 1956-01-01 -> age 70
        self.assertEqual(services.age_on(date(1956, 1, 1), on_date), 70)

    def test_check_eligibility_age_threshold(self):
        # Eligible 70-year old
        res_70 = services.check_eligibility("15/05/1956", block="Sonari", on_date=date(2026, 10, 15))
        self.assertTrue(res_70["eligible"])
        self.assertEqual(res_70["age"], 70)
        self.assertTrue(res_70["ai_supported"])

        # Ineligible 59-year old
        res_59 = services.check_eligibility("20/12/1966", block="Sonari", on_date=date(2026, 10, 15))
        self.assertFalse(res_59["eligible"])
        self.assertEqual(res_59["age"], 59)
        self.assertTrue(any("below the required 60 years" in r for r in res_59["reasons"]))

    def test_validate_application_missing_fields(self):
        errors, cleaned = services.validate_application_data({}, doc_path=None)
        self.assertTrue(any("full name is required" in e for e in errors))
        self.assertTrue(any("village is required" in e for e in errors))
        self.assertTrue(any("block is required" in e for e in errors))
        self.assertTrue(any("bank account is required" in e for e in errors))
        self.assertTrue(any("date of birth must be given" in e for e in errors))
        self.assertTrue(any("age proof document is required" in e for e in errors))

    def test_validate_application_bank_and_ifsc(self):
        # Invalid bank account length
        data = {
            "applicant_name": "Lilabati Borah",
            "village": "Kathoni",
            "block": "Sonari",
            "bank_account": "12345",  # too short
            "ifsc": "SBIN0003077",
            "dob": "10/05/1955"
        }
        errors, _ = services.validate_application_data(data, doc_path="/tmp/proof.pdf")
        self.assertTrue(any("bank account number must be 9 to 18 digits" in e for e in errors))

        # Invalid IFSC format
        data["bank_account"] = "123456789012"
        data["ifsc"] = "INVALID12"
        errors, _ = services.validate_application_data(data, doc_path="/tmp/proof.pdf")
        self.assertTrue(any("IFSC must be 11 characters" in e for e in errors))

        # Valid IFSC and bank account
        data["ifsc"] = "sbin0003077"  # lower case should be normalized
        errors, cleaned = services.validate_application_data(data, doc_path="/tmp/proof.pdf")
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["ifsc"], "SBIN0003077")
        self.assertEqual(cleaned["bank_account"], "123456789012")

    def test_ai_supported_blocks_validation(self):
        valid_data = {
            "applicant_name": "Pramila Saikia",
            "village": "Lakhipur",
            "block": "Rajapara",  # Valid departmental block, but not pilot AI block
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077",
            "dob": "10/05/1955"
        }
        # Via regular portal: passes
        errors_portal, _ = services.validate_application_data(valid_data, doc_path="/tmp/proof.pdf", is_ai=False)
        self.assertEqual(errors_portal, [])

        # Via AI assistant: fails with pilot block notification
        errors_ai, _ = services.validate_application_data(valid_data, doc_path="/tmp/proof.pdf", is_ai=True)
        self.assertTrue(any("AI-assisted applications are currently available only for" in e for e in errors_ai))


class TestApplicationLifecycleAndDatabase(unittest.TestCase):
    """Integration tests for application creation, deduplication, citizen auth, and officer workflows."""

    @classmethod
    def setUpClass(cls):
        # Ensure database is accessible
        cls.conn = services.get_db()
        cur = cls.conn.cursor()
        cur.execute("SELECT 1")
        cur.close()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        # Use a designated test mobile number that doesn't conflict with seed data
        self.test_mobile = "9999000001"
        self._cleanup_mobile(self.test_mobile)

    def tearDown(self):
        self._cleanup_mobile(self.test_mobile)

    def _cleanup_mobile(self, mobile):
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

    def test_application_creation_and_unique_number(self):
        data = {
            "applicant_name": "Hemaprabha Gogoi",
            "village": "Namdang",
            "block": "Sonari",
            "gender": "Female",
            "marital_status": "Widowed",
            "husband_name": "Late Biren Gogoi",
            "bank_account": "9876543210123",
            "ifsc": "SBIN0003077",
            "dob": "12/03/1952"
        }
        res = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf", is_ai=False
        )
        self.assertTrue(res["success"], res.get("errors"))
        self.assertIn("application_no", res)
        self.assertTrue(res["application_no"].startswith("SSP26") or res["application_no"].startswith("SSP"))
        self.assertEqual(res["status"], "PENDING")

        app_id = res["application_id"]

        # Verify record exists in DB
        app_details = services.get_citizen_application(self.test_mobile, app_id)
        self.assertIsNotNone(app_details)
        self.assertEqual(app_details["applicant_name"], "Hemaprabha Gogoi")
        self.assertEqual(app_details["block"], "Sonari")
        self.assertEqual(app_details["status"], "PENDING")

        # Verify portal user account created with DOB password
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (self.test_mobile,))
        row = cur.fetchone()
        self.assertIsNotNone(row)
        expected_hash = services.hash_password("12031952")
        self.assertEqual(row[0], expected_hash)

        # Verify audit trail contains SUBMIT
        audit = services.get_audit_trail(cur, app_id)
        self.assertTrue(any(a["action"] == "SUBMIT" for a in audit))
        cur.close()
        conn.close()

    def test_duplicate_submission_prevention(self):
        data = {
            "applicant_name": "Hemaprabha Gogoi",
            "village": "Namdang",
            "block": "Sonari",
            "bank_account": "9876543210123",
            "ifsc": "SBIN0003077",
            "dob": "12/03/1952"
        }
        # First submission
        res1 = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        self.assertTrue(res1["success"])

        # Second submission with same mobile while first is PENDING
        res2 = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        self.assertFalse(res2["success"])
        self.assertIn("already exists", res2["errors"][0])
        self.assertEqual(res2["existing"]["application_no"], res1["application_no"])

    def test_citizen_authorization_and_idor_protection(self):
        data = {
            "applicant_name": "Citizen One",
            "village": "Namdang",
            "block": "Sonari",
            "bank_account": "9876543210123",
            "ifsc": "SBIN0003077",
            "dob": "12/03/1952"
        }
        res = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        app_id = res["application_id"]

        # Citizen 1 can access
        app1 = services.get_citizen_application(self.test_mobile, app_id)
        self.assertIsNotNone(app1)

        # Citizen 2 (different mobile) cannot access Citizen 1's application
        other_mobile = "9999000002"
        app2 = services.get_citizen_application(other_mobile, app_id)
        self.assertIsNone(app2)

        # Citizen 2 cannot withdraw Citizen 1's application
        withdraw_attempt = services.withdraw_citizen_application(other_mobile, app_id)
        self.assertFalse(withdraw_attempt["success"])

        # Citizen 1 can withdraw their own application
        withdraw_res = services.withdraw_citizen_application(self.test_mobile, app_id)
        self.assertTrue(withdraw_res["success"])
        self.assertEqual(withdraw_res["status"], "WITHDRAWN")

        # After withdrawal, applicant can submit again
        res_reapply = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        self.assertTrue(res_reapply["success"])

    def test_officer_block_level_authorization(self):
        data = {
            "applicant_name": "Sonari Resident",
            "village": "Namdang",
            "block": "Sonari",
            "bank_account": "9876543210123",
            "ifsc": "SBIN0003077",
            "dob": "12/03/1952"
        }
        res = services.submit_application(
            self.test_mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        app_id = res["application_id"]

        # Officer for Sonari can view details and checklist
        details = services.get_officer_application_details("officer_sonari", app_id)
        self.assertIsNotNone(details)
        self.assertEqual(details["block"], "Sonari")
        self.assertTrue(details["review_checklist"]["is_age_eligible"])
        self.assertTrue(details["review_checklist"]["is_bank_account_valid"])

        # Officer for Rajapara cannot view Sonari application
        with self.assertRaises(PermissionError):
            services.get_officer_application_details("officer_rajapara", app_id)

        # Officer for Rajapara cannot decide Sonari application
        with self.assertRaises(PermissionError):
            services.decide_application(
                "officer_rajapara", app_id, "APPROVED", reason="Unpermitted approval"
            )

        # Officer for Sonari approves the application
        decide_res = services.decide_application(
            "officer_sonari", app_id, "APPROVED", reason="Documents and age verified in person"
        )
        self.assertTrue(decide_res["success"])
        self.assertEqual(decide_res["status"], "APPROVED")

        # Concurrency check: deciding already decided application fails
        decide_again = services.decide_application(
            "officer_sonari", app_id, "REJECTED", reason="Late rejection"
        )
        self.assertFalse(decide_again["success"])
        self.assertIn("Cannot decide", decide_again["error"])

        # Verify decision and reason recorded in audit log
        conn = services.get_db()
        cur = conn.cursor()
        audit = services.get_audit_trail(cur, app_id)
        approve_entry = next((a for a in audit if a["action"] == "APPROVED"), None)
        self.assertIsNotNone(approve_entry)
        self.assertEqual(approve_entry["actor"], "officer:officer_sonari")
        self.assertEqual(approve_entry["note"], "Documents and age verified in person")
        cur.close()
        conn.close()


if __name__ == "__main__":
    unittest.main()
