# LLM Gateway

Simple gateway for serving local models to multiple users.

Main difference from litellm and bifrost is quota mechanism. Instead of limiting RPM or TPM, this gateway only limits the number concurrent requests to the upstream LLM. It is useful when your tokens don't cost any actual money, but you also don't want one of the users to overload your local LLM serving.

It sits in front of llama.cpp and vLLM servers and speaks the OpenAI API:

- **Virtual models** rename real models
- **Virtual keys** set allowed virtual models and a concurrency quota for each user
- **Concurrency quotas** are fractional (`0.5` means half a worker) and live in Valkey, so replicas share them.

Streaming, multimodal inputs and embeddings are passed through unchanged. Details are in
[docs/wiki/architecture.md](docs/wiki/architecture.md).

## Configuration

The gateway reads three TOML files from `config/` and refuses to start if anything in them is wrong. Copy the
examples and edit them:

```sh
cp -r examples/config config
```

- `config/providers.toml` — the real LLM servers.
- `config/virtual_models.toml` — virtual model names and the targets they route to.
- `config/virtual_keys.toml` — virtual keys, the models they may use and their quotas.

All secrets are stored in `.env`. To generate random secrets for existing config, run:

```sh
uv run scripts/generate_keys.py    # modifies .env
```

The file formats, the validation rules and the other environment variables are described in
[docs/wiki/configuration.md](docs/wiki/configuration.md).

## Run with docker/podman compose

```sh
podman compose up -d    # or: docker compose up -d
```

This starts the gateway on port 4000 and a Valkey container for the quota state.

**`config/` must be mounted**, and the compose file mounts `./config` for you. The image deliberately contains no
config — without that directory the gateway finds no files and exits at startup. The same goes for `.env`: compose
fails to start if it is missing.

Check that it works:

```sh
curl http://localhost:4000/v1/models -H "Authorization: Bearer $LLM_KEY_MY_SERVICE_1"
```

Point any OpenAI-compatible client at `http://localhost:4000/v1` with a virtual key as the API key.

## Run without compose

```sh
uv run main.py --host 0.0.0.0 --port 4000
```

Needs a Valkey server of your own; set `VALKEY_URL` if it is not at `localhost:6379`.
