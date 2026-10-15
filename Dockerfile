FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[shared]'

EXPOSE 8765
CMD ["memory", "serve", "--host", "0.0.0.0"]
