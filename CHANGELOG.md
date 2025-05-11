# Changelog

## 1.3.0

- Use Reticulum directly to get the status of interfaces/servers.
- Better error handling and details
- Removed redundant parameters
- Added `RNS Testnet Dublin` to config.

## 1.2.0

- Added `--no-rnsd` flag to not start `rnsd` in a separate thread.
- Added `MANAGED_RNSD` environment variable to control if `rnsd` is managed by the script.
- Added `docker-compose.yml` file to the project.
- Replaced debug endpoint with health check endpoint.
- Added bandit and ruff to dev dependencies.
- Moved to chainguard images for docker. (rootless and distroless)