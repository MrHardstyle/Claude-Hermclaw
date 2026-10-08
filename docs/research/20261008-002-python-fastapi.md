# 20261008-002 – Python / FastAPI / Pydantic / SQLAlchemy / SSE

## Question
Welche Versionen und welche API für HTTP-API, Validierung, ORM und Server-Sent Events?

## Why needed
P02, P04 (SSE), P30 (API).

## Sources
- FastAPI SSE-Referenz: https://fastapi.tiangolo.com/reference/sse/
- FastAPI SSE-Tutorial: https://fastapi.tiangolo.com/tutorial/server-sent-events/
- Installierte Pakete in der Build-Umgebung (PyPI, 2026-10-08): fastapi 0.143.0, starlette 1.7.0, pydantic 2.13.5, SQLAlchemy 2.1.4, alembic 1.20.0, psycopg 3.3.6, httpx 0.28.1, uvicorn 0.54.0, pytest 9.1.1, hypothesis 6.168.5, ruff 0.16.10, mypy 2.4.0

## Source date/version
siehe oben, Abruf 2026-10-08.

## Relevant facts
- FastAPI hat native SSE-Unterstützung: `from fastapi.sse import EventSourceResponse, ServerSentEvent`; Generator-Pfadfunktion mit `response_class=EventSourceResponse`; `ServerSentEvent` setzt `id`, `event`, `retry`, `comment`.
- Pydantic v2 erzeugt JSON-Schema über `model_json_schema()`.
- SQLAlchemy 2.x: `AsyncSession`, `async_sessionmaker`, Treiber `postgresql+psycopg://` (psycopg 3 async).

## Compatibility with our hardware
unkritisch.

## Compatibility with our versions
Python 3.12 (Build) und 3.13 (trixie) werden von allen genannten Paketen unterstützt.

## Rejected alternatives
`sse-starlette` als Zusatzpaket – nicht mehr nötig, da FastAPI SSE nativ liefert.

## Decision fixed by architecture
FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, asyncio, httpx, psycopg.

## Implementation consequences
- SSE-Endpoint `GET /api/jobs/{id}/events/stream` liefert `ServerSentEvent(id=<sequence>)`, wertet `Last-Event-ID` aus und sendet Heartbeat-Kommentare.
- Versionsuntergrenzen in `pyproject.toml`: fastapi>=0.135 (SSE), sqlalchemy>=2.0, pydantic>=2.7.

## Open risks
FastAPI-SSE ist relativ neu; Verhalten bei Client-Abbruch wird durch Tests abgedeckt.
