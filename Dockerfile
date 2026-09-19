# Why this file exists: Render has a native Python runtime, but a Dockerfile makes
# every backend in this project deploy the same way (Java and C# require one).
#
# JS/TS vs Python: the multi-stage layout has THREE stages built from one file.
#   base    - production dependencies and the app code
#   test    - base plus the dev dependencies and tests (`docker build --target test`)
#   runtime - base plus a non-root user: the image Render runs (the last stage,
#             so a plain `docker build .` produces it)
# There is no compile step: Python ships source, so the "build" is just installing
# dependencies into the image (the moral equivalent of `npm ci`).
FROM python:3.13-slim AS base
# PYTHONUNBUFFERED: print logs immediately instead of buffering (so `docker logs`
# and Render's log tab are live). PYTHONDONTWRITEBYTECODE: skip .pyc files.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /srv
# Copy the dependency list FIRST so this layer is cached until requirements.txt
# changes, not re-run on every code edit.
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app

FROM base AS test
COPY requirements-dev.txt pyproject.toml ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
# Run as an unprivileged user: if the app were ever compromised, it can't write
# to the system.
RUN useradd --create-home app
USER app
EXPOSE 8080
# `sh -c` so ${PORT:-8080} is expanded (Render injects PORT); `exec` makes uvicorn
# the main process so it receives SIGTERM directly and can shut down gracefully.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8080}"]
