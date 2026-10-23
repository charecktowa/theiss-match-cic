#!/bin/sh
# Builds the saber_scrape database from data/saber_sip.sql (or $SABER_DUMP) on
# the compose db.
# Re-runnable: it drops and recreates only the target database, never theiss.
set -eu

cd "$(dirname "$0")/../.."
target=${TARGET_DB:-saber_scrape}
dump=${SABER_DUMP:-data/saber_sip.sql}

case $target in
    *[!a-z0-9_]* | '') echo "TARGET_DB must be a lowercase identifier" >&2; exit 1 ;;
esac
# The application's database is read from the db container itself: compose can
# take POSTGRES_DB from .env without exporting it to this shell. If it cannot be
# read, nothing is dropped.
app_db=$(docker compose exec -T db sh -c 'printf %s "$POSTGRES_DB"') || app_db=
if [ -z "$app_db" ]; then
    echo "could not read POSTGRES_DB from the db container; refusing to drop anything" >&2
    exit 1
fi
# The target is dropped, so every database the stack depends on is refused:
# the application's, the server's own, and the CIC catalog.
case $target in
    theiss | "$app_db" | postgres | template0 | template1 | cic_scrape)
        echo "refusing to drop and rebuild the $target database" >&2
        exit 1 ;;
esac

# $1 is the database to connect to; the SQL comes from stdin. The single
# quotes keep $POSTGRES_USER for the container's shell, as in the README.
run_psql() {
    docker compose exec -T db sh -c \
        'psql -U "$POSTGRES_USER" -d "$1" -X -q -v ON_ERROR_STOP=1 -f -' sh "$1"
}

echo "recreating database $target"
printf 'DROP DATABASE IF EXISTS %s WITH (FORCE);\nCREATE DATABASE %s;\n' "$target" "$target" \
    | run_psql postgres

echo "restoring $dump"
run_psql "$target" < "$dump"

# One transaction: if any check fails, the database holds only the restore.
echo "building raw, catalog and app"
cat db/saber_scrape/01_raw.sql db/saber_scrape/02_schema.sql db/saber_scrape/03_load.sql db/saber_scrape/04_checks.sql \
    | { echo 'BEGIN;'; cat; echo 'COMMIT;'; } \
    | run_psql "$target"

echo "done: $target"
