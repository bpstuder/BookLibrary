#!/bin/sh
# entrypoint.sh — Adjust UID/GID of the app user at runtime.
#
# This allows the container to write files with the same owner as the host user,
# avoiding permission issues on mounted volumes (library, data).
#
# Usage (docker-compose.yml):
#   environment:
#     PUID: 1000   # host user ID  (default: 1000)
#     PGID: 1000   # host group ID (default: 1000)

PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "BookLibrary starting with UID=${PUID} GID=${PGID}"

# Adjust group if PGID differs from the current appgroup GID
if [ "$(getent group appgroup | cut -d: -f3)" != "${PGID}" ]; then
    groupmod -o -g "${PGID}" appgroup
fi

# Adjust user if PUID differs from the current appuser UID
if [ "$(id -u appuser)" != "${PUID}" ]; then
    usermod -o -u "${PUID}" appuser
fi

# Ensure the data directory (DB, covers, config) is owned by the app user.
# The library mount is read-only so we don't touch it.
chown -R appuser:appgroup /app/data 2>/dev/null || true

# Drop privileges and exec the main process
exec gosu appuser "$@"
