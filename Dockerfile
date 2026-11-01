# Post-Radio's HTTP server (`postradio serve`). Everything it keeps (cache, data release, state, an optional
# config.yaml) lives under /data: mount one folder there. See docs/library.md, "Running the server".
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY postradio ./postradio
RUN pip install --no-cache-dir ".[server]" && useradd --create-home --uid 1000 postradio

ENV POSTRADIO_HOME=/data PYTHONUNBUFFERED=1
RUN mkdir /data && chown postradio /data
USER postradio
WORKDIR /data
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)"

CMD ["postradio", "serve", "--host", "0.0.0.0", "--port", "8765"]
