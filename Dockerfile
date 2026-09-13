# Image of the gateway itself. Config is not copied in: it is mounted at /app/config (see docker-compose.yml).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies are installed first, so editing the source does not reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY main.py ./
COPY llm_gateway/ ./llm_gateway/

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 4000
CMD ["python", "main.py", "--host", "0.0.0.0", "--port", "4000"]
