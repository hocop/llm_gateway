import re
from pathlib import Path

import pytest

from llm_gateway.config import ConfigError, UpstreamRoute, VirtualKey, VirtualRoute, load_config
from tests.helpers import (
    PROVIDERS_TOML,
    SECRETS,
    SECRETS_ENV,
    VIRTUAL_KEYS_TOML,
    VIRTUAL_MODELS_TOML,
    write_config,
)

EXAMPLE_CONFIG_DIR = Path(__file__).parent.parent / "examples" / "config"


def test_example_config_loads() -> None:
    environ = {"LLM_KEY_ME": "a", "LLM_KEY_MY_FRIEND": "b", "LLM_KEY_MY_SERVICE_1": "c"}
    config = load_config(EXAMPLE_CONFIG_DIR, environ)

    assert config.models["fast_model"].routes == (UpstreamRoute("vllm", "qwen3.8_27B-fp8"),)
    assert config.models["any_model"].routes == (VirtualRoute("fast_model"), VirtualRoute("smart_model"))
    assert config.referenced_models("any_model") == ["any_model", "fast_model", "smart_model"]
    assert config.keys["my_friend"].max_concurrency("smart_model") == 0.5
    assert config.find_key("c") is config.keys["my_service_1"]


def test_real_model_names_may_contain_slashes(tmp_path: Path) -> None:
    models = VIRTUAL_MODELS_TOML.replace('"vllm/qwen-fast"', '"vllm/Qwen/Qwen3-8B"')
    config = load_config(write_config(tmp_path, virtual_models=models), SECRETS_ENV)

    assert config.models["fast_model"].routes == (UpstreamRoute("vllm", "Qwen/Qwen3-8B"),)


def test_keys_are_found_by_secret(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), SECRETS_ENV)

    assert config.find_key(SECRETS["limited"]) is config.keys["limited"]
    assert config.find_key("wrong") is None
    assert config.find_key("") is None


def test_key_access_and_quotas() -> None:
    key = VirtualKey.model_validate(
        {"name": "key", "models": ["fast_*", "embedding_model"], "quotas": [{"model": "fast_a", "max_concurrency": 2}]}
    )

    assert key.allows("fast_a") and key.allows("fast_b") and key.allows("embedding_model")
    assert not key.allows("smart_model")
    assert key.max_concurrency("fast_a") == 2
    assert key.max_concurrency("fast_b") is None


@pytest.mark.parametrize(
    ("files", "error"),
    [
        ({"providers": PROVIDERS_TOML + PROVIDERS_TOML}, "Duplicate provider 'llama_cpp'"),
        ({"providers": PROVIDERS_TOML.replace("url", "address")}, "providers.toml"),
        ({"providers": "[[providers]\n"}, "providers.toml"),
        ({"providers": PROVIDERS_TOML.replace("http://llama.test/v1", "llama.test/v1")}, "should match pattern"),
        (
            {"virtual_models": VIRTUAL_MODELS_TOML.replace('"vllm/qwen-fast"', '"openai/gpt"')},
            "Virtual model 'fast_model' routes to unknown provider 'openai'",
        ),
        (
            {"virtual_models": VIRTUAL_MODELS_TOML.replace('"/smart_model"', '"/missing"')},
            "Virtual model 'any_model' routes to unknown virtual model 'missing'",
        ),
        (
            {"virtual_models": VIRTUAL_MODELS_TOML.replace('"llama_cpp/qwen-smart"', '"/any_model"')},
            "references itself",
        ),
        ({"virtual_models": VIRTUAL_MODELS_TOML.replace('"vllm/qwen-fast"', '"qwen-fast"')}, "'provider/model'"),
        ({"virtual_models": VIRTUAL_MODELS_TOML.replace('"vllm/qwen-fast"', "[]")}, "non-empty list"),
        ({"virtual_keys": VIRTUAL_KEYS_TOML.replace('name = "me"', 'name = "Me"')}, "should match pattern"),
        (
            {"virtual_keys": VIRTUAL_KEYS_TOML.replace('["fast_model", "smart_model"]', '["fast_model", "gpt"]')},
            "Virtual key 'limited' allows unknown virtual model 'gpt'",
        ),
        (
            {"virtual_keys": VIRTUAL_KEYS_TOML.replace('["fast_model", "smart_model"]', '["fast_model", "smrt_*"]')},
            "Virtual key 'limited' allows unknown virtual model 'smrt_*'",
        ),
        (
            {"virtual_keys": VIRTUAL_KEYS_TOML.replace('model = "smart_model", max_concurrency = 0.5', 'model = "gpt"')},
            "Field required",
        ),
        (
            {
                "virtual_keys": VIRTUAL_KEYS_TOML.replace(
                    '{ model = "smart_model", max_concurrency = 0.5 }', '{ model = "gpt", max_concurrency = 0.5 }'
                )
            },
            "Virtual key 'limited' has a quota for unknown virtual model 'gpt'",
        ),
        (
            {
                "virtual_keys": VIRTUAL_KEYS_TOML.replace(
                    '{ model = "smart_model", max_concurrency = 0.5 }', '{ model = "fast_model", max_concurrency = 0.5 }'
                )
            },
            "Virtual key 'limited' has several quotas for one virtual model",
        ),
        (
            {"virtual_keys": VIRTUAL_KEYS_TOML.replace("max_concurrency = 0.5", "max_concurrency = 0.05")},
            "greater than or equal to 0.1",
        ),
        (
            {"virtual_keys": VIRTUAL_KEYS_TOML.replace("max_concurrency = 0.5", "max_concurrency = 0.505")},
            "multiple of 0.01",
        ),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, files: dict[str, str], error: str) -> None:
    write_config(tmp_path, **files)

    with pytest.raises(ConfigError, match=re.escape(error)):
        load_config(tmp_path, SECRETS_ENV)


def test_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="providers.toml"):
        load_config(tmp_path, SECRETS_ENV)


def test_key_secrets_must_be_set_and_unique(tmp_path: Path) -> None:
    write_config(tmp_path)
    without_secret = {name: secret for name, secret in SECRETS_ENV.items() if name != "LLM_KEY_LIMITED"}

    with pytest.raises(ConfigError, match="set the LLM_KEY_LIMITED environment variable"):
        load_config(tmp_path, without_secret)
    with pytest.raises(ConfigError, match="same secret as another key"):
        load_config(tmp_path, SECRETS_ENV | {"LLM_KEY_LIMITED": SECRETS["me"]})
