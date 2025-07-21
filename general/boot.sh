#!/bin/bash

echo "Starting System"

set -euo pipefail

alembic upgrade head

exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf