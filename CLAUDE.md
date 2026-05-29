# Živý obraz — Weather Display Service

Single Docker container running on Raspberry Pi 5. Fetches environmental data every 15 minutes via cron and pushes it to the Živý obraz e-ink display API.

## Architecture

```
Netatmo API   → indoor/outdoor sensor data → Živý obraz
OpenAQ API    → AQI from ČHMÚ station Brno-Svatoplukova → Živý obraz
```

**Weather forecast text (Alojz style)** is handled natively by the Živý obraz platform via the YrNoProvider integration (`lovecka.info`). This is NOT part of this container — configure it directly in the Živý obraz editor as a native widget.

**LLM is NOT used.** Do not implement any LLM/Ollama calls (no clothing advice, no ventilation advice, no summaries).

## Data Sources

### Current Conditions — Netatmo API
- Auth: OAuth2 client credentials (`NETATMO_CLIENT_ID`, `NETATMO_CLIENT_SECRET`, `NETATMO_REFRESH_TOKEN`)
- Refresh token is auto-renewed on each run and persisted
- Endpoints: `getstationsdata` (weather station), `gethomecoachsdata` (Home Coach devices)
- Fields from **indoor module** (NAMain): `Temperature`, `Humidity`, `CO2`, `Pressure`
- Fields from **outdoor module** (NAModule1): `Temperature`, `Humidity`
- Fields from **rain module** (NAModule3): `Rain`, `sum_rain_1`, `sum_rain_24`
- Fields from **wind module** (NAModule2): `WindStrength`, `WindAngle`, `GustStrength`, `max_wind_str`
- Fields from **Home Coach** devices: `Temperature`, `Humidity`, `CO2`, `Noise`, `health_idx`
- Note: Netatmo outdoor AQI is NOT used — it comes from The Weather Company model, not the sensor

### Air Quality — OpenAQ / ČHMÚ
- Station: **Brno-Svatoplukova** (ČHMÚ)
- OpenAQ API v3: `https://api.openaq.org/v3/`
- Fetch latest PM2.5, PM10, NO2, O3 values for this station
- Push as `openaq_pm25`, `openaq_pm10`, `openaq_no2`, `openaq_o3` and `openaq_aqi` (EAQI label) to Živý obraz
- If unavailable: skip push silently, do not push empty/null values

### Feels-like Temperature
- Wind chill (WMO formula) when temp ≤ 14 °C and wind > 4.8 km/h
- Heat index (Rothfusz) when temp ≥ 27 °C and humidity ≥ 40 %
- Otherwise raw temperature
- Wind source: Netatmo wind module if present, fallback to Open-Meteo (`LOCATION_LAT`, `LOCATION_LON`)

### Air Quality Labels (indoor)
Czech one-word label derived from CO2/humidity (indoor module) or `health_idx` (Home Coach):

| Score | Label        |
|-------|--------------|
| 0     | `Zdravý`     |
| 1     | `Dobrý`      |
| 2     | `Přijatelný` |
| 3     | `Špatný`     |
| 4     | `Nezdravý`   |

## Environment Variables

```
NETATMO_CLIENT_ID         # Netatmo developer app
NETATMO_CLIENT_SECRET     # Netatmo developer app
NETATMO_REFRESH_TOKEN     # OAuth2 refresh token (auto-renewed)
ZO_IMPORT_KEY             # Živý obraz import key
OPENAQ_API_KEY            # OpenAQ v3 API key (openaq.org)
LOCATION_LAT              # for Open-Meteo wind fallback
LOCATION_LON              # for Open-Meteo wind fallback
HOMECOACH_MAP             # MAC→slug mapping, e.g. AA:BB:CC:DD:EE:FF=pracovna,...
```

Secrets (`NETATMO_*`, `ZO_IMPORT_KEY`, `OPENAQ_API_KEY`) are managed in Portainer as Docker Swarm secrets under `/run/secrets/<name>`. Never committed to the repository.

## Container

- Single Python container, Alpine, cron every 15 minutes
- Multi-stage Alpine Dockerfile: builder installs deps, final image copies — no pip in production
- Built for `linux/arm64` (RPi 5)
- Image: `ghcr.io/srameko/netatmo-to-zivyobraz`
- Connect to existing `home_network` overlay network in Swarm
- Use `deploy:` section, not `restart:`
- Bind mounts require manual `mkdir -p` before Swarm deploy

## Živý obraz API

- Push via GET: `https://in.zivyobraz.eu/?import_key=KEY&param=value&param2=value2`
- All values in a single GET request per run
- Do not push null or empty values — skip missing metrics silently

## Error Handling

- Netatmo token expired: refresh using refresh token, retry once, persist new token
- Netatmo unavailable: skip entire run, log error
- OpenAQ unavailable: skip AQI values, continue with rest
- Never push empty/null values to Živý obraz

## Notes

- Bind mounts require manual `mkdir -p` before Docker Swarm deploy — Swarm does NOT create them
- GitHub Actions are pinned to commit hashes (not mutable tags) — Dependabot handles weekly updates
- Trivy scanning is standard in CI
