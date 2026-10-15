#!/usr/bin/env bash
# ==============================================================================
# Sewa Setu — Production Database Restoration Script
#
# Validates integrity checksums, warns for safety, and performs clean restoration
# into the target PostgreSQL database cluster.
# ==============================================================================

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: $0 <path_to_backup.sql.gz>"
    echo "Example: $0 /var/sewasetu/backups/sewasetu_backup_20261015_120000.sql.gz"
    exit 1
fi

BACKUP_FILE="$1"
CHECKSUM_FILE="${BACKUP_FILE}.sha256"

if [ ! -f "$BACKUP_FILE" ]; then
    echo "ERROR: Backup file '$BACKUP_FILE' does not exist."
    exit 1
fi

if [ -f "$CHECKSUM_FILE" ]; then
    echo "Verifying SHA-256 integrity checksum..."
    sha256sum -c "$CHECKSUM_FILE"
    echo "Checksum verification PASSED."
else
    echo "WARNING: No .sha256 checksum file found. Proceeding with unverified archive."
fi

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-sewasetu}"
DB_USER="${POSTGRES_USER:-sewasetu}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-sewasetu123}}"

echo "======================================================================"
echo "CRITICAL: You are about to restore into '${DB_NAME}' on ${DB_HOST}:${DB_PORT}."
echo "This will replace existing database records with the backup state."
echo "======================================================================"

if [ -t 0 ]; then
    read -p "Type 'RESTORE' to confirm: " CONFIRM
    if [ "$CONFIRM" != "RESTORE" ]; then
        echo "Restoration aborted."
        exit 1
    fi
fi

echo "Restoring database from ${BACKUP_FILE}..."
gunzip -c "$BACKUP_FILE" | PGPASSWORD="${POSTGRES_PASSWORD}" psql \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --single-transaction

echo "=== Database restored successfully ==="
