# Reticulum Status Page

[![Socket Badge](https://socket.dev/api/badge/pypi/package/rns-status-page/1.1.2?artifact_id=tar-gz)](https://socket.dev/pypi/package/rns-status-page/overview/)
[![Status Page](https://status.quad4.io/badge/rstatus/status?labelColor=&color=&style=flat&label=rstatus)](https://status.quad4.io)

[Reticulum](https://reticulum.network/) status page using direct Reticulum library integration and `rnsd` from the utilities. Built using Flask, Gunicorn, and HTMX.

Request to Add or Remove an Interface: Open a [Issue](https://github.com/Sudo-Ivan/rns-status-page/issues/new) or message me on Reticulum `c0cdcb64499e4f0d544ff87c9d5e2485` this only applies to my instance at [rstatus.quad4.io](https://rstatus.quad4.io)

> [!NOTE]  
> [rstatus.quad4.io](https://rstatus.quad4.io/) is protected by [BunkerWeb](https://www.bunkerweb.io/), requests are logged in order to prevent abuse, bots and LLM scrapers.

## Features

- Check status of Reticulum interfaces.
- Download specific or all interfaces (txt)
- API for usage in other projects/applications

### Security

- API rate limiting with [Flask-Limiter](https://flask-limiter.readthedocs.io/en/latest/)
- [CORS](https://flask-cors.readthedocs.io/en/latest/) for locking down cross origin requests
- [Talisman](https://github.com/GoogleCloudPlatform/flask-talisman) for security headers
- Docker uses [Chainguard](https://github.com/chainguard-dev) images for rootless and distroless containers

## Install

```bash
pip install rns-status-page
```

## Usage

```bash
rns-status-page
```

It uses `uptime.json` to track uptime of interfaces and persist across rns-status-page restarts.

### No rnsd

If you already have rnsd or nomadnet running you can use the `--no-rnsd` flag to not start rnsd on a separate thread for the status page.

```bash
rns-status-page --no-rnsd
```

## Docker/Podman

> [!NOTE]  
> Please wait at least 5 minutes for RNS to initialize and stats to be available.

```bash
docker run -d --name rns-status-page -p 5000:5000 ghcr.io/sudo-ivan/rns-status-page:latest
```

```bash
touch ./uptime.json
chown 65532:65532 ./uptime.json
docker run -d --name rns-status-page -p 5000:5000 -v ./uptime.json:/home/nonroot/uptime.json ghcr.io/sudo-ivan/rns-status-page:latest
```

If you have existing config, `chown 65532:65532 uptime.json`

Replace `docker` with `podman` if you are using podman.

### Docker Compose

```bash
# Create uptime.json with correct permissions
touch ./uptime.json
chown 65532:65532 ./uptime.json

# Start the service
docker compose up -d
```

The compose configuration includes:
- Resource limits (CPU/Memory)
- Security capabilities (NET_ADMIN, NET_RAW)
- Health checks
- Automatic restart policy

### Debugging

Verify rnstatus works:

```bash
docker exec rns-status-page rnstatus
```

Should display interface stats.

## To-Do

- [ ] More tracking over time and stats
- [ ] Integrate more with the Reticulum network (Data sharing for more reliable stats, usage by LXMF bots, API over Reticulum)
- [ ] Stale server detection (node is up but no announces being received/sent)
- [ ] Filter by reliability and uptime
- [ ] Micron Status Page
- [ ] Optional I2P, yggdrasil support (in docker)
- [ ] Convert announces received/sent into a more readable format
- [ ] Add API security tests
- [ ] Memory and performance optimization
- [ ] History endpoint for changes over time
- [ ] Sqlite database instead of json for uptime and history
- [ ] Dedicated settings file to configure various things

## API

Read the [API.md](API.md) file for more information on api usage.

## How it works

1. Starts `rnsd` in a separate thread, unless `--no-rnsd` flag is used which will use existing shared connection or rnsd instance
2. Uses Reticulum directly to get the status of interfaces/servers. (originally used `rnstatus` command output)
3. Flask and Gunicorn are used to serve the status page and API

## Contributing

All contributions are welcome!

## License

MIT 