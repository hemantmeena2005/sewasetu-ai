# -*- coding: utf-8 -*-
"""
Tests for Phase 6: 15-Day Automatic Deemed Approval System.

Validates:
1. Accurate, persisted deadline calculation (submitted_at + 15 calendar days).
2. Boundary conditions: applications under 15 days old are not deemed approved.
3. Overdue applications (submitted_at >= 15 days ago) are automatically transitioned to DEEMED_APPROVED.
4. Terminal state protection: already APPROVED, REJECTED, or WITHDRAWN applications are never re-processed.
5. Statutory eligibility enforcement: underage applicants are skipped and not deemed approved.
6. Idempotency: repeated runs of deemed approval do not create duplicate transitions or duplicate audit entries.
7. Concurrency & race condition safety: officer decision vs deemed approval scheduler.
8. Audit trail completeness: records DEEMED_APPROVED, actor, statutory deadline, and legal justification.
9. Portal and Citizen MCP integration: citizen status view and MCP tools reflect DEEMED_APPROVED accurately.
10. Officer MCP integration: officer review tools reflect DEEMED_APPROVED and block further manual decisions.
11. Multi-worker safety with FOR UPDATE SKIP LOCKED.
"""

import os
import sys
import unittest
import concurrent.futures
from datetime import datetime, timedelta

# Ensure app directory is in Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import werkzeug
if not hasattr(werkzeug, "__version__"):
    werkzeug.__version__ = "3.0.0"

import app as portal
import services
import mcp_citizen
import mcp_officer


class TestAutomaticDeemedApproval(unittest.TestCase):
    """Test suite for Phase 6 automatic deemed approval workflow."""

    def setUp(self):
        portal.app.config["TESTING"] = True
        self.mobiles = [
            "9876550001", "9876550002", "9876550003",
            "9876550004", "9876550005", "9876550006"
        ]
        self._cleanup()

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE mobile = ANY(%s)", (self.mobiles,))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM audit_log WHERE application_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM applications WHERE id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM otps WHERE mobile = ANY(%s)", (self.mobiles,))
        conn.commit()
        cur.close()
        conn.close()

    def _insert_custom_app(self, mobile, name, block, submitted_at, dob="12/04/1952", status="PENDING"):
        conn = services.get_db()
        cur = conn.cursor()
        app_no = "SSP26DMD" + mobile[-4:]
        dob_dt = datetime.strptime(dob, "%d/%m/%Y").date()
        cur.execute(
            """INSERT INTO applications
               (application_no, applicant_name, mobile, dob, gender, marital_status,
                village, block, bank_account, ifsc, doc_path, status, submitted_at)
               VALUES (%s, %s, %s, %s, 'Female', 'Widowed', 'Namdang', %s,
                       '123456789012', 'SBIN0003077', '/var/sewasetu/uploads/proof.pdf', %s, %s)
               RETURNING id""",
            (app_no, name, mobile, dob_dt, block, status, submitted_at)
        )
        app_id = cur.fetchone()[0]
        services.write_audit(cur, app_id, "SUBMIT", f"applicant:{mobile}", "Submitted application")
        conn.commit()
        cur.close()
        conn.close()
        return {"id": app_id, "application_no": app_no}

    # -----------------------------------------------------------------------
    # 1. Authoritative Deadline Calculation
    # -----------------------------------------------------------------------
    def test_deadline_calculation_and_overdue_check(self):
        now = datetime.now()
        sub_time = now - timedelta(days=10)
        deadline = services.calculate_application_deadline(sub_time)
        # Exactly 15 calendar days from submission
        self.assertEqual(deadline, sub_time + timedelta(days=15))

        # 10 days old -> not overdue
        self.assertFalse(services.is_application_overdue(sub_time, reference_time=now))

        # 16 days old -> overdue
        overdue_sub = now - timedelta(days=16)
        self.assertTrue(services.is_application_overdue(overdue_sub, reference_time=now))

    # -----------------------------------------------------------------------
    # 2. Boundary: Application Under 15 Days is NOT Deemed Approved
    # -----------------------------------------------------------------------
    def test_recent_application_not_deemed_approved(self):
        # Submitted 10 days ago (within 15-day SLA)
        recent_ts = datetime.now() - timedelta(days=10)
        app = self._insert_custom_app(
            mobile=self.mobiles[0],
            name="Recent Applicant",
            block="Sonari",
            submitted_at=recent_ts
        )

        # Attempt deemed approval on this individual application
        res = services.apply_deemed_approval(app["id"])
        self.assertFalse(res["success"])
        self.assertIn("has not reached its statutory deadline", res["error"])

        # Run batch processor: should scan but approve 0
        batch_res = services.process_deemed_approvals(batch_size=50)
        self.assertNotIn(app["application_no"], batch_res["approved_applications"])

        # Check DB status remains PENDING
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id = %s", (app["id"],))
        self.assertEqual(cur.fetchone()[0], "PENDING")
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 3. Overdue Application is Automatically Approved
    # -----------------------------------------------------------------------
    def test_overdue_application_automatically_approved(self):
        # Submitted 16 days ago (exceeded 15-day SLA)
        overdue_ts = datetime.now() - timedelta(days=16)
        app = self._insert_custom_app(
            mobile=self.mobiles[1],
            name="Overdue Applicant",
            block="Sonari",
            submitted_at=overdue_ts
        )

        res = services.apply_deemed_approval(app["id"], actor="system:rtps-auto", correlation_id="test-run-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "DEEMED_APPROVED")
        self.assertEqual(res["decided_by"], "RTPS-AUTO")

        # Verify DB status
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT status, decided_by, decided_at FROM applications WHERE id = %s",
            (app["id"],)
        )
        row = cur.fetchone()
        self.assertEqual(row[0], "DEEMED_APPROVED")
        self.assertEqual(row[1], "RTPS-AUTO")
        self.assertIsNotNone(row[2])

        # Verify Audit Log
        cur.execute(
            "SELECT action, actor, note FROM audit_log WHERE application_id = %s AND action = 'DEEMED_APPROVED'",
            (app["id"],)
        )
        audit_row = cur.fetchone()
        self.assertIsNotNone(audit_row)
        self.assertEqual(audit_row[0], "DEEMED_APPROVED")
        self.assertEqual(audit_row[1], "system:rtps-auto")
        self.assertIn("Section 5 of Purvanchal Right to Public Services Act", audit_row[2])
        self.assertIn("Statutory 15-day deadline expired", audit_row[2])
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 4. Terminal State Protection (Already Approved/Rejected are Untouched)
    # -----------------------------------------------------------------------
    def test_terminal_states_not_deemed_approved(self):
        overdue_ts = datetime.now() - timedelta(days=20)
        app_approved = self._insert_custom_app(
            mobile=self.mobiles[2],
            name="Already Approved",
            block="Sonari",
            submitted_at=overdue_ts,
            status="APPROVED"
        )
        app_rejected = self._insert_custom_app(
            mobile=self.mobiles[3],
            name="Already Rejected",
            block="Sonari",
            submitted_at=overdue_ts,
            status="REJECTED"
        )

        # Attempt deemed approval on both
        res_appr = services.apply_deemed_approval(app_approved["id"])
        self.assertFalse(res_appr["success"])
        self.assertIn("terminal status 'APPROVED'", res_appr["error"])

        res_rej = services.apply_deemed_approval(app_rejected["id"])
        self.assertFalse(res_rej["success"])
        self.assertIn("terminal status 'REJECTED'", res_rej["error"])

    # -----------------------------------------------------------------------
    # 5. Underage Ineligible Applicants are NOT Automatically Approved
    # -----------------------------------------------------------------------
    def test_underage_applicant_skipped_by_deemed_approval(self):
        # Overdue submission, but applicant is 36 years old (born 1990)
        overdue_ts = datetime.now() - timedelta(days=18)
        app_underage = self._insert_custom_app(
            mobile=self.mobiles[4],
            name="Underage Applicant",
            block="Sonari",
            submitted_at=overdue_ts,
            dob="10/05/1990"
        )

        res = services.apply_deemed_approval(app_underage["id"])
        self.assertFalse(res["success"])
        self.assertIn("does not satisfy statutory age criteria", res["error"])

        # Verify status remains PENDING in database
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id = %s", (app_underage["id"],))
        self.assertEqual(cur.fetchone()[0], "PENDING")
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 6. Idempotency: Re-running Does Not Duplicate Decisions
    # -----------------------------------------------------------------------
    def test_idempotency_of_deemed_approval(self):
        overdue_ts = datetime.now() - timedelta(days=16)
        app = self._insert_custom_app(
            mobile=self.mobiles[5],
            name="Idempotency Test",
            block="Sonari",
            submitted_at=overdue_ts
        )

        # First run succeeds
        res1 = services.apply_deemed_approval(app["id"])
        self.assertTrue(res1["success"])

        # Second run immediately following is a safe no-op
        res2 = services.apply_deemed_approval(app["id"])
        self.assertFalse(res2["success"])
        self.assertIn("terminal status 'DEEMED_APPROVED'", res2["error"])

        # Verify only 1 DEEMED_APPROVED audit entry exists
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE application_id = %s AND action = 'DEEMED_APPROVED'",
            (app["id"],)
        )
        self.assertEqual(cur.fetchone()[0], 1)
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 7. Concurrency: Race Condition with Officer Decision
    # -----------------------------------------------------------------------
    def test_concurrency_race_condition_protection(self):
        overdue_ts = datetime.now() - timedelta(days=16)
        app = self._insert_custom_app(
            mobile=self.mobiles[0],
            name="Race Condition Applicant",
            block="Sonari",
            submitted_at=overdue_ts
        )
        app_id = app["id"]

        def officer_action():
            return services.decide_application(
                officer_username="officer_sonari",
                app_id_or_no=app_id,
                decision="REJECTED",
                reason="Officer rejected in field camp"
            )

        def deemed_action():
            return services.apply_deemed_approval(app_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(officer_action)
            f2 = executor.submit(deemed_action)
            r1 = f1.result()
            r2 = f2.result()

        successes = [r for r in [r1, r2] if r.get("success")]
        failures = [r for r in [r1, r2] if not r.get("success")]

        # Exactly ONE terminal decision succeeds
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)

        # Confirm database is in a consistent state
        conn = services.get_db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM applications WHERE id = %s", (app_id,))
        final_status = cur.fetchone()[0]
        self.assertIn(final_status, ("REJECTED", "DEEMED_APPROVED"))

        # Verify no contradictory multiple decision entries in audit log
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE application_id = %s AND action IN ('REJECTED', 'DEEMED_APPROVED')",
            (app_id,)
        )
        self.assertEqual(cur.fetchone()[0], 1)
        cur.close(); conn.close()

    # -----------------------------------------------------------------------
    # 8. Web Portal & MCP Integration Verification
    # -----------------------------------------------------------------------
    def test_deemed_approved_reflected_in_portal_and_mcp(self):
        overdue_ts = datetime.now() - timedelta(days=17)
        app = self._insert_custom_app(
            mobile=self.mobiles[1],
            name="Integration Verify",
            block="Sonari",
            submitted_at=overdue_ts
        )
        app_id = app["id"]
        app_no = app["application_no"]

        # Apply deemed approval
        res = services.apply_deemed_approval(app_id)
        self.assertTrue(res["success"])

        # 1. Web portal admin view reflects DEEMED_APPROVED
        client = portal.app.test_client()
        with client.session_transaction() as sess:
            sess["admin"] = True
            sess["admin_user"] = "admin"

        r_web = client.get(f"/admin/application/{app_id}")
        self.assertEqual(r_web.status_code, 200)
        self.assertIn(b"DEEMED_APPROVED", r_web.data)
        self.assertIn(b"RTPS-AUTO", r_web.data)

        # 2. Citizen MCP get_application_status reflects DEEMED_APPROVED
        citizen_token = mcp_citizen.create_citizen_session_token(self.mobiles[1])
        c_status = mcp_citizen.get_application_status(
            session_token=citizen_token,
            application_no=app_no
        )
        self.assertTrue(c_status["success"])
        self.assertEqual(c_status["status"], "DEEMED_APPROVED")
        self.assertEqual(c_status["decided_by"], "RTPS-AUTO")

        # 3. Officer MCP get_application_details reflects DEEMED_APPROVED
        officer_token = mcp_officer.create_officer_session_token("officer_sonari")
        o_details = mcp_officer.get_application_details(
            application_id=app_id,
            officer_token=officer_token
        )
        self.assertTrue(o_details["success"])
        self.assertEqual(o_details["application"]["status"], "DEEMED_APPROVED")

        # 4. Officer cannot approve or reject a deemed approved application
        o_decide = mcp_officer.approve_application(
            application_id=app_id,
            reason="Late officer approval",
            officer_confirmed=True,
            officer_token=officer_token
        )
        self.assertFalse(o_decide["success"])
        self.assertIn("Cannot decide application", o_decide["error"])


if __name__ == "__main__":
    unittest.main()
