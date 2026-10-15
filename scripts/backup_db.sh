#!/usr/bin/env bash
# ==============================================================================
# Sewa Setu — Production Database Automated Backup Script
#
# Generates timestamped, gzip-compressed PostgreSQL dumps, creates SHA-256
# integrity checksums, manages backup retention, and optionally archives
# to OpenStack Swift object storage.
# ==============================================================================

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/sewasetu/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-sewasetu}"
DB_USER="${POSTGRES_USER:-sewasetu}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-${DB_PASSWORD:-sewasetu123}}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

BACKUP_FILE="${BACKUP_DIR}/sewasetu_backup_${TIMESTAMP}.sql.gz"
CHECKSUM_FILE="${BACKUP_FILE}.sha256"

echo "=== [$(date)] Starting Sewa Setu Database Backup ==="
echo "Target Database: ${DB_NAME} on ${DB_HOST}:${DB_PORT} as user ${DB_USER}"
echo "Backup Output: ${BACKUP_FILE}"

# Execute pg_dump with custom compression
PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump \
    -h "$DB_HOST" \
    -p "$DB_PORT" \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --format=plain \
    --no-owner \
    --no-privileges \
    | gzip -9 > "$BACKUP_FILE"

# Generate SHA-256 Checksum
sha256sum "$BACKUP_FILE" > "$CHECKSUM_FILE"
chmod 600 "$BACKUP_FILE" "$CHECKSUM_FILE"

FILESIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "Backup generated successfully (${FILESIZE}). Checksum:"
cat "$CHECKSUM_FILE"

# Optional: Upload to OpenStack Swift object storage if configured
if command -v openstack > /dev/null 2>&1 && [ -n "${OS_AUTH_URL:-}" ]; then
    echo "Archiving backup to OpenStack Swift container 'sewasetu-db-backups'..."
    openstack container create sewasetu-db-backups || true
    openstack object create sewasetu-db-backups "$BACKUP_FILE"
    openstack object create sewasetu-db-backups "$CHECKSUM_FILE"
    echo "Uploaded to Swift container successfully."
fi

# Prune old backups older than retention window
echo "Cleaning up backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -name "sewasetu_backup_*.sql.gz*" -mtime +"$RETENTION_DAYS" -exec rm -f {} +

echo "=== [$(date)] Backup process completed successfully ==="
