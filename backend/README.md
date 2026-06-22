# Kinora backend

FastAPI service that powers Kinora's generation-on-scroll showrunner: typed
settings, structured logging, Prometheus metrics, async SQLAlchemy + Alembic
(schema lands in the data-layer phase), and the agent/scheduler/render
subsystems added in later phases.

## Requirements

- Python **3.11+**
- (For the full stack) Docker + Docker Compose — see [`../infra`](../infra)

## Quickstart

```bash
# from the repo root
make install            # creates backend/.venv and installs the package + dev tools

# provide secrets
cp ../.env.example .env  # then edit DASHSCOPE_API_KEY
# (backend/.env is gitignored)

# run the API
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Then:

- `GET http://localhost:8000/health`  → `{"status": "ok"}`
- `GET http://localhost:8000/ready`   → `{"status": "ready"}`
- `GET http://localhost:8000/metrics` → Prometheus exposition
- `GET http://localhost:8000/docs`    → OpenAPI UI

## Layout

```
app/
  core/            # config (pydantic-settings) + structlog logging
  observability/   # Prometheus registry + metrics helpers
  main.py          # create_app() factory and ASGI `app`
migrations/        # Alembic environment (versions land in later phases)
tests/             # pytest suite
```

## Configuration

All settings live in [`app/core/config.py`](app/core/config.py) and load from the
environment / `backend/.env`. The only required value is `DASHSCOPE_API_KEY`;
every other field has a localhost-friendly default. See
[`../.env.example`](../.env.example) for the full list.

## Developer tasks (from the repo root)

| Command         | Action                                   |
| --------------- | ---------------------------------------- |
| `make lint`     | `ruff check` + `mypy`                    |
| `make fmt`      | `black` + `ruff --fix`                   |
| `make test`     | `pytest`                                 |
| `make migrate`  | `alembic upgrade head`                   |
| `make revision` | `alembic revision --autogenerate`        |

## Database migrations

```bash
# create the schema once Postgres is up (see ../infra)
.venv/bin/alembic -c alembic.ini upgrade head

# generate a new revision after editing models (data-layer phase onward)
.venv/bin/alembic -c alembic.ini revision --autogenerate -m "describe change"
```

`alembic.ini` leaves `sqlalchemy.url` empty on purpose — the URL is read from
`Settings.database_url` in `migrations/env.py`, so the same config works locally,
in Docker, and in the cloud.
