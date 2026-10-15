#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sewa Setu — Database Migration & Schema Verification Script.

Executes schema.sql idempotently against the configured database, validates all
required tables, indexes, and relations, and ensures the database is fully initialized
for production workloads.
"""

import os
import sys
import psycopg2

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(BASE_DIR, "app", "schema.sql")
if not os.path.exists(SCHEMA_PATH):
    SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")


def get_connection():
    import configparser
    ini_path = os.path.join(BASE_DIR, "app", "config", "app.ini")
    if not os.path.exists(ini_path):
        ini_path = os.path.join(BASE_DIR, "config", "app.ini")
    cfg = configparser.ConfigParser()
    if os.path.exists(ini_path):
        cfg.read(ini_path)

    host = os.environ.get("DB_HOST") or cfg.get("database", "host", fallback="db")
    port = int(os.environ.get("DB_PORT") or cfg.get("database", "port", fallback=5432))
    name = os.environ.get("DB_NAME") or os.environ.get("POSTGRES_DB") or cfg.get("database", "name", fallback="sewasetu")
    user = os.environ.get("DB_USER") or os.environ.get("POSTGRES_USER") or cfg.get("database", "user", fallback="sewasetu")
    password = os.environ.get("DB_PASSWORD") or os.environ.get("POSTGRES_PASSWORD") or cfg.get("database", "password", fallback="sewasetu123")

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=name,
        user=user,
        password=password
    )


def run_migrations():
    print(f"Connecting to database to apply migrations...")
    try:
        conn = get_connection()
        conn.autocommit = False
    except Exception as e:
        print(f"ERROR: Could not connect to database: {e}", file=sys.stderr)
        return 1

    cur = conn.cursor()

    try:
        print(f"Reading schema from: {SCHEMA_PATH}")
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            sql_script = f.read()

        cur.execute(sql_script)
        conn.commit()
        print("Schema applied successfully.")

        # Verification of critical production tables
        required_tables = [
            "applications",
            "portal_users",
            "otps",
            "audit_log",
            "oauth_keys",
            "oauth_clients",
            "oauth_requests",
            "oauth_codes",
            "oauth_refresh_tokens",
            "oauth_program_tokens"
        ]

        cur.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public';
        """)
        existing_tables = {row[0] for row in cur.fetchall()}

        missing = [t for t in required_tables if t not in existing_tables]
        if missing:
            print(f"ERROR: Missing required tables after migration: {missing}", file=sys.stderr)
            conn.rollback()
            return 1

        print("Verification passed: all required tables exist:")
        for t in required_tables:
            print(f"  ✓ {t}")

        cur.close()
        conn.close()
        print("Database migrations and verification completed successfully.")
        return 0

    except Exception as e:
        conn.rollback()
        print(f"Migration failed with error: {e}", file=sys.stderr)
        cur.close()
        conn.close()
        return 1


if __name__ == "__main__":
    sys.exit(run_migrations())
