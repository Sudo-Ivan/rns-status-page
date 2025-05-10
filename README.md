# Reticulum Status Page

[Reticulum](https://reticulum.network/) status page using `rnstatus` and `rsnd` from the utilities. Built using Flask, Gunicorn, and HTMX.

Request to Add or Remove an Interface: Open a [Issue](https://github.com/Sudo-Ivan/rns-status-page/issues/new) or message me on Reticulum `c0cdcb64499e4f0d544ff87c9d5e2485` this only applies to my instance at rnstatus.quad4.io

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

```bash
docker run -d --name rns-status-page -p 5000:5000 ghcr.io/sudo-ivan/rns-status-page:latest
```

```bash
docker run -d --name rns-status-page -p 5000:5000 -v ./uptime.json:/app/uptime.json ghcr.io/sudo-ivan/rns-status-page:latest
```

replace `docker` with `podman` if you are using podman.

## To-Do

- [ ] More tracking and stats.
- [ ] Filter by reliability, uptime.
- [ ] Micron Status Page.
- [ ] Optional I2P, yggdrasil support.

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