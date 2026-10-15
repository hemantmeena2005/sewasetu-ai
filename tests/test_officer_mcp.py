# -*- coding: utf-8 -*-
"""
Tests for Phase 5: Officer MCP Server & Review Workflow.

Validates:
1. Authorized officer retrieves pending applications with pagination and sorting.
2. Officer sees only applications from their authorized block (block isolation).
3. Unauthorized caller (unauthenticated, citizen token, invalid token) cannot access officer tools.
4. Officer retrieves application details and review evidence (masked bank details, no public URLs).
5. Authorized officer approves an eligible pending application with explicit human confirmation.
6. Officer rejects an application with a recorded reason and explicit confirmation.
7. Rejection without a required reason is rejected.
8. Invalid status transitions are blocked (e.g. approving already approved/rejected).
9. Concurrent decisions do not create inconsistent records (race condition protection).
10. Unauthorized access across blocks to application details and decision history is blocked.
11. Recorded decisions appear in the existing portal and database system of record.
12. Sensitive applicant information (bank account) is masked.
13. Under-age applicant approval is blocked by backend statutory validation.
"""

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
import oauth
import mcp_officer
import mcp_citizen


class TestOfficerMCPWorkflow(unittest.TestCase):
    """Integration and tool tests for the Officer MCP server and review workflow."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        self.mobile_sonari = "9876540001"
        self.mobile_rajapara = "9876540002"
        self.mobile_underage = "9876540003"
        self._cleanup_mobiles([self.mobile_sonari, self.mobile_rajapara, self.mobile_underage])

        # Generate tokens for testing
        self.token_sonari = mcp_officer.create_officer_session_token("officer_sonari")
        self.token_rajapara = mcp_officer.create_officer_session_token("officer_rajapara")
        self.token_admin = mcp_officer.create_officer_session_token("admin")
        self.token_citizen = mcp_citizen.create_citizen_session_token("9876549999")

        # Create test applications in DB
        self.app_sonari = self._create_test_app(
            mobile=self.mobile_sonari,
            name="Sonari Resident",
            village="Namdang",
            block="Sonari",
            dob="12/04/1952",  # Eligible: age 74
            bank_acc="123456789012"
        )
        self.app_rajapara = self._create_test_app(
            mobile=self.mobile_rajapara,
            name="Rajapara Resident",
            village="Mornoi",
            block="Rajapara",
            dob="10/10/1950",  # Eligible: age 76
            bank_acc="987654321098"
        )
        self.app_underage = self._create_test_app(
            mobile=self.mobile_underage,
            name="Young Applicant",
            village="Namdang",
            block="Sonari",
            dob="10/05/1990",  # Ineligible: age 36
            bank_acc="555566667777",
            force_db=True
        )

    def tearDown(self):
        self._cleanup_mobiles([self.mobile_sonari, self.mobile_rajapara, self.mobile_underage])

    def _cleanup_mobiles(self, mobiles):
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE mobile = ANY(%s)", (mobiles,))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM audit_log WHERE application_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM applications WHERE id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM otps WHERE mobile = ANY(%s)", (mobiles,))
        conn.commit()
        cur.close()
        conn.close()

    def _create_test_app(self, mobile, name, village, block, dob, bank_acc, force_db=False):
        if force_db:
            conn = services.get_db()
            cur = conn.cursor()
            app_no = "SSP26TEST" + mobile[-4:]
            dob_dt = datetime.strptime(dob, "%d/%m/%Y").date()
            cur.execute(
                """INSERT INTO applications
                   (application_no, applicant_name, mobile, dob, gender, marital_status,
                    village, block, bank_account, ifsc, doc_path, status, submitted_at)
                   VALUES (%s, %s, %s, %s, 'Female', 'Single', %s, %s, %s, 'SBIN0003077',
                           '/var/sewasetu/uploads/proof.pdf', 'PENDING', NOW())
                   RETURNING id""",
                (app_no, name, mobile, dob_dt, village, block, bank_acc)
            )
            app_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            return {"success": True, "application_id": app_id, "application_no": app_no}

        data = {
            "applicant_name": name,
            "dob": dob,
            "gender": "Female",
            "village": village,
            "block": block,
            "bank_account": bank_acc,
            "ifsc": "SBIN0003077"
        }
        res = services.submit_application(
            mobile, data, doc_path="/var/sewasetu/uploads/proof.pdf"
        )
        self.assertTrue(res["success"])
        return res

    # -----------------------------------------------------------------------
    # 1. Officer Authentication
    # -----------------------------------------------------------------------
    def test_officer_authentication_tool(self):
        # Valid credentials
        res = mcp_officer.authenticate_officer("officer_sonari", "change-me-before-deploying")
        self.assertTrue(res["success"])
        self.assertIn("officer_token", res)
        self.assertEqual(res["officer_username"], "officer_sonari")
        self.assertIn("Sonari", res["authorized_blocks"])

        # Invalid credentials
        res_bad = mcp_officer.authenticate_officer("officer_sonari", "wrong-password")
        self.assertFalse(res_bad["success"])
        self.assertIn("Invalid officer credentials", res_bad["error"])

    # -----------------------------------------------------------------------
    # 2. Block-Level Isolation on Pending Applications
    # -----------------------------------------------------------------------
    def test_pending_applications_block_isolation(self):
        # Officer Sonari should see the Sonari applications but NOT the Rajapara application
        res = mcp_officer.get_pending_applications(
            officer_token=self.token_sonari, limit=50, sort_order="desc"
        )
        self.assertTrue(res["success"])
        blocks = {app["block"] for app in res["applications"]}
        self.assertTrue(all(b == "Sonari" for b in blocks))
        app_nos = [app["application_no"] for app in res["applications"]]
        self.assertIn(self.app_sonari["application_no"], app_nos)
        self.assertNotIn(self.app_rajapara["application_no"], app_nos)

        # Officer Sonari cannot query block_filter='Rajapara' (forbidden)
        res_cross = mcp_officer.get_pending_applications(
            officer_token=self.token_sonari, block_filter="Rajapara"
        )
        self.assertFalse(res_cross["success"])
        self.assertIn("Forbidden", res_cross["error"])

    # -----------------------------------------------------------------------
    # 3. Unauthorized User Access Blocked
    # -----------------------------------------------------------------------
    def test_unauthorized_token_access_blocked(self):
        # Citizen token used on officer tool
        res_cit = mcp_officer.get_pending_applications(officer_token=self.token_citizen)
        self.assertFalse(res_cit["success"])
        self.assertIn("Unauthorized", res_cit["error"])

        # Completely invalid / empty token
        res_none = mcp_officer.get_pending_applications(officer_token="")
        self.assertFalse(res_none["success"])
        self.assertIn("Unauthorized", res_none["error"])

        res_tampered = mcp_officer.get_pending_applications(officer_token="tampered.jwt.token")
        self.assertFalse(res_tampered["success"])
        self.assertIn("Unauthorized", res_tampered["error"])

    # -----------------------------------------------------------------------
    # 4. Application Details and Review Evidence
    # -----------------------------------------------------------------------
    def test_application_details_and_sensitive_data_masking(self):
        # Officer Sonari views Sonari application
        res = mcp_officer.get_application_details(
            application_id=self.app_sonari["application_id"],
            officer_token=self.token_sonari
        )
        self.assertTrue(res["success"])
        app = res["application"]
        self.assertEqual(app["application_no"], self.app_sonari["application_no"])
        self.assertEqual(app["applicant_name"], "Sonari Resident")
        # Bank account MUST be masked (XXXXXX9012)
        self.assertEqual(app["bank_account_masked"], "XXXXXX9012")
        self.assertNotIn("123456789012", str(app))
        # Document info is present but not exposed as an insecure public URL
        self.assertTrue(app["document"]["attached"])
        self.assertEqual(app["document"]["filename"], "proof.pdf")

        # Officer Sonari attempting to view Rajapara application is blocked
        res_forbidden = mcp_officer.get_application_details(
            application_id=self.app_rajapara["application_id"],
            officer_token=self.token_sonari
        )
        self.assertFalse(res_forbidden["success"])
        self.assertIn("Forbidden", res_forbidden["error"])

    # -----------------------------------------------------------------------
    # 5. Review Checklist Generation
    # -----------------------------------------------------------------------
    def test_review_checklist(self):
        # Eligible applicant checklist
        res = mcp_officer.get_review_checklist(
            application_id=self.app_sonari["application_id"],
            officer_token=self.token_sonari
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["overall_readiness"], "READY_FOR_DECISION")
        self.assertTrue(res["is_statutorily_eligible"])
        criteria = {item["criterion"]: item["status"] for item in res["checklist"]}
        self.assertEqual(criteria["Age Requirement (>= 60 years)"], "PASS")
        self.assertEqual(criteria["Administrative Jurisdiction"], "PASS")
        self.assertEqual(criteria["Age Proof Documentation"], "PASS")
        self.assertEqual(criteria["DBT Bank Account Verification"], "PASS")

        # Underage applicant checklist
        res_underage = mcp_officer.get_review_checklist(
            application_id=self.app_underage["application_id"],
            officer_token=self.token_sonari
        )
        self.assertTrue(res_underage["success"])
        self.assertEqual(res_underage["overall_readiness"], "DEFICIENT")
        self.assertFalse(res_underage["is_statutorily_eligible"])
        criteria_u = {item["criterion"]: item["status"] for item in res_underage["checklist"]}
        self.assertEqual(criteria_u["Age Requirement (>= 60 years)"], "FAIL")
        self.assertTrue(any("under 60 years" in iss for iss in res_underage["outstanding_issues"]))

    # -----------------------------------------------------------------------
    # 6. Approval with Explicit Human Confirmation
    # -----------------------------------------------------------------------
    def test_approve_application_flow(self):
        app_id = self.app_sonari["application_id"]

        # 1. Attempt approval WITHOUT human confirmation (must be blocked)
        res_unconfirmed = mcp_officer.approve_application(
            application_id=app_id,
            reason="Verified physical documents",
            officer_confirmed=False,
            officer_token=self.token_sonari
        )
        self.assertFalse(res_unconfirmed["success"])
        self.assertIn("Explicit officer confirmation required", res_unconfirmed["error"])

        # 2. Attempt approval with unauthorized officer (must be forbidden)
        res_unauth = mcp_officer.approve_application(
            application_id=app_id,
            reason="Verified physical documents",
            officer_confirmed=True,
            officer_token=self.token_rajapara
        )
        self.assertFalse(res_unauth["success"])
        self.assertIn("Forbidden", res_unauth["error"])

        # 3. Successful approval with authorized officer
        res_ok = mcp_officer.approve_application(
            application_id=app_id,
            reason="Physical age proof and residence verified in camp",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )
        self.assertTrue(res_ok["success"])
        self.assertEqual(res_ok["status"], "APPROVED")
        self.assertEqual(res_ok["decided_by"], "officer_sonari")

        # Verify status in database
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT status, decided_by FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
        self.assertEqual(row[0], "APPROVED")
        self.assertEqual(row[1], "officer_sonari")
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 7. Statutory Age Requirement Enforcement on Approval
    # -----------------------------------------------------------------------
    def test_underage_approval_blocked(self):
        # Even if officer tries to approve underage applicant, backend blocks it
        res = mcp_officer.approve_application(
            application_id=self.app_underage["application_id"],
            reason="Erroneous approval attempt",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )
        self.assertFalse(res["success"])
        self.assertIn("minimum required is 60", res["error"])

    # -----------------------------------------------------------------------
    # 8. Rejection with Mandatory Reason and Human Confirmation
    # -----------------------------------------------------------------------
    def test_reject_application_flow(self):
        app_id = self.app_rajapara["application_id"]

        # 1. Missing confirmation
        res_unconfirmed = mcp_officer.reject_application(
            application_id=app_id,
            reason="Unreadable document",
            officer_confirmed=False,
            officer_token=self.token_rajapara
        )
        self.assertFalse(res_unconfirmed["success"])
        self.assertIn("Explicit officer confirmation required", res_unconfirmed["error"])

        # 2. Missing reason
        res_no_reason = mcp_officer.reject_application(
            application_id=app_id,
            reason="   ",
            officer_confirmed=True,
            officer_token=self.token_rajapara
        )
        self.assertFalse(res_no_reason["success"])
        self.assertIn("Rejection reason is mandatory", res_no_reason["error"])

        # 3. Successful rejection
        res_ok = mcp_officer.reject_application(
            application_id=app_id,
            reason="Uploaded age proof certificate was completely illegible.",
            officer_confirmed=True,
            officer_token=self.token_rajapara
        )
        self.assertTrue(res_ok["success"])
        self.assertEqual(res_ok["status"], "REJECTED")

        # Verify status in database
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT status, decided_by FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
        self.assertEqual(row[0], "REJECTED")
        self.assertEqual(row[1], "officer_rajapara")
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 9. Invalid Status Transitions Blocked
    # -----------------------------------------------------------------------
    def test_invalid_status_transitions_blocked(self):
        app_id = self.app_sonari["application_id"]
        # Approve first
        mcp_officer.approve_application(
            application_id=app_id,
            reason="Valid approval",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )

        # Attempt to reject an already approved application
        res_redecide = mcp_officer.reject_application(
            application_id=app_id,
            reason="Attempting to re-decide approved application",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )
        self.assertFalse(res_redecide["success"])
        self.assertIn("current status is 'APPROVED'", res_redecide["error"])

    # -----------------------------------------------------------------------
    # 10. Decision History and Audit Trail
    # -----------------------------------------------------------------------
    def test_decision_history_and_audit(self):
        app_id = self.app_sonari["application_id"]
        mcp_officer.approve_application(
            application_id=app_id,
            reason="Audited approval reason",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )

        # Officer Sonari can inspect decision history for the application
        res = mcp_officer.get_decision_history(
            application_id=app_id,
            officer_token=self.token_sonari
        )
        self.assertTrue(res["success"])
        self.assertGreaterEqual(res["record_count"], 1)
        actions = [h["decision"] for h in res["history"]]
        self.assertIn("APPROVED", actions)

        # Officer Rajapara cannot inspect Sonari application history
        res_cross = mcp_officer.get_decision_history(
            application_id=app_id,
            officer_token=self.token_rajapara
        )
        self.assertFalse(res_cross["success"])
        self.assertIn("Forbidden", res_cross["error"])

    # -----------------------------------------------------------------------
    # 11. Consistency with Existing Web Portal
    # -----------------------------------------------------------------------
    def test_decisions_reflected_in_web_portal(self):
        app_id = self.app_sonari["application_id"]
        mcp_officer.approve_application(
            application_id=app_id,
            reason="Approved via MCP tool",
            officer_confirmed=True,
            officer_token=self.token_sonari
        )

        # Check existing web portal endpoint /admin/application/<id>
        client = portal.app.test_client()
        with client.session_transaction() as sess:
            sess["admin"] = True
            sess["admin_user"] = "admin"

        resp = client.get(f"/admin/application/{app_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"APPROVED", resp.data)
        self.assertIn(b"officer:officer_sonari", resp.data)
        self.assertIn(b"Approved via MCP tool", resp.data)

    # -----------------------------------------------------------------------
    # 12. Concurrent Decisions Race Condition Protection
    # -----------------------------------------------------------------------
    def test_concurrent_decisions_prevented(self):
        import concurrent.futures
        app_id = self.app_sonari["application_id"]

        results = []
        def try_decide(decision, reason):
            return mcp_officer.approve_application(
                application_id=app_id,
                reason=reason,
                officer_confirmed=True,
                officer_token=self.token_sonari
            ) if decision == "APPROVED" else mcp_officer.reject_application(
                application_id=app_id,
                reason=reason,
                officer_confirmed=True,
                officer_token=self.token_sonari
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(try_decide, "APPROVED", "Thread 1 approval")
            f2 = executor.submit(try_decide, "REJECTED", "Thread 2 rejection")
            r1 = f1.result()
            r2 = f2.result()

        successes = [r for r in [r1, r2] if r.get("success")]
        failures = [r for r in [r1, r2] if not r.get("success")]

        # Exactly one decision must succeed, the other must fail safely
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIn("already processed", failures[0]["error"].lower())

    # -----------------------------------------------------------------------
    # 13. Safe, Structured Error Handling
    # -----------------------------------------------------------------------
    def test_safe_structured_error_responses(self):
        # Non-existent application ID
        res = mcp_officer.get_application_details(
            application_id=9999999,
            officer_token=self.token_sonari
        )
        self.assertFalse(res["success"])
        self.assertIn("not found", res["error"])

        # Invalid checklist lookup
        res_chk = mcp_officer.get_review_checklist(
            application_id=9999999,
            officer_token=self.token_sonari
        )
        self.assertFalse(res_chk["success"])
        self.assertIn("not found", res_chk["error"])


if __name__ == "__main__":
    unittest.main()

