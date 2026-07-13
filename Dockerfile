FROM python:3.13-slim

ARG APP_UID=99
ARG APP_GID=100

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PYTHONPATH=/app/src \
    RAFFLE_DB_PATH=/app/data/gw2bot.db

WORKDIR /app

RUN mkdir -p /app/data \
    && chown "${APP_UID}:${APP_GID}" /app/data

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown="${APP_UID}:${APP_GID}" src ./src

USER ${APP_UID}:${APP_GID}

# Documents the default web calendar port; only served when WEB_ENABLED=true,
# and WEB_PORT overrides it at runtime (EXPOSE does not publish anything).
EXPOSE 8080

CMD ["python", "-m", "gw2bot"]
