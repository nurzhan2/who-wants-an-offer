# Engineering rules

How code is written in this repository. Architecture is in
[ARCHITECTURE.md](ARCHITECTURE.md), sources in [SOURCES.md](SOURCES.md),
scoring in [MATCHING.md](MATCHING.md).

## Stack — not changed without an explicit decision

- Python 3.12, FastAPI, SQLAlchemy 2.0 **async**, Alembic, Pydantic v2
- PostgreSQL 17 + pgvector (Docker, host port 5436). SQLite is not supported
- `httpx.AsyncClient` for HTTP; `requests` is not used
- `structlog` for logs; no `print()` outside scripts
- Dependencies managed with `uv`, project config in `pyproject.toml`
- Frontend: React 18 + Vite + TypeScript strict + TanStack Query/Table +
  Tailwind + shadcn/ui. No Redux/MobX

## Layout

```
backend/app/
  api/v1/      FastAPI routers, HTTP layer only
  core/        config (pydantic-settings), logging, exceptions
  db/          models.py, session.py; migrations in backend/alembic/
  resume/      text extraction from PDF/DOCX + LLM profile extraction
  sources/     connectors: base.py plus one file per source
  normalize/   raw data to Vacancy, skill dictionary, deduplication
  matching/    embeddings, rule-based scoring
  workshop/    reference documents and checkable rules for CVs and letters
  pipeline/    run orchestration, scheduler
  services/    business logic between api/ and db/
frontend/src/
  pages/ components/ api/ hooks/ types/
```

## Hard rules

1. **Typed throughout.** Every function is annotated; `mypy --strict` passes on
   `app/`. `Any` only with a comment saying why.
2. **No business logic in routers.** A router validates, calls a service and
   answers. Everything else lives in `services/`.
3. **No raw dicts between layers.** The internal contract is Pydantic models
   from `app/schemas/`.
4. **Secrets only from the environment**, through `app.core.config.settings`.
   No key in code, a test or a default.
5. **Every connector is a plugin.** It subclasses `BaseSource`, registers
   itself and is never imported by the pipeline directly. Adding a source must
   not require changes outside `sources/`.
6. **External calls are wrapped:** retries with exponential backoff
   (`tenacity`), a timeout, a rate limiter, a response cache in development.
7. **Idempotent.** Re-running a connector creates no duplicates: upsert on
   `(source, external_id)`.
8. **Schema changes only through Alembic.** No `create_all()` outside tests.
9. **Tests are part of the change.** A module without tests is not done. HTTP
   is mocked with fixtures from `tests/fixtures/`; CI never calls external APIs.
10. **Code comments and docstrings in English.** Commits follow Conventional
    Commits, in English. The UI and user-facing text are in Russian.

## Definition of done

A change is finished when all of these hold:

- [ ] `ruff check . && ruff format --check .` is clean
- [ ] `mypy backend/app` is clean
- [ ] `pytest` is green, coverage of new code ≥ 70%
- [ ] `npm run typecheck && npm run lint` in `frontend/` is clean (if touched)
- [ ] A migration exists and applies to an empty database
- [ ] `.env.example` lists any new variable
- [ ] README and docs follow any change in a contract
- [ ] The commit is atomic, with a meaningful message

## What this project does not do

- No connectors to LinkedIn or Indeed: their terms forbid automated collection
- No solving or bypassing captchas, Cloudflare or other bot protection
- No disabling rate limits "to go faster"
- No hardcoded paths, cities or skills: they belong in configuration or the DB
- No new dependency without need; check what is already there first
- No rewriting unrelated modules along the way
- No silencing exceptions with `except Exception: pass`

## Two executors in one repository

`backend/` is anonymous and read-only: it reads public pages and APIs, without
an account or cookies, and sends nothing anywhere. `agent/` is a separate
package that runs in a browser under the user's own account and submits
applications, each one only after a person confirms it. They carry different
risks, so they never import each other, Playwright is not a backend dependency,
and the agent never runs on a server. The captcha rule applies to both: the
agent does not solve a captcha, it shows it to the person and waits. Details
are in [agent/README.md](../agent/README.md).

## LLM calls inside the project

- Every prompt lives in its own file under `app/llm/prompts/`, never inline
- Every model answer is validated by a Pydantic model; an invalid one gets one
  retry with the error message, then falls back to the rule-based result
- Tokens are counted and logged
- The model is set in configuration, not at the call site
