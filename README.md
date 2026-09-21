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
cp .env.example .env
docker compose build
docker compose run --rm --no-deps manager python manage.py generate_keys   # paste into .env
docker compose up -d
docker compose exec manager python manage.py createsuperuser
```

Open http://localhost:8080/ and log in. Add a Mergin account and a database
under Registry before adding a project.

Installations with an existing PostGIS server remove the `postgis` service
from `docker-compose.yml` and point `DATABASE_URL` at their own. The role in
`DATABASE_URL` owns the `dbsync_manager` schema and needs `CREATE` on the
database; it is not the role syncs run as.

## Layout

```
Dockerfile            FROM lutraconsulting/mergin-db-sync; manager on top (§10)
docker-compose.yml    manager + optional postgis
docker/               s6-overlay services: migrate → web + supervise
manager/              Django project
  dbsync_manager/     settings (all from environment), urls
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
