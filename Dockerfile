ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

FROM python:${PYTHON_VERSION}-slim

ARG NODE_VERSION
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl git util-linux \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && npm install -g @anthropic-ai/claude-code claude-flow \
    && ln -sf /usr/bin/claude /usr/local/bin/claude \
    && ln -sf /usr/bin/claude-flow /usr/local/bin/claude-flow \
    && claude --version \
    && claude-flow --version \
    && uv --version

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN uv pip install --python /app/.venv/bin/python -e . --no-deps \
    && groupadd -g 1000 fleet \
    && useradd -u 1000 -g 1000 -m -d /home/fleet fleet \
    && mkdir -p /var/lib/fleet \
    && chown -R 1000:1000 /app /var/lib/fleet

ENV HOME=/var/lib/fleet/home \
    XDG_CACHE_HOME=/var/lib/fleet/cache \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/python", "-m", "fleet"]
