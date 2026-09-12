# Make this deployable using compose

Add `docker-compose.yml` and Dockerfile. Add valkey to the compose file. Make this compatible with both podman compose and docker compose. Test using podman.

## About config

Config files (3 of them) must be mounted to the service image.

## Readme

Write a readme explaining what this service is, how to run and configure it. Be brief. For long explanations, link to existing wiki.

Main launch path is through docker/podman compose. Explain that config must be mounted, or it will not work.
