set -euo pipefail

SECRET_ID="rds/flask-demo"
REGION="us-east-1"

SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text)

export PGPASSWORD=$(echo "$SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['password'])")
DB_HOST=$(echo "$SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['host'])")
DB_USER=$(echo "$SECRET" | python3 -c "import json,sys; print(json.load(sys.stdin)['username'])")

psql -h "$DB_HOST" -U "$DB_USER" -d logistics -f /opt/app/schema.sql

psql -h "$DB_HOST" -U "$DB_USER" -d logistics -tAc "select (select count(*) from customers),(select count(*) from drivers),(select count(*) from shipments);"
