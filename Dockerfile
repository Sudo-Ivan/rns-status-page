# Note: If chainguard images stop working, use Alpine images.

FROM cgr.dev/chainguard/python:latest-dev AS build

USER root

RUN apk add --no-cache \
    --repository=https://packages.wolfi.dev/os \
    build-base \
    libffi-dev \
    rust \
    pkgconf \
    linux-headers

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"

WORKDIR /app

RUN pip3 install setuptools-rust pyopenssl cryptography rns>=0.9.6

COPY requirements.txt .
RUN pip3 install -r requirements.txt

COPY rns_status_page ./rns_status_page/
COPY setup.py .
COPY README.md .
COPY LICENSE .

RUN pip3 install -e .

RUN mkdir -p /home/nonroot/.reticulum
COPY config /home/nonroot/.reticulum/config
RUN chown -R 65532:65532 /home/nonroot

FROM cgr.dev/chainguard/python:latest

LABEL org.opencontainers.image.source="https://github.com/Sudo-Ivan/rns-status-page"
LABEL org.opencontainers.image.description="Reticulum network status page."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="Sudo-Ivan"

COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app
COPY --from=build /home/nonroot /home/nonroot

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED="yes"
ENV PYTHONPATH="/app"

USER nonroot
WORKDIR /home/nonroot

VOLUME /home/nonroot/.reticulum

ENTRYPOINT ["python", "/app/rns_status_page/status_page.py"]
