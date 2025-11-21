#!/bin/bash

# Clear BigQuery table script 
# ./clear_bigquery.sh

PROJECT_ID="its-gro"
DATASET="uh_legis"
TABLE="transcripts"

echo "========================================="
echo "Clear BigQuery Table"
echo "========================================="
echo ""
echo "This will DELETE ALL DATA from:"
echo "  Project: ${PROJECT_ID}"
echo "  Dataset: ${DATASET}"
echo "  Table: ${TABLE}"
echo ""
echo "Are you sure? This cannot be undone!"
echo ""
read -p "Type 'yes' to continue: " confirm

if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Deleting all rows from ${PROJECT_ID}.${DATASET}.${TABLE}..."

bq query --use_legacy_sql=false \
  --project_id=${PROJECT_ID} \
  "DELETE FROM \`${PROJECT_ID}.${DATASET}.${TABLE}\` WHERE TRUE"

echo ""
echo "Done! Table cleared."
echo ""
echo "To verify, run:"
echo "  bq query --use_legacy_sql=false 'SELECT COUNT(*) FROM \`${PROJECT_ID}.${DATASET}.${TABLE}\`'"
