"""Runtime settings read from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    config_dir: Path
    valkey_url: str
    # Longest time a request waits for free quota before getting 429
    quota_timeout: float
    # Longest silence from an upstream (connecting, waiting for headers or the next chunk)
    upstream_timeout: float

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "Settings":
        return cls(
            config_dir=Path(environ.get("LLM_GATEWAY_CONFIG_DIR", "config")),
            valkey_url=environ.get("VALKEY_URL", "valkey://localhost:6379/0"),
            quota_timeout=float(environ.get("LLM_GATEWAY_QUOTA_TIMEOUT", "300")),
            upstream_timeout=float(environ.get("LLM_GATEWAY_UPSTREAM_TIMEOUT", "600")),
        )
