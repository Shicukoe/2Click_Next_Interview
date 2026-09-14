FROM docker.io/library/python:3.13.15-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY data ./data
COPY app ./app

# Self-checks for the value parsers and the handoff assistant: a broken rule fails the build.
RUN python -m app.parse && python -m app.assistant

RUN useradd --system --create-home crm
USER crm

EXPOSE 3000
# Import runs once, in a single process, before any web worker starts.
CMD ["sh", "-c", "python -m app.importer && exec gunicorn --bind 0.0.0.0:3000 --access-logfile - app.main:app"]
