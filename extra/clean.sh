#!/bin/sh
set -eu
cd "$(dirname "$0")"

docker compose stop app
docker compose run --rm --no-deps app python -m app.clean
docker compose down --volumes --remove-orphans --rmi local
storage=${STORAGE_DIR:-/var/lib/libvirt/images/cloudmaker}
rmdir "$storage"
