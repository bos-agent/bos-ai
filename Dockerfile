FROM ghcr.io/astral-sh/uv:latest AS uv

FROM node:22-bookworm-slim AS node

FROM python:3.13-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=uv /uvx /usr/local/bin/uvx
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s /usr/local/lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack

WORKDIR /app

COPY LICENSE README.md pyproject.toml uv.lock /app/
COPY src /app/src

RUN python -m pip install --no-cache-dir .

WORKDIR /workspace

ENTRYPOINT ["tini", "--", "python", "-m", "bos.runner._main"]
CMD ["--workspace", "/workspace"]
