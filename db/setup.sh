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

# $1 is the database to query, $2 a query returning one boolean. A failed query
# aborts the script instead of reading as "no", since a "no" can lead to a drop.
check() {
    out=$(printf '%s\n' "$2" | query_psql "$1") || {
        echo "could not query $1" >&2
        exit 1
    }
    [ "$out" = t ]
}

database_exists() {
    check postgres "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = '$1');"
}

table_exists() {
    database_exists "$1" && check "$1" "SELECT to_regclass('$2') IS NOT NULL;"
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
# cic_scrape and saber_scrape are loaded separately below, and the CIC step can
# drop its database, so the application must not share either name.
case $app_db in
    cic_scrape | saber_scrape)
        echo "POSTGRES_DB cannot be $app_db: setup.sh loads that dataset itself" >&2
        exit 1 ;;
esac

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

# The CIC dump creates its database outside a transaction, then loads the schema
# and rows in one, so its tables exist only after a restore that completed. A
# cic_scrape without them is empty, and dropping it loses nothing.
if table_exists cic_scrape catalog.thesis; then
    echo 'cic_scrape: already loaded, skipping the restore'
else
    require_file "$data_dir/cic_scrape.sql"
    if database_exists cic_scrape; then
        echo 'cic_scrape: exists but is empty (its restore was rolled back), dropping it to retry'
        printf 'DROP DATABASE cic_scrape WITH (FORCE);\n' | run_psql postgres
    fi
    echo "restoring $data_dir/cic_scrape.sql"
    # The dump creates cic_scrape itself, so it runs against the server's own
    # database. It fails if the target database already exists.
    run_psql postgres <"$data_dir/cic_scrape.sql" >/dev/null
    echo 'loaded cic_scrape'
fi

# build.sh creates saber_scrape before filling it, but builds its catalog in one
# transaction, so catalog.thesis exists only after a build that completed.
if table_exists saber_scrape catalog.thesis; then
    echo 'saber_scrape: already built, skipping (run db/saber_scrape/build.sh to rebuild it)'
else
    require_file "$data_dir/saber_sip.sql"
    echo 'building saber_scrape'
    # Both set explicitly, so an exported TARGET_DB cannot redirect the rebuild.
    TARGET_DB=saber_scrape SABER_DUMP="$data_dir/saber_sip.sql" db/saber_scrape/build.sh
fi

echo
echo 'databases:'
printf 'SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1;\n' |
    query_psql postgres
