# dbsync-manager image (design §10).
#
# Built on the unmodified db-sync image; the manager is added on top and
# db-sync's daemon is started per project by the supervisor as a subprocess.
# Bump DBSYNC_VERSION to upgrade db-sync. Patches in patches/ are applied at
# build time and the directory is empty once upstream has shipped the fix.

ARG DBSYNC_VERSION=2.3.0
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

# --- db-sync patches (only dbsync.py exists in the image; the patch also
#     touches CHANGELOG.md and docs/, which are skipped) ---
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
