#!/bin/sh
# Make the mounted data dir writable for the unprivileged bot user, then drop privileges.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R bot:bot "${DATA_DIR:-/data}"
    exec gosu bot "$@"
fi
exec "$@"
