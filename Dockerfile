FROM python:3.12-slim AS base
# uv pinned by version + digest (supply-chain); bump deliberately.
COPY --from=ghcr.io/astral-sh/uv:0.11.31@sha256:ecd4de2f060c64bea0ff8ecb182ddf46ba3fcccdc8a60cfdbaf20d1a047d7437 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY companies.toml ./
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -u 1000 -m runner && chown -R runner /app
USER runner
# One image, two console scripts: the fetcher runs `job-hunter …`, Cloud Run
# overrides the command to `job-hunter-mcp` (spec 2026-09-02 §5). So no
# ENTRYPOINT pins the binary — the command names it, here and on the platform.
EXPOSE 8080
CMD ["job-hunter", "--help"]
