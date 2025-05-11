# Reticulum Status Page

[![Socket Badge](https://socket.dev/api/badge/pypi/package/rns-status-page/1.1.2?artifact_id=tar-gz)](https://socket.dev/pypi/package/rns-status-page/overview/)


[Reticulum](https://reticulum.network/) status page using `rnstatus` and `rnsd` from the utilities. Built using Flask, Gunicorn, and HTMX.

Request to Add or Remove an Interface: Open a [Issue](https://github.com/Sudo-Ivan/rns-status-page/issues/new) or message me on Reticulum `c0cdcb64499e4f0d544ff87c9d5e2485` this only applies to my instance at [rstatus.quad4.io](https://rstatus.quad4.io)

## Install

```bash
pip install rns-status-page
```

## Usage

```bash
rns-status-page
```

It uses `uptime.json` to track uptime of interfaces and persist across rns-status-page restarts.

## Docker/Podman

> [!NOTE]  
> Please wait atleast 5 minutes for rnstatus to work.

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
docker exec rns-status-page rnstatus # or docker exec <your-container-name> rnstatus
```

## To-Do

- [ ] More tracking and stats.
- [ ] Stale server detection (node is up but no announces being recieved/sent).
- [ ] Configuration for the status page and API.
- [ ] Filter by reliability, uptime.
- [ ] Micron Status Page.
- [ ] Optional I2P, yggdrasil support (in docker).
- [ ] Convert announces recieved/sent into a more readable format.
- [ ] Add API security tests.
- [ ] Add docker-compose.yml.

## API

Read the [API.md](API.md) file for more information on api usage.

## How it works

1. starts `rnsd` in a seperate thread.
2. uses `rnstatus` to get the status of the Reticulum network using provided config file. 
3. Flask and Gunicorn are used to serve the status page and API.

## Contributing

All contributions are welcome!

## License

MIT 