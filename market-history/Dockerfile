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

RUN apt-get update \
    # Cleaning cache:  \
  && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/* \
  # Installing `poetry` package manager:
  # https://github.com/python-poetry/poetry
  && pip install idna "poetry==$POETRY_VERSION" && poetry --version

RUN mkdir -p /opt/services/app/src
WORKDIR /opt/services/app/src

COPY . /opt/services/app/src
COPY ./boot.sh /boot.sh

RUN poetry config installer.max-workers 10

# Project initialization:
RUN poetry install

RUN chmod +x /boot.sh
