build:
  podman compose build

up:
  podman compose up

down:
  podman compose down

check:
  uv run ty check
  uv run ruff check

test:
  uv run pytest
