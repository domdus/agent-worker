FROM python:3.12-slim-bookworm

ARG VERSION=dev

LABEL \
  org.opencontainers.image.source="https://github.com/domdus/agent-worker" \
  org.opencontainers.image.description="Headless coding agent (Cursor SDK, Claude Code or OpenAI Codex): MCP tool, job API and OpenAI-compatible chat" \
  org.opencontainers.image.licenses="MIT" \
  org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    CODEX_HOME=/data/codex \
    HOST=0.0.0.0 \
    PORT=8787

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client rsync curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 worker \
    && mkdir -p /data && chown worker:worker /data

WORKDIR /app
COPY requirements.txt .
# The Codex CLI pinned with the SDK (statically linked) also goes on PATH as `codex` for manual use.
# Its smoke test runs in a throwaway CODEX_HOME: /data/codex is created later by the worker user.
RUN pip install --no-cache-dir -r requirements.txt \
    && ln -s "$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" /usr/local/bin/codex \
    && CODEX_HOME=/tmp/codex-check codex --version && rm -rf /tmp/codex-check
COPY agent_worker ./agent_worker

USER worker
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" -H "Host: 127.0.0.1" >/dev/null || exit 1
CMD ["python", "-m", "agent_worker.app"]
