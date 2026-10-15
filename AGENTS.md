# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.13+ package using a `src` layout. Application code lives in `src/llm_memory/`: `api.py`, `cli.py`, and `mcp_server.py` expose the service, while `service.py`, `models.py`, `schemas.py`, `db.py`, and `config.py` implement its core. Database migrations are in `src/llm_memory/migrations/versions/` (with the Alembic environment under `migrations/`). Tests are in `tests/`; product specifications are in `specs/`. Docker and shared-deployment examples are in `Dockerfile`, `compose.yaml`, and `.env.shared.example`.

## Build, Test, and Development Commands

- `uv sync --extra dev --extra shared` installs the project and development dependencies.
- `uv run pytest` runs the test suite; add `--cov=llm_memory --cov-report=term-missing` for coverage details.
- `uv run ruff check .` checks lint and style.
- `uv run mypy src/llm_memory` runs strict static type checking.
- `uv build` creates wheel and source distributions.
- `uv run memory serve` starts the local API; `uv run memory init` initializes its database.

## Coding Style & Naming Conventions

Use four spaces for indentation, Python type annotations, and lines no longer than 100 characters. Follow Ruff defaults and the strict mypy settings in `pyproject.toml`. Use `snake_case` for modules, functions, and variables; use `PascalCase` for classes. Keep API schemas and persistence models explicit, and add schema changes through Alembic migrations rather than editing an initialized database manually.

## Testing Guidelines

Tests use pytest and are named `tests/test_*.py`; test functions should use the `test_*` prefix. Add or update focused tests for behavior changes, including CLI or API coverage where relevant. Run `uv run pytest` before submitting; CI also runs Ruff, mypy, dependency auditing, and package builds on Python 3.13 and 3.14. Coverage is reported but has no configured minimum.

## Commit & Pull Request Guidelines

This repository has no commit history yet, so no established commit convention can be inferred. Use concise, imperative commit subjects, optionally with a scope (for example, `fix(api): validate memory filters`). Pull requests should explain the behavior change, note relevant configuration or migration impact, link related issues, and include the checks run. Add request/response examples for API changes; include screenshots only when a visual change makes them useful.

## Security & Configuration

Use `.env.example` and `.env.shared.example` as templates; never commit real tokens, credentials, or populated `.env` files. Keep local API use bound to loopback. For shared access, configure authentication and TLS as described in `README.md`.
