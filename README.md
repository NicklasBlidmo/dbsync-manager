# dbsync-manager

Web UI and supervisor for [Mergin Maps DB Sync](https://github.com/MerginMaps/db-sync):
set up, monitor, pause and remove synchronisations between Mergin Maps
projects and PostGIS, packaged as one Docker container.

The design is in [docs/dbsync-manager-design.md](docs/dbsync-manager-design.md).
Section numbers in code comments (§6, §8, ...) refer to it.

## Status

Slice 1 of 6 (design §14): registry, encrypted credentials, Django admin as
the UI, Docker image and compose file, supervisor process frame. The
supervisor does not start db-sync yet.

## Quick start

```sh
cp .env.example .env && chmod 600 .env
python3 manager/generate_keys.py --write .env    # fills the two key lines in place
# edit .env: DATABASE_URL, ALLOWED_HOSTS, and BIND_IP/HTTP_PORT if 8080 is taken
docker compose build
docker compose up -d                      # existing PostGIS server
# docker compose --profile postgis up -d  # or: bundled PostGIS
docker compose logs -f manager            # "preflight: ... ok", migrations, "tick: no projects"
docker compose exec manager python manage.py createsuperuser
```

Open `http://<ALLOWED_HOSTS entry>:<HTTP_PORT>/` and log in with the account
you just created; there are no default credentials. Add a Mergin account and
a database under Registry before adding a project.

No Python on the host? Generate the keys with the image instead, bypassing
its entrypoint (which would run migrate first), and paste the two values
onto the existing `DJANGO_SECRET_KEY=` and `DBSYNC_MANAGER_SECRET_KEY=` lines:

```sh
docker compose run --rm --no-deps --entrypoint python manager generate_keys.py
```

Copy `DBSYNC_MANAGER_SECRET_KEY` somewhere safe outside the server. Once a
credential has been saved it cannot be replaced (design §10).

### When it does not start

The container runs a preflight before `migrate` and prints one line per
problem, prefixed `dbsync-manager preflight:`: a missing or malformed key, a
malformed `DATABASE_URL`, an unreachable server, a refused login, a missing
schema the role may not create. It then waits `DBSYNC_FAIL_DELAY` seconds
before the restart policy tries again. Fix `.env` and run
`docker compose up -d`; Compose recreates the container when `.env` changed.

"CSRF verification failed" at login, with a correct `.env`, means the browser
holds cookies from another application on the same host name. The manager
uses its own cookie names to avoid this; clear the site data once if the
browser still has cookies from an older build.

### Database

The `postgis` service is behind the Compose profile `postgis` and is not
started by a plain `docker compose up`. Most installations already have a
PostGIS server and point `DATABASE_URL` at it; from inside the container
that is the server's LAN address or a shared Docker network, never
`localhost`.

The role in `DATABASE_URL` owns the `dbsync_manager` schema and nothing
else; it is not the role syncs run as. Either give it `CREATE` on the
database, or pre-create the schema and grant nothing more:

```sql
CREATE ROLE dbsync_manager LOGIN PASSWORD '...';
CREATE SCHEMA dbsync_manager AUTHORIZATION dbsync_manager;
```

All manager tables, Django's own included, live in that schema. Nothing is
created in `public` or in synced schemas.

## Layout

```
Dockerfile            FROM lutraconsulting/mergin-db-sync; manager on top (§10)
docker-compose.yml    manager + optional postgis
docker/               s6-overlay services: migrate → web + supervise
manager/              Django project
  dbsync_manager/     settings (all from environment), urls
  generate_keys.py    standalone key generator (no Django needed)
  preflight.py        start-up checks with one-line messages
  registry/           data model (§5), crypto (§4), admin, groups (§4)
  supervisor/         `manage.py supervise` (§6)
patches/              applied to db-sync at image build (#163)
tools/                check_sync.sh, standalone cron check without the manager
docs/                 design document
```

## Development

```sh
python -m venv .venv && . .venv/bin/activate      # or .venv\Scripts\activate
pip install -r requirements.txt
cd manager
export DJANGO_SECRET_KEY=dev DBSYNC_MANAGER_SECRET_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
export DATABASE_URL=postgres://postgres:postgres@localhost:5432/gis
python manage.py check
python manage.py test registry        # unit tests, no database needed
python manage.py migrate && python manage.py runserver
```
