#!/usr/bin/env python3
"""Dela upp en DB Sync-config med flera anslutningar i en container per GeoPackage.

Bakgrund (fynd F3, POC-runbook): alla anslutningar mot samma Mergin-projekt delar
arbetskatalog i DB Sync. Den anslutning som råkar hämta en ny version tar bara sina
egna ändringar; övriga lager ser "No changes on Mergin Maps" och tappar sina.
Öppet uppströms: https://github.com/MerginMaps/db-sync/issues/163
Lutras rekommenderade workaround är separata containrar (egen /tmp/dbsync per lager).

    ./gen_dbsync.py dbsync bonsta          # -> dbsync/conn/<lager>/config.yaml + docker-compose.bonsta.yml
    ./gen_dbsync.py dbsync-test test

Lägg sedan i .env:  COMPOSE_FILE=docker-compose.yml:docker-compose.bonsta.yml[:docker-compose.test.yml]
"""
import copy
import pathlib
import re
import sys

import yaml

IMAGE = "lutraconsulting/mergin-db-sync:2.3.0"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main(cfg_dir: str, name: str) -> None:
    cfg_dir_p = pathlib.Path(cfg_dir)
    cfg = yaml.safe_load((cfg_dir_p / "config.yaml").read_text(encoding="utf-8"))
    conns = cfg.pop("connections")
    services = {}
    for c in conns:
        layer = pathlib.Path(c["sync_file"]).stem            # 1-punkter
        d = cfg_dir_p / "conn" / layer
        (d / "work").mkdir(parents=True, exist_ok=True)
        single = copy.deepcopy(cfg)
        single["connections"] = [c]
        header = (f"# GENERERAD av gen_dbsync.py från {cfg_dir}/config.yaml — redigera inte här.\n"
                  f"# En container per GeoPackage (fynd F3, db-sync issue #163).\n")
        (d / "config.yaml").write_text(header + yaml.safe_dump(single, allow_unicode=True, sort_keys=False),
                                       encoding="utf-8")
        svc = f"dbsync-{slug(name)}-{slug(layer)}"
        services[svc] = {
            "image": IMAGE,
            "platform": "linux/amd64",
            "container_name": f"ak-{svc}",
            "restart": "unless-stopped",
            "depends_on": {"postgis": {"condition": "service_healthy"}},
            "volumes": [f"./{cfg_dir}/conn/{layer}:/config",
                        f"./{cfg_dir}/conn/{layer}/work:/tmp/dbsync"],
            "command": ["/config/config.yaml", "--log-file", "/config/dbsync.log", "--log-verbosity", "INFO"],
            "profiles": ["sync"],
        }
    out = pathlib.Path(f"docker-compose.{slug(name)}.yml")
    out.write_text("# GENERERAD av gen_dbsync.py — en DB Sync-container per GeoPackage. Redigera inte här.\n"
                   + yaml.safe_dump({"services": services}, sort_keys=False), encoding="utf-8")
    print(f"{len(services)} tjänster -> {out}")
    for s in services:
        print("  ", s)
    print(f"Konfig per lager i {cfg_dir}/conn/<lager>/config.yaml (lösenord ingår — ligger utanför git).")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
