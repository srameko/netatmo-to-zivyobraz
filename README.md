# netatmo-to-zivyobraz

Fetches data from a [Netatmo](https://dev.netatmo.com) weather station and Healthy Home Coach, asks a local LLM for clothing and ventilation advice, and pushes everything to [Živý obraz](https://zivyobraz.eu) as custom values.

## Values pushed to Živý obraz

### Weather Station

| Key | Description |
|-----|-------------|
| `netatmo_indoor_temp` | Indoor temperature (°C) |
| `netatmo_indoor_humidity` | Indoor humidity (%) |
| `netatmo_indoor_co2` | CO₂ (ppm) |
| `netatmo_indoor_health` | Air quality label with ventilation advice (Czech) |
| `netatmo_pressure` | Pressure (mbar) |
| `netatmo_outdoor_temp` | Outdoor temperature (°C) |
| `netatmo_outdoor_humidity` | Outdoor humidity (%) |
| `netatmo_outdoor_feels_like` | Feels-like temperature (°C) |
| `netatmo_wind_speed` | Wind speed (km/h) |
| `netatmo_wind_gust` | Gust speed (km/h) |
| `netatmo_wind_angle` | Wind direction (°) |
| `netatmo_wind_max` | Max wind speed (km/h) |
| `netatmo_rain_current` | Current rainfall (mm/h) |
| `netatmo_rain_1h` | Rainfall last hour (mm) |
| `netatmo_rain_24h` | Rainfall last 24 h (mm) |

### Healthy Home Coach

Keys are derived from the device name configured in `HOMECOACH_MAP` (e.g. `Pracovna` → `netatmo_pracovna_*`).

| Key | Description |
|-----|-------------|
| `netatmo_{slug}_temp` | Temperature (°C) |
| `netatmo_{slug}_humidity` | Humidity (%) |
| `netatmo_{slug}_co2` | CO₂ (ppm) |
| `netatmo_{slug}_noise` | Noise (dB) |
| `netatmo_{slug}_health` | Air quality label with ventilation advice (Czech) |

### LLM advice

| Key | Description |
|-----|-------------|
| `llm_obleceni` | Clothing recommendation based on outdoor conditions |
| `llm_vetrani_{slug}` | Ventilation advice per Home Coach room |

### Feels-like calculation

Uses a composite formula matching Czech meteorological practice (ČHMÚ):
- **Wind chill** (WMO/Environment Canada) when temp ≤ 14 °C and wind > 4.8 km/h
- **Heat index** (Rothfusz) when temp ≥ 27 °C and humidity ≥ 40 %
- Otherwise raw temperature

Wind is taken from the Netatmo wind module if present, otherwise fetched from [Open-Meteo](https://open-meteo.com) using `LOCATION_LAT`/`LOCATION_LON`.

### Air quality labels

Both the indoor base module and each Home Coach device produce a Czech label that includes ventilation advice based on outdoor temperature:

| Score | Normal outdoor (−10 – 35 °C) | Extreme outdoor |
|-------|------------------------------|-----------------|
| 0 | `Zdravý` | `Zdravý` |
| 1 | `Dobrý` | `Dobrý` |
| 2 | `Přijatelný – větrejte` | `Přijatelný – větrejte krátce` |
| 3 | `Špatný – větrejte` | `Špatný – větrejte krátce` |
| 4 | `Nezdravý – větrejte ihned` | `Nezdravý – větrejte krátce` |

The indoor module score is derived from CO₂ and humidity. Home Coach score is provided directly by Netatmo.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NETATMO_CLIENT_ID` | ✓ | From dev.netatmo.com → your app |
| `NETATMO_CLIENT_SECRET` | ✓ | From dev.netatmo.com → your app |
| `NETATMO_REFRESH_TOKEN` | ✓ | Refresh token (auto-renewed on each run) |
| `ZO_IMPORT_KEY` | ✓ | From zivyobraz.eu → Values → Import key |
| `OLLAMA_URL` | | Ollama base URL (default: `http://localhost:11434`) |
| `OLLAMA_MODEL` | | Model name (default: `gemma4:e2b`) |
| `LOCATION_LAT` | | Latitude for Open-Meteo wind fallback |
| `LOCATION_LON` | | Longitude for Open-Meteo wind fallback |
| `HOMECOACH_MAP` | | MAC→name mapping, e.g. `AA:BB:CC:DD:EE:FF=Pracovna,…` |

Secrets (`NETATMO_*`, `ZO_IMPORT_KEY`) can be supplied as Docker Swarm secrets under `/run/secrets/<name>` or as plain environment variables.

## Deployment (Docker Swarm)

```yaml
  netatmo-to-zivyobraz:
    image: ghcr.io/srameko/netatmo-to-zivyobraz:latest
    volumes:
      - /volume1/docker/netatmo-to-zivyobraz:/data
    environment:
      - OLLAMA_URL=http://ollama:11434
      - OLLAMA_MODEL=gemma4:e2b
      - LOCATION_LAT=50.08
      - LOCATION_LON=14.44
      - HOMECOACH_MAP=AA:BB:CC:DD:EE:FF=Pracovna,11:22:33:44:55:66=Obyvak
    secrets:
      - NETATMO_CLIENT_ID
      - NETATMO_CLIENT_SECRET
      - NETATMO_REFRESH_TOKEN
      - ZO_IMPORT_KEY
    restart: unless-stopped
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.hostname == rpi
```

Add secrets in Portainer — never in git.

## CI/CD

- GitHub Actions: build → Trivy scan → push to `ghcr.io`
- Platform: `linux/arm64` (RPi 5)
- Dependabot: weekly updates of pinned action hashes
