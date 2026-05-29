# Živý obraz — Weather Display Service

Single Docker container running on Raspberry Pi 5. Fetches environmental data every 30 minutes via cron and pushes it to the Živý obraz e-ink display API.

## Architecture

```
Netatmo API   → current indoor/outdoor sensor data → Živý obraz
OpenAQ API    → AQI from ČHMÚ station Brno-Svatoplukova → Živý obraz
```

**Weather forecast text (Alojz style)** is handled natively by the Živý obraz platform via the YrNoProvider integration (`lovecka.info`). This is NOT part of this container — configure it directly in the Živý obraz editor as a native widget.

## Data Sources

### Current Conditions — Netatmo API
- Auth: OAuth2 client credentials (`NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN`)
- Endpoint: `https://api.netatmo.com/api/getstationsdata`
- Fields from **indoor module** (NAMain): `Temperature`, `Humidity`, `CO2`, `Pressure`, `Noise`
- Fields from **outdoor module** (NAModule1): `Temperature`, `Humidity`
- Note: Netatmo outdoor AQI is NOT used — it comes from The Weather Company, not the sensor

### Air Quality — OpenAQ / ČHMÚ
- Station: **Brno-Svatoplukova** (ČHMÚ)
- OpenAQ API v3: `https://api.openaq.org/v3/`
- Find station by name or coordinates, fetch latest PM2.5 / PM10 / AQI
- No API key required for basic usage

## Environment Variables

```
NETATMO_CLIENT_ID
NETATMO_CLIENT_SECRET
NETATMO_REFRESH_TOKEN
ZIVYOBRAZ_API_KEY
ZIVYOBRAZ_DEVICE_ID
LOCATION_LAT
LOCATION_LON
LOCATION_ALT
```

Secrets are managed in Portainer — never committed to the repository.

## Container

- Single Python container, cron every 30 minutes
- Use `python:3.12-slim` base image
- Dependencies: `requests`, `python-crontab` or just `crond` in entrypoint
- Bind mount for logs if needed: `/volume1/docker/zivyobraz/logs`
- Connect to existing `home_network` overlay network in Swarm

## Živý obraz API

- Push each value as a custom sensor reading
- Use the "vlastní hodnota" (custom value) widget type in the Živý obraz editor
- One push per metric per cron run

## Error Handling

- If Netatmo token expired: refresh using refresh token, retry once
- If Netatmo is unavailable: skip push, do not push empty value
- If OpenAQ is unavailable: skip AQI push

## Notes

- Ollama (`gemma4:e2b`) runs as systemd service on RPi OS (not in Docker) — available at `http://rpi.home:11434`; used for other tasks, not this container
- The container runs in Docker Swarm; use `deploy:` section in compose, not `restart:`
