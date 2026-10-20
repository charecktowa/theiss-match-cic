#!/bin/sh
# Loads the datasets from data/ into the compose db container in one step.
# Idempotent: a database is only loaded when it is not there yet, so re-running
# the script after adding data skips what is already done.
set -eu

cd "$(dirname "$0")/.."
data_dir=${DATA_DIR:-data}

require_file() {
    [ -f "$1" ] || {
        echo "missing $1: the datasets are not committed, see README" >&2
        exit 1
    }
}

# $1 is the database to connect to; the SQL comes from stdin.
run_psql() {
    db=$1
    docker compose exec -T db sh -c \
        'psql -U "$POSTGRES_USER" -d "$1" -X -q -v ON_ERROR_STOP=1 -f -' sh "$db"
}

# Like run_psql, but prints bare values, for the checks below.
query_psql() {
    db=$1
    docker compose exec -T db sh -c \
        'psql -U "$POSTGRES_USER" -d "$1" -X -q -A -t -v ON_ERROR_STOP=1 -f -' sh "$db"
}

database_exists() {
    printf "SELECT 1 FROM pg_database WHERE datname = '%s';\n" "$1" |
        query_psql postgres | grep -qx 1
}

table_exists() {
    printf "SELECT to_regclass('%s') IS NOT NULL;\n" "$2" | query_psql "$1" | grep -qx t
}

echo 'starting the db service'
docker compose up -d db
tries=0
until docker compose exec -T db pg_isready -q -d postgres 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -ge 60 ]; then
        echo 'the db service did not become ready' >&2
        exit 1
    fi
    sleep 1
done

# Compose can take POSTGRES_DB from .env without exporting it to this shell.
app_db=$(docker compose exec -T db sh -c 'printf %s "$POSTGRES_DB"') || app_db=
if [ -z "$app_db" ]; then
    echo 'could not read POSTGRES_DB from the db container' >&2
    exit 1
fi

if table_exists "$app_db" public.theses; then
    echo "$app_db: already loaded, skipping the SABER restore"
else
    require_file "$data_dir/saber_sip.sql"
    echo "restoring $data_dir/saber_sip.sql into $app_db"
    # stdout only carries the dump's setval chatter; errors still reach stderr.
    run_psql "$app_db" <"$data_dir/saber_sip.sql" >/dev/null
    echo "loaded $app_db"
fi

if database_exists cic_scrape; then
    echo 'cic_scrape: already exists, skipping the restore'
else
    require_file "$data_dir/cic_scrape.sql"
    echo "restoring $data_dir/cic_scrape.sql"
    # The dump creates cic_scrape itself, so it runs against the server's own
    # database. It fails if the target database already exists.
    run_psql postgres <"$data_dir/cic_scrape.sql" >/dev/null
    echo 'loaded cic_scrape'
fi

if database_exists saber_scrape; then
    echo 'saber_scrape: already exists, skipping (run db/saber_scrape/build.sh to rebuild it)'
else
    require_file "$data_dir/saber_sip.sql"
    echo 'building saber_scrape'
    db/saber_scrape/build.sh
fi

echo
echo 'databases:'
printf 'SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1;\n' |
    query_psql postgres
