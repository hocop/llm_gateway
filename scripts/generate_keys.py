"""Writes secrets for the virtual keys that have none yet into `.env`.

Secrets already in the file are never changed, so running this after adding a virtual key
only fills in the new one. The gateway accepts any non-empty secret; `sk-<uuid4>` is just
the shape generated here.
"""

import argparse
import tomllib
from pathlib import Path
from uuid import uuid4

KEY_ENV_PREFIX = "LLM_KEY_"  # the gateway reads the secret of the key <name> from LLM_KEY_<NAME>


def _key_names(config_dir: Path) -> list[str]:
    """Names of the virtual keys. Only the names are needed, so the rest is left to startup validation."""
    with (config_dir / "virtual_keys.toml").open("rb") as file:
        return [key["name"] for key in tomllib.load(file)["virtual_keys"]]


def _defined_variables(text: str) -> set[str]:
    """Names of the variables already in the file, comments ignored."""
    lines = (line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return {line.split("=", 1)[0].strip() for line in lines if "=" in line}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate missing virtual key secrets")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()

    text = args.env_file.read_text() if args.env_file.exists() else ""
    defined = _defined_variables(text)
    missing = [name for name in _key_names(args.config_dir) if KEY_ENV_PREFIX + name.upper() not in defined]
    if not missing:
        print(f"{args.env_file}: every virtual key already has a secret")
        return

    # The file is rewritten with its old content first, so existing secrets and other variables stay untouched
    separator = "" if not text or text.endswith("\n") else "\n"
    additions = "".join(f"{KEY_ENV_PREFIX}{name.upper()}=sk-{uuid4()}\n" for name in missing)
    args.env_file.write_text(text + separator + additions)
    print(f"{args.env_file}: added a secret for {', '.join(missing)}")


if __name__ == "__main__":
    main()
