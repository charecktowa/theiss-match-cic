#!/bin/sh
# Builds the saber_scrape database from data/saber_sip.sql on the compose db.
# Re-runnable: it drops and recreates only the target database, never theiss.
set -eu

cd "$(dirname "$0")/../.."
target=${TARGET_DB:-saber_scrape}

case $target in
    *[!a-z0-9_]* | '') echo "TARGET_DB must be a lowercase identifier" >&2; exit 1 ;;
esac
if [ "$target" = theiss ]; then
    echo "refusing to overwrite the original theiss database" >&2
    exit 1
fi

# $1 is the database to connect to; the SQL comes from stdin. The single
# quotes keep $POSTGRES_USER for the container's shell, as in the README.
run_psql() {
    docker compose exec -T db sh -c \
        'psql -U "$POSTGRES_USER" -d "$1" -X -q -v ON_ERROR_STOP=1 -f -' sh "$1"
}

echo "recreating database $target"
printf 'DROP DATABASE IF EXISTS %s WITH (FORCE);\nCREATE DATABASE %s;\n' "$target" "$target" \
    | run_psql postgres

echo "restoring data/saber_sip.sql"
run_psql "$target" < data/saber_sip.sql

# One transaction: if any check fails, the database holds only the restore.
echo "building raw, catalog and app"
cat db/saber_scrape/01_raw.sql db/saber_scrape/02_schema.sql db/saber_scrape/03_load.sql db/saber_scrape/04_checks.sql \
    | { echo 'BEGIN;'; cat; echo 'COMMIT;'; } \
    | run_psql "$target"

echo "done: $target"
