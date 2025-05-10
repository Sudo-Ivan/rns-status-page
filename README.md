# Reticulum Status Page

Reticulum status page using `rnstatus` and `rsnd` from Reticulum Network Stack utilities. Built using Flask, Gunicorn, and HTMX.

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

WIP

## API

Read the [API.md](API.md) file for more information on api usage.

## How it works

1. starts `rnsd` in a seperate thread.
2. uses `rnstatus` to get the status of the Reticulum network using provided config file. 

## Contributing

All contributions are welcome!

## License

MIT 