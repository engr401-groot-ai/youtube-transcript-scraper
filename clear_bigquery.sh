#!/bin/bash
# Clear BigQuery table script
# Usage:
#   ./clear_bigquery.sh
# Optional overrides:
#   PROJECT_ID=its-gro DATASET=uh_legis TABLE=hearing_videos ./clear_bigquery.sh

# Strict error handling
set -euo pipefail

# Default configuration (can be overridden by env vars)
PROJECT_ID="${PROJECT_ID:-its-gro}"
DATASET="${DATASET:-uh_legis}"
TABLE="${TABLE:-hearing_videos}"

# Fully qualified table name
FULL_TABLE="${PROJECT_ID}.${DATASET}.${TABLE}"

echo "========================================="
echo "      Clear BigQuery Table (TRUNCATE)    "
echo "========================================="
echo ""
echo "TARGET: ${FULL_TABLE}"
echo "ACTION: TRUNCATE TABLE (Removes all rows, keeps schema)"
echo ""
echo "⚠️  WARNING: This operation is irreversible."
echo ""

read -p "Type 'yes' to confirm: " confirm

if [[ "$confirm" != "yes" ]]; then
  echo "Aborted by user."
  exit 0
fi

echo ""
echo "Truncating ${FULL_TABLE}..."

# Uses TRUNCATE TABLE which is free and faster than DELETE
bq query \
  --use_legacy_sql=false \
  --project_id="${PROJECT_ID}" \
  --format=none \
  "TRUNCATE TABLE \`${FULL_TABLE}\`"

echo "✓ Done! Table truncated."
echo ""
echo "Verifying row count..."
bq query \
  --use_legacy_sql=false \
  --project_id="${PROJECT_ID}" \
  --format=pretty \
  "SELECT count(*) as row_count FROM \`${FULL_TABLE}\`"