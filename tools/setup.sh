#!/bin/sh
# dbsync-manager setup: one command for a Linux host with Docker.
#
#   sh tools/setup.sh
#
# Asks what .env needs, with defaults, writes .env, generates the keys, builds
# the image, starts it, waits for the health check and offers to create the
# first user. Plain POSIX sh; Python on the host is optional (without it the
# keys come from the image). Idempotent: a value already filled in .env is
# never overwritten, and a re-run asks only for what is still empty.
#
# Non-interactive (repeatable installs, the clean-room test): every answer can
# be given as an environment variable, and with no terminal every unanswered
# question takes its default:
#
#   SETUP_PROFILE=postgis|external      bundled PostGIS or an existing server
#   SETUP_DATABASE_URL=...              external: the registry connection
#   SETUP_POSTGRES_PASSWORD=...         bundled: the postgres superuser
#   SETUP_MANAGER_DB_PASSWORD=...       bundled: the registry role
#   SETUP_SYNC_DB_PASSWORD=...          bundled: the sync role
#   SETUP_BIND_IP=0.0.0.0  SETUP_HTTP_PORT=8080  SETUP_ALLOWED_HOSTS=192.0.2.10
#   SETUP_TZ=Europe/Stockholm  SETUP_PROJECT_NAME=dbsync-manager
#   SETUP_YES=1                         no pauses
#   DJANGO_SUPERUSER_USERNAME/PASSWORD/EMAIL   create the first user without a prompt
#
# Values written to .env must survive Compose: no $, #, quotes or spaces
# (CLAUDE.md); the script refuses them.
set -eu

cd "$(dirname "$0")/.."

say() { printf '%s\n' "$*"; }
die() { printf 'setup: %s\n' "$*" >&2; exit 1; }

interactive=0
if [ -t 0 ] && [ -t 1 ]; then interactive=1; fi
if [ "${SETUP_YES:-0}" = 1 ]; then pauses=0; else pauses=$interactive; fi

# --- checks -----------------------------------------------------------------

[ "$(uname -s)" = Linux ] || die "this script is for a Linux host with Docker; on other systems follow the README by hand"
command -v docker >/dev/null 2>&1 || die "docker is not installed (https://docs.docker.com/engine/install/)"
compose_version=$(docker compose version --short 2>/dev/null || true)
[ -n "$compose_version" ] || die "'docker compose' (v2) is not available; Compose 2.20 or newer is required"
major=${compose_version%%.*}; rest=${compose_version#*.}; minor=${rest%%.*}
case "$major$minor" in *[!0-9]*) major=0; minor=0;; esac
if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 20 ]; }; then
    die "docker compose $compose_version is too old; 2.20 or newer is required (depends_on 'required: false')"
fi
[ -f .env.example ] || die "run from the repository: .env.example not found"

# --- .env helpers -----------------------------------------------------------

env_get() {   # env_get KEY: the value Compose would use (the last line wins)
    grep "^$1=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

env_set() {   # env_set KEY VALUE: on the key's first line, other lines for it dropped, appended if absent
    awk -v k="$1" -v v="$2" '
        BEGIN { done = 0 }
        index($0, k "=") == 1 { if (!done) { print k "=" v; done = 1 }; next }
        { print }
        END { if (!done) print k "=" v }
    ' .env > .env.setup-tmp
    cat .env.setup-tmp > .env      # keeps the mode and inode of .env
    rm -f .env.setup-tmp
}

env_safe() {   # true when VALUE survives an env file unchanged
    case "$1" in
        *[\$\#\'\"\ ]*|*"	"*) return 1 ;;
    esac
    return 0
}

ask() {   # ask VAR "prompt" DEFAULT -> $VAR; SETUP_VAR wins, no terminal takes the default
    var=$1; prompt=$2; default=$3
    eval "preset=\${SETUP_$var:-}"
    while :; do
        if [ -n "$preset" ]; then
            value=$preset; preset=""
        elif [ "$interactive" = 1 ]; then
            if [ -n "$default" ]; then printf '%s [%s]: ' "$prompt" "$default"; else printf '%s: ' "$prompt"; fi
            read -r value || value=""
            [ -n "$value" ] || value=$default
        else
            value=$default
        fi
        if env_safe "$value"; then break; fi
        [ "$interactive" = 1 ] || die "$var: '$value' contains \$, #, quotes or spaces, which an env file cannot carry"
        say "  no \$, #, quotes or spaces, please"
    done
    eval "$var=\$value"
}

ask_if_empty() {   # ask_if_empty KEY "prompt" DEFAULT: writes KEY to .env unless it is already filled in
    current=$(env_get "$1")
    if [ -n "$current" ] && [ "$current" != "${4:-}" ]; then
        eval "$1=\$current"
        return 0
    fi
    ask "$1" "$2" "$3"
    eval "env_set \"$1\" \"\$$1\""
}

gen_secret() {   # 24 random bytes as letters and digits: env-file safe, psql safe
    head -c 36 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32
}

port_taken() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    else
        return 1
    fi
}

# --- .env -------------------------------------------------------------------

if [ ! -f .env ]; then
    cp .env.example .env
    say "created .env from .env.example"
fi
chmod 600 .env

say "dbsync-manager setup. Enter accepts the value in brackets."
say ""

# Installation name and port first: a second installation on the same host
# needs its own project name and port (and gets port-suffixed cookies).
ask_if_empty COMPOSE_PROJECT_NAME "Compose project name (a second installation on this host needs its own)" "${SETUP_PROJECT_NAME:-dbsync-manager}"

current_port=$(env_get HTTP_PORT)
default_port=${SETUP_HTTP_PORT:-${current_port:-8080}}
if [ -z "${SETUP_HTTP_PORT:-}" ] && port_taken "$default_port"; then
    p=$default_port
    while port_taken "$p"; do p=$((p + 1)); done
    say "port $default_port is in use on this host; $p is free"
    default_port=$p
fi
ask HTTP_PORT "Host port for the web UI" "$default_port"
case "$HTTP_PORT" in *[!0-9]*|"") die "HTTP_PORT must be a number" ;; esac
if port_taken "$HTTP_PORT" && ! docker compose ps --status running 2>/dev/null | grep -q "[:]$HTTP_PORT->"; then
    die "port $HTTP_PORT is in use by something else on this host (ss -ltn | grep -w $HTTP_PORT); choose another"
fi
env_set HTTP_PORT "$HTTP_PORT"

ask_if_empty BIND_IP "Address to listen on (0.0.0.0 = every interface)" "${SETUP_BIND_IP:-0.0.0.0}" "0.0.0.0"

lan=$(hostname -I 2>/dev/null | awk '{print $1}')
ask_if_empty ALLOWED_HOSTS "Host names or addresses people will type in the browser (comma-separated)" "${SETUP_ALLOWED_HOSTS:-${lan:-localhost}}"

first_host=${ALLOWED_HOSTS%%,*}
ask_if_empty DBSYNC_BASE_URL "Address used in alert mails" "http://$first_host:$HTTP_PORT" "http://localhost:8080"

tz_default=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || true)
ask_if_empty TZ "Time zone for logs and the UI" "${SETUP_TZ:-${tz_default:-UTC}}" "UTC"

# Database: bundled or external.
current_profiles=$(env_get COMPOSE_PROFILES)
case "$current_profiles" in *postgis*) profile_default=postgis ;; *) profile_default=${SETUP_PROFILE:-external} ;; esac
ask PROFILE "Database: 'postgis' (bundled, created for you) or 'external' (an existing PostGIS server)" "$profile_default"
case "$PROFILE" in
    postgis)
        env_set COMPOSE_PROFILES postgis
        pg_db=$(env_get POSTGRES_DB); pg_db=${pg_db:-gis}
        env_set POSTGRES_DB "$pg_db"
        pg_user=$(env_get POSTGRES_USER); env_set POSTGRES_USER "${pg_user:-postgres}"
        ask_if_empty POSTGRES_PASSWORD "Password for the postgres superuser" "${SETUP_POSTGRES_PASSWORD:-$(gen_secret)}" "change-me"
        ask_if_empty MANAGER_DB_PASSWORD "Password for the registry role dbsync_manager" "${SETUP_MANAGER_DB_PASSWORD:-$(gen_secret)}" "change-me-too"
        ask_if_empty SYNC_DB_PASSWORD "Password for the sync role (the first Database row)" "${SETUP_SYNC_DB_PASSWORD:-$(gen_secret)}" "change-me-three"
        sync_role=$(env_get SYNC_DB_ROLE); sync_role=${sync_role:-dbsync_sync}
        env_set SYNC_DB_ROLE "$sync_role"
        # The same password twice: env files are not interpolated.
        env_set DATABASE_URL "postgres://dbsync_manager:$MANAGER_DB_PASSWORD@postgis:5432/$pg_db"
        ;;
    external)
        current_url=$(env_get DATABASE_URL)
        case "$current_url" in
            ""|*@postgis:*|*change-me*) current_url="" ;;
        esac
        if [ -n "$current_url" ]; then
            DATABASE_URL=$current_url
        else
            ask DATABASE_URL "Registry connection, postgres://role:password@host:5432/db (see .env.example for the role)" "${SETUP_DATABASE_URL:-}"
            [ -n "$DATABASE_URL" ] || die "DATABASE_URL is required for an external server"
            env_set DATABASE_URL "$DATABASE_URL"
        fi
        ;;
    *) die "PROFILE must be 'postgis' or 'external'" ;;
esac

# --- keys -------------------------------------------------------------------

need_keys=0
[ -n "$(env_get DJANGO_SECRET_KEY)" ] && [ -n "$(env_get DBSYNC_MANAGER_SECRET_KEY)" ] || need_keys=1
built=0
if [ "$need_keys" = 1 ]; then
    if command -v python3 >/dev/null 2>&1; then
        python3 manager/generate_keys.py --write .env
    else
        say "no python3 on the host: building the image to generate the keys with it"
        docker compose build
        built=1
        keys=$(docker compose run --rm --no-deps --entrypoint python manager generate_keys.py)
        for key in DJANGO_SECRET_KEY DBSYNC_MANAGER_SECRET_KEY; do
            value=$(printf '%s\n' "$keys" | grep "^$key=" | cut -d= -f2-)
            [ -n "$value" ] || die "could not generate $key with the image"
            [ -n "$(env_get "$key")" ] || env_set "$key" "$value"
        done
    fi
    say ""
    say "Store this key in the password manager now. It encrypts every credential"
    say "in the registry; without it they cannot be read, and it must never be"
    say "replaced once a credential has been saved (design section 10):"
    say ""
    say "  DBSYNC_MANAGER_SECRET_KEY=$(env_get DBSYNC_MANAGER_SECRET_KEY)"
    say ""
    if [ "$pauses" = 1 ]; then
        printf 'Press Enter when it is stored... '
        read -r _ || true
    fi
fi

# --- build, start, wait -----------------------------------------------------

[ "$built" = 1 ] || docker compose build
docker compose up -d

db_wait=$(env_get DBSYNC_DB_WAIT); db_wait=${db_wait:-30}
deadline=$((db_wait + 60))
say "waiting up to ${deadline}s for the health check..."
elapsed=0
healthy=0
while [ "$elapsed" -lt "$deadline" ]; do
    if docker compose exec -T manager python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)" >/dev/null 2>&1; then
        healthy=1
        break
    fi
    sleep 5
    elapsed=$((elapsed + 5))
    docker compose logs --no-log-prefix --since 6s manager 2>/dev/null | grep -E 'preflight|start aborted' || true
done
if [ "$healthy" != 1 ]; then
    say ""
    docker compose logs --no-log-prefix --tail 30 manager 2>/dev/null || true
    die "the manager did not become healthy within ${deadline}s; the lines above say why (README: When it does not start)"
fi
say "manager is up"

# --- first user -------------------------------------------------------------

has_superuser() {
    docker compose exec -T manager python manage.py shell -c \
        "import sys; from django.contrib.auth.models import User; sys.exit(0 if User.objects.filter(is_superuser=True).exists() else 1)" \
        >/dev/null 2>&1
}
if has_superuser; then
    say "a superuser already exists; skipping createsuperuser"
elif [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    docker compose exec -T \
        -e "DJANGO_SUPERUSER_USERNAME=$DJANGO_SUPERUSER_USERNAME" \
        -e "DJANGO_SUPERUSER_PASSWORD=$DJANGO_SUPERUSER_PASSWORD" \
        -e "DJANGO_SUPERUSER_EMAIL=${DJANGO_SUPERUSER_EMAIL:-}" \
        manager python manage.py createsuperuser --noinput
elif [ "$interactive" = 1 ]; then
    say "Create the first user (it can log in to everything, including Settings):"
    docker compose exec manager python manage.py createsuperuser
else
    say "no terminal: create the first user later with"
    say "  docker compose exec manager python manage.py createsuperuser"
fi

# --- summary ----------------------------------------------------------------

say ""
say "Done. Open http://$first_host:$HTTP_PORT/ and log in."
if [ "$PROFILE" = postgis ]; then
    say ""
    say "First Settings -> Databases row (the bundled PostGIS, created at first start):"
    say "  Name      gis"
    say "  Host      postgis"
    say "  Port      5432"
    say "  Dbname    $pg_db"
    say "  User      $sync_role"
    say "  Password  the SYNC_DB_PASSWORD line in .env"
    say "Then a Mergin account under Settings -> Mergin accounts, and Add sync."
fi
