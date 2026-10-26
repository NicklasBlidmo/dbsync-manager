# dbsync-manager image (design §10).
#
# Built on the unmodified db-sync image; the manager is added on top and
# db-sync's daemon is started per project by the supervisor as a subprocess.
# Bump DBSYNC_VERSION to upgrade db-sync. Patches in patches/ are applied at
# build time; a patch is deleted once upstream has shipped the fix (0001 did
# in 2.4.0). The tag is the release number on Docker Hub
# (lutraconsulting/mergin-db-sync:2.4.0, published 2026-10-19).

ARG DBSYNC_VERSION=2.4.0
FROM lutraconsulting/mergin-db-sync:${DBSYNC_VERSION}

ARG S6_OVERLAY_VERSION=3.2.0.0
ARG S6_ARCH=x86_64

# --- system: s6-overlay (two processes in one container), psql for hooks ---
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp/
ADD https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz /tmp/
# gdal-bin: ogr2ogr for hooks that import lookup tables (§8, §11)
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils postgresql-client gdal-bin \
    && rm -rf /var/lib/apt/lists/* \
    && tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz \
    && tar -C / -Jxpf /tmp/s6-overlay-${S6_ARCH}.tar.xz \
    && rm -f /tmp/s6-overlay-*.tar.xz

# --- optional newer PostgreSQL client (slice 13, docs/backup.md) ---
# pg_dump refuses a server newer than itself. Empty = the distribution's
# postgresql-client above; PG_CLIENT_MAJOR=16 installs that major from
# apt.postgresql.org. Set in .env (compose passes it as a build arg).
ARG PG_CLIENT_MAJOR=
RUN if [ -n "$PG_CLIENT_MAJOR" ]; then \
        set -e; \
        apt-get update && apt-get install -y --no-install-recommends curl ca-certificates; \
        install -d /usr/share/postgresql-common/pgdg; \
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
        . /etc/os-release; \
        echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list; \
        apt-get update && apt-get install -y --no-install-recommends "postgresql-client-${PG_CLIENT_MAJOR}"; \
        rm -rf /var/lib/apt/lists/*; \
    fi

# --- db-sync patches (only the .py files exist in the image; anything else
#     a patch touches is skipped) ---
COPY patches/ /tmp/patches/
RUN set -e; cd /mergin-db-sync; \
    for p in /tmp/patches/*.patch; do \
        [ -e "$p" ] || continue; \
        echo "applying $(basename "$p")"; \
        git apply --verbose --include='*.py' "$p"; \
    done; \
    rm -rf /tmp/patches

# --- manager ---
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --system --create-home --home-dir /home/manager --shell /usr/sbin/nologin manager \
    && mkdir -p /app /data \
    && chown manager:manager /app /data

COPY --chown=manager:manager manager/ /app/
COPY docker/s6-rc.d/ /etc/s6-overlay/s6-rc.d/
COPY docker/scripts/ /etc/s6-overlay/scripts/
RUN chmod +x /etc/s6-overlay/scripts/* /etc/s6-overlay/s6-rc.d/*/run 2>/dev/null || true

ENV PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=dbsync_manager.settings \
    DBSYNC_DATA_DIR=/data \
    DBSYNC_DAEMON_DIR=/mergin-db-sync \
    S6_KEEP_ENV=1 \
    S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0

WORKDIR /app
# collectstatic needs settings to import; the keys are build-time dummies
# and are not baked into the image (ENV is not used here).
RUN DJANGO_SECRET_KEY=build DBSYNC_MANAGER_SECRET_KEY=build \
    DATABASE_URL=postgres://build@localhost/build \
    python manage.py collectstatic --noinput

EXPOSE 8080
VOLUME ["/data"]
ENTRYPOINT ["/init"]
