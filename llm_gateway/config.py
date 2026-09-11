"""Gateway configuration: providers, virtual models and virtual keys loaded from TOML files."""

import fnmatch
import hmac
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from llm_gateway.quota import MIN_BUDGET

KEY_ENV_PREFIX = "LLM_KEY_"


class ConfigError(Exception):
    """Configuration files are missing, malformed or inconsistent."""


@dataclass(frozen=True)
class UpstreamRoute:
    """A real model served by a provider, written as `provider/model`."""

    provider: str
    model: str


@dataclass(frozen=True)
class VirtualRoute:
    """A reference to another virtual model of this gateway, written as `/name`."""

    name: str


type Route = UpstreamRoute | VirtualRoute


def _parse_routes(value: object) -> object:
    targets = [value] if isinstance(value, str) else value
    if not isinstance(targets, list) or not targets:
        raise ValueError("must be a model or a non-empty list of models")
    routes: list[Route] = []
    for target in targets:
        # Real model names may contain slashes too, so only the first one separates the provider
        provider, slash, model = str(target).partition("/")
        if not slash or not model:
            raise ValueError(f"{target!r} must be 'provider/model' or '/virtual_model'")
        routes.append(UpstreamRoute(provider, model) if provider else VirtualRoute(model))
    return tuple(routes)


def _as_tuple(value: object) -> object:
    return (value,) if isinstance(value, str) else value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provider(_Strict):
    name: str
    url: str
    key: str = ""  # empty means no Authorization header


class VirtualModel(_Strict):
    name: str
    routes: Annotated[tuple[Route, ...], BeforeValidator(_parse_routes)] = Field(alias="model")
    description: str = ""


class Quota(_Strict):
    model: str
    max_concurrency: float = Field(ge=MIN_BUDGET)


class VirtualKey(_Strict):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    models: Annotated[tuple[str, ...], BeforeValidator(_as_tuple)]  # names or wildcard patterns
    quotas: tuple[Quota, ...] = ()

    def allows(self, model: str) -> bool:
        return any(fnmatch.fnmatchcase(model, pattern) for pattern in self.models)

    def max_concurrency(self, model: str) -> float | None:
        """The key's quota for a virtual model, None when unlimited."""
        return next((quota.max_concurrency for quota in self.quotas if quota.model == model), None)


class _ProvidersFile(_Strict):
    providers: tuple[Provider, ...]


class _VirtualModelsFile(_Strict):
    virtual_models: tuple[VirtualModel, ...]


class _VirtualKeysFile(_Strict):
    virtual_keys: tuple[VirtualKey, ...]


@dataclass(frozen=True)
class GatewayConfig:
    providers: Mapping[str, Provider]
    models: Mapping[str, VirtualModel]
    keys: Mapping[str, VirtualKey]
    secrets: Mapping[str, str]  # virtual key name -> secret

    def find_key(self, secret: str) -> VirtualKey | None:
        for name, expected in self.secrets.items():
            if hmac.compare_digest(secret.encode(), expected.encode()):
                return self.keys[name]
        return None

    def referenced_models(self, name: str) -> list[str]:
        """The virtual model followed by all virtual models it routes to, transitively."""
        return [name, *_references(self.models, name)]


def load_config(config_dir: Path, environ: Mapping[str, str] = os.environ) -> GatewayConfig:
    """Load `providers.toml`, `virtual_models.toml` and `virtual_keys.toml`, and check references."""
    providers = _index(_read(config_dir / "providers.toml", _ProvidersFile).providers, "provider")
    models = _index(_read(config_dir / "virtual_models.toml", _VirtualModelsFile).virtual_models, "virtual model")
    keys = _index(_read(config_dir / "virtual_keys.toml", _VirtualKeysFile).virtual_keys, "virtual key")
    _check_routes(models, providers)
    _check_keys(keys, models)
    return GatewayConfig(providers, models, keys, _read_secrets(keys, environ))


def _read[T: BaseModel](path: Path, schema: type[T]) -> T:
    try:
        with path.open("rb") as file:
            return schema.model_validate(tomllib.load(file))
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise ConfigError(f"{path}: {error}") from error


def _index[T: Provider | VirtualModel | VirtualKey](items: Sequence[T], kind: str) -> dict[str, T]:
    index: dict[str, T] = {}
    for item in items:
        if item.name in index:
            raise ConfigError(f"Duplicate {kind} {item.name!r}")
        index[item.name] = item
    return index


def _references(models: Mapping[str, VirtualModel], name: str) -> list[str]:
    """Names of virtual models reachable from `name` through references, breadth-first."""
    found: list[str] = []
    queue = [name]
    while queue:
        for route in models[queue.pop(0)].routes:
            if isinstance(route, VirtualRoute) and route.name not in found:
                found.append(route.name)
                queue.append(route.name)
    return found


def _check_routes(models: Mapping[str, VirtualModel], providers: Mapping[str, Provider]) -> None:
    for model in models.values():
        for route in model.routes:
            if isinstance(route, UpstreamRoute) and route.provider not in providers:
                raise ConfigError(f"Virtual model {model.name!r} routes to unknown provider {route.provider!r}")
            if isinstance(route, VirtualRoute) and route.name not in models:
                raise ConfigError(f"Virtual model {model.name!r} routes to unknown virtual model {route.name!r}")
    for name in models:
        if name in _references(models, name):
            raise ConfigError(f"Virtual model {name!r} references itself")


def _check_keys(keys: Mapping[str, VirtualKey], models: Mapping[str, VirtualModel]) -> None:
    for key in keys.values():
        for pattern in key.models:
            is_wildcard = any(char in pattern for char in "*?[")
            if not is_wildcard and pattern not in models:
                raise ConfigError(f"Virtual key {key.name!r} allows unknown virtual model {pattern!r}")
        quota_models = [quota.model for quota in key.quotas]
        for model in quota_models:
            if model not in models:
                raise ConfigError(f"Virtual key {key.name!r} has a quota for unknown virtual model {model!r}")
        if len(set(quota_models)) != len(quota_models):
            raise ConfigError(f"Virtual key {key.name!r} has several quotas for one virtual model")


def _read_secrets(keys: Mapping[str, VirtualKey], environ: Mapping[str, str]) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for name in keys:
        variable = KEY_ENV_PREFIX + name.upper()
        secret = environ.get(variable)
        if not secret:
            raise ConfigError(f"Virtual key {name!r} has no secret, set the {variable} environment variable")
        if secret in secrets.values():
            raise ConfigError(f"Virtual key {name!r} has the same secret as another key")
        secrets[name] = secret
    return secrets
