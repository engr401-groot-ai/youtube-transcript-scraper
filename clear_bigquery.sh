#!/bin/bash
set -euo pipefail

# Clear BigQuery table(s)
# Usage:
#   ./clear_bigquery.sh
#   ./clear_bigquery.sh transcripts_json
#   ./clear_bigquery.sh transcripts_json uh_mentions

PROJECT_ID="${PROJECT_ID:-its-gro}"
DATASET="${DATASET:-uh_legis}"

# Default tables for THIS project
if [ "$#" -eq 0 ]; then
  TABLES=("transcripts_json")
else
  TABLES=("$@")
fi

echo "========================================="
echo "Clear BigQuery Table(s)"
echo "========================================="
echo ""
echo "This will DELETE ALL DATA from:"
echo "  Project: ${PROJECT_ID}"
echo "  Dataset: ${DATASET}"
for t in "${TABLES[@]}"; do
  echo "  Table:   ${t}"
done
echo ""
echo "Are you sure? This cannot be undone!"
echo ""

read -p "Type 'yes' to continue: " confirm
if [ "${confirm}" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

echo ""
for t in "${TABLES[@]}"; do
  echo "Truncating ${PROJECT_ID}.${DATASET}.${t} ..."
  bq query --use_legacy_sql=false \
    --project_id="${PROJECT_ID}" \
    "TRUNCATE TABLE \`${PROJECT_ID}.${DATASET}.${t}\`"
done

echo ""
echo "Done! Table(s) cleared."
echo ""
echo "To verify, run:"
for t in "${TABLES[@]}"; do
  echo "  bq query --use_legacy_sql=false --project_id=${PROJECT_ID} 'SELECT COUNT(*) AS n FROM \`${PROJECT_ID}.${DATASET}.${t}\`'"
done
echo ""
