#!/bin/sh
# Loads the datasets from data/ into the compose db container in one step.
# Idempotent: a database is loaded only when it is missing or when its previous
# load did not finish, so re-running the script skips what is already done.
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

# Like run_psql, but wraps the input in one transaction. The dumps have no
# transaction boundary of their own, so a failure would otherwise leave a
# half-filled database that looks loaded to the checks below.
restore_psql() {
    db=$1
    docker compose exec -T db sh -c \
        'psql -U "$POSTGRES_USER" -d "$1" -X -q --single-transaction -v ON_ERROR_STOP=1 -f -' sh "$db"
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

# A dump that creates its own database cannot be wrapped in a transaction, so
# its progress cannot be rolled back. A comment written only after the restore
# succeeds records that the database is complete; one without it is a failed or
# interrupted attempt.
loaded_marker='setup.sh: cic_scrape.sql loaded'

mark_loaded() {
    printf "COMMENT ON DATABASE %s IS '%s';\n" "$1" "$loaded_marker" | run_psql postgres
}

marker_matches() {
    database_exists "$1" || return 1
    printf "SELECT coalesce(shobj_description(oid, 'pg_database'), '') FROM pg_database WHERE datname = '%s';\n" \
        "$1" | query_psql postgres | grep -qxF "$loaded_marker"
}

echo 'starting the db service'
docker compose up -d db
tries=0
# Checked over TCP: while the entrypoint initializes the volume it runs a
# temporary server on the unix socket only, which a socket check would accept
# before the real server (and the application database) exists.
until docker compose exec -T db pg_isready -q -h 127.0.0.1 -d postgres 2>/dev/null; do
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
    # restore_psql makes this atomic, which is what gives the check above its
    # meaning: the tables only survive a restore that ran to completion.
    # stdout only carries the dump's setval chatter; errors still reach stderr.
    restore_psql "$app_db" <"$data_dir/saber_sip.sql" >/dev/null
    echo "loaded $app_db"
fi

if marker_matches cic_scrape; then
    echo 'cic_scrape: already loaded, skipping the restore'
else
    require_file "$data_dir/cic_scrape.sql"
    if database_exists cic_scrape; then
        echo 'cic_scrape: its restore did not finish, dropping it to retry'
        printf 'DROP DATABASE cic_scrape WITH (FORCE);\n' | run_psql postgres
    fi
    echo "restoring $data_dir/cic_scrape.sql"
    # The dump creates cic_scrape itself, so it runs against the server's own
    # database. It fails if the target database already exists.
    run_psql postgres <"$data_dir/cic_scrape.sql" >/dev/null
    mark_loaded cic_scrape
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
