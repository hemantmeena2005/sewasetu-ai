# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job under the Purvanchal Right to Public Services (RTPS) Act.

Under Section 5 of the RTPS Act, an eligible application not processed within the
statutory 15-day period stands automatically approved by operation of law.
This job identifies overdue applications, verifies statutory eligibility, atomically
transitions their status to 'DEEMED_APPROVED', generates immutable audit log records,
and dispatches citizen SMS notifications.

Compatible with cron execution and persistent daemon execution.
"""

import os
import sys
import time
import argparse
from datetime import datetime

# Ensure app root is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import services


def run_cycle(batch_size=500, actor="system:rtps-cron"):
    """Execute a single atomic deemed approval processing cycle."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = services.process_deemed_approvals(batch_size=batch_size, actor=actor)
    if not result.get("success"):
        print(f"[{now_str}] Deemed approval error: {result.get('error')}", file=sys.stderr)
        return 1

    scanned = result.get("scanned", 0)
    approved = result.get("approved_count", 0)
    skipped = result.get("skipped_ineligible_count", 0)
    errors = result.get("error_count", 0)

    print(
        f"[{now_str}] RTPS Deemed Approval Job: {scanned} scanned, "
        f"{approved} automatically approved, {skipped} skipped (ineligible/deficient), {errors} errors."
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description="Purvanchal RTPS Act Statutory Deemed Approval Worker.")
    parser.add_argument("--batch-size", type=int, default=500, help="Maximum records per batch")
    parser.add_argument("--actor", type=str, default="system:rtps-cron", help="Audit actor identifier")
    parser.add_argument("--daemon", action="store_true", help="Run persistently in daemon mode")
    parser.add_argument("--interval", type=int, default=300, help="Sleep interval in seconds for daemon mode")
    args = parser.parse_args()

    if args.daemon:
        print(f"Starting deemed approval background daemon (interval: {args.interval}s)...")
        while True:
            try:
                run_cycle(batch_size=args.batch_size, actor="system:rtps-daemon")
            except Exception as e:
                print(f"Daemon worker exception: {e}", file=sys.stderr)
            time.sleep(args.interval)
    else:
        return run_cycle(batch_size=args.batch_size, actor=args.actor)


if __name__ == "__main__":
    sys.exit(main())
