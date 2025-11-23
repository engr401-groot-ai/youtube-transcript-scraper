#!/bin/bash
# Clear BigQuery table script
# Usage:
#   ./clear_bigquery.sh
# Optional overrides:
#   PROJECT_ID=its-gro DATASET=uh_legis TABLE=transcripts ./clear_bigquery.sh

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-its-gro}"
DATASET="${DATASET:-uh_legis}"
TABLE="${TABLE:-transcripts}"

FULL_TABLE="${PROJECT_ID}.${DATASET}.${TABLE}"

echo "========================================="
echo "Clear BigQuery Table"
echo "========================================="
echo ""
echo "This will DELETE ALL DATA from:"
echo "  ${FULL_TABLE}"
echo ""
read -p "Type 'yes' to continue: " confirm

if [[ "$confirm" != "yes" ]]; then
  echo "Aborted."
  exit 0
fi

echo ""
echo "Deleting all rows from ${FULL_TABLE}..."

bq query --use_legacy_sql=false \
  --project_id="${PROJECT_ID}" \
  "DELETE FROM \`${FULL_TABLE}\` WHERE TRUE"

echo ""
echo "Done! Table cleared."
echo "Verify with:"
echo "  bq query --use_legacy_sql=false \"SELECT COUNT(*) FROM \`${FULL_TABLE}\`\""
