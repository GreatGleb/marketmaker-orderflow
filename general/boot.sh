#!/bin/bash

echo "Starting System"

set -euo pipefail

alembic upgrade head

fastapi run main.py --port 8000 --reload
