#!/bin/sh
set -eu
cd "$(dirname "$0")"

# Separate installation: the check never deletes the main project's VMs.
export COMPOSE_PROJECT_NAME=cloudmaker-extra-check
export STORAGE_DIR=/var/lib/libvirt/images/cloudmaker-check
export API_PORT=18000
export PROMETHEUS_PORT=19090
export LC_ALL=C

./prepare.sh
docker compose up -d --build --wait --wait-timeout 120
python3 scripts/check.py
