FROM python:3.12-slim

ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random \
  # pip:
  PIP_NO_CACHE_DIR=off \
  PIP_DISABLE_PIP_VERSION_CHECK=on \
  PIP_DEFAULT_TIMEOUT=100 \
  # poetry:
  POETRY_VERSION=2.0.1\
  POETRY_VIRTUALENVS_CREATE=false \
  POETRY_CACHE_DIR='/var/cache/pypoetry'

EXPOSE 8000

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    supervisor \
    postgresql-client-15 \
    postgresql-15-repack && \
    mkdir -p /var/log/supervisor && \
    rm -rf /var/lib/apt/lists/*

RUN pip install idna "poetry==$POETRY_VERSION" && poetry --version

RUN mkdir -p /opt/services/app/src
WORKDIR /opt/services/app/src

COPY pyproject.toml poetry.lock* ./

RUN poetry config installer.max-workers 10
RUN poetry install

COPY . .

RUN chmod +x boot.sh

COPY supervisord.conf /etc/supervisor/supervisord.conf