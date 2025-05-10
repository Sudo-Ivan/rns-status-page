FROM python:3.13-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rns_status_page ./rns_status_page/
COPY setup.py .
COPY config /root/.reticulum/config

VOLUME /root/.reticulum

EXPOSE 5000

LABEL org.opencontainers.image.source="https://github.com/Sudo-Ivan/rns-status-page"
LABEL org.opencontainers.image.description="Reticulum network status page."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.authors="Sudo-Ivan"

ENTRYPOINT ["python", "rns_status_page/status_page.py"]
