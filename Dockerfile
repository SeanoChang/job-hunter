FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY companies.toml ./
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd -u 1000 -m runner && chown -R runner /app
USER runner
ENTRYPOINT ["job-hunter"]
CMD ["--help"]
