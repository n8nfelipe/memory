FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY src ./src
RUN pip install --no-cache-dir '.[shared]' \
  && useradd --create-home --uid 10001 appuser \
  && chown -R appuser:appuser /app

EXPOSE 8765
USER appuser
HEALTHCHECK --interval=30s --timeout=3s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
CMD ["memory", "serve", "--host", "0.0.0.0"]
