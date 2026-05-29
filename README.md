# netatmo-to-zivyobraz

Fetches data from a [Netatmo](https://dev.netatmo.com) weather station and Healthy Home Coach, pushes everything to [Živý obraz](https://zivyobraz.eu) as custom values every 15 minutes.

Weather forecast text (Alojz style) is handled natively by the Živý obraz platform — not by this container.

## Values pushed to Živý obraz

### Weather Station

| Key                          | Description                          |
|------------------------------|--------------------------------------|
| `netatmo_indoor_temp`        | Indoor temperature (°C)              |
| `netatmo_indoor_humidity`    | Indoor humidity (%)                  |
| `netatmo_indoor_co2`         | CO₂ (ppm)                            |
| `netatmo_indoor_health`      | Air quality label (Czech)            |
| `netatmo_pressure`           | Pressure (mbar)                      |
| `netatmo_outdoor_temp`       | Outdoor temperature (°C)             |
| `netatmo_outdoor_humidity`   | Outdoor humidity (%)                 |
| `netatmo_outdoor_feels_like` | Feels-like temperature (°C)          |
| `netatmo_wind_speed`         | Wind speed (km/h)                    |
| `netatmo_wind_gust`          | Gust speed (km/h)                    |
| `netatmo_wind_angle`         | Wind direction (°)                   |
| `netatmo_wind_max`           | Max wind speed (km/h)                |
| `netatmo_rain_current`       | Current rainfall (mm/h)              |
| `netatmo_rain_1h`            | Rainfall last hour (mm)              |
| `netatmo_rain_24h`           | Rainfall last 24 h (mm)              |

### Healthy Home Coach

Keys are derived from the device name configured in `HOMECOACH_MAP` (e.g. `Pracovna` → `netatmo_pracovna_*`).

| Key                       | Description                   |
|---------------------------|-------------------------------|
| `netatmo_{slug}_temp`     | Temperature (°C)              |
| `netatmo_{slug}_humidity` | Humidity (%)                  |
| `netatmo_{slug}_co2`      | CO₂ (ppm)                     |
| `netatmo_{slug}_noise`    | Noise (dB)                    |
| `netatmo_{slug}_health`   | Air quality label (Czech)     |
| `llm_vetrani_{slug}`      | Ventilation advice (Czech, LLM) |

### Air Quality — ČHMÚ via OpenAQ

Real measurements from the nearest ČHMÚ station (Brno-Svatoplukova).
Netatmo's built-in AQI is NOT used — it is a model from The Weather Company, not a real sensor.

| Key           | Description          |
|---------------|----------------------|
| `openaq_pm25` | PM2.5 (µg/m³)        |
| `openaq_pm10` | PM10 (µg/m³)         |

### Feels-like calculation

Uses a composite formula matching Czech meteorological practice (ČHMÚ):

- **Wind chill** (WMO/Environment Canada) when temp ≤ 14 °C and wind > 4.8 km/h
- **Heat index** (Rothfusz) when temp ≥ 27 °C and humidity ≥ 40 %
- Otherwise raw temperature

Wind is taken from the Netatmo wind module if present, otherwise fetched from [Open-Meteo](https://open-meteo.com).

### Air quality labels

| Score | Normal outdoor (−10–35 °C)   | Extreme outdoor                |
|-------|------------------------------|--------------------------------|
| 0     | `Zdravý`                     | `Zdravý`                       |
| 1     | `Dobrý`                      | `Dobrý`                        |
| 2     | `Přijatelný – větrejte`      | `Přijatelný – větrejte krátce` |
| 3     | `Špatný – větrejte`          | `Špatný – větrejte krátce`     |
| 4     | `Nezdravý – větrejte ihned`  | `Nezdravý – větrejte krátce`   |

## Environment variables

| Variable                | Required | Description                                            |
|-------------------------|----------|--------------------------------------------------------|
| `NETATMO_CLIENT_ID`     | ✓        | From dev.netatmo.com → your app                        |
| `NETATMO_CLIENT_SECRET` | ✓        | From dev.netatmo.com → your app                        |
| `NETATMO_REFRESH_TOKEN` | ✓        | Refresh token (auto-renewed on each run)               |
| `ZO_IMPORT_KEY`         | ✓        | From zivyobraz.eu → Values → Import key                |
| `OPENAQ_API_KEY`        | ✓        | From openaq.org → API keys                             |
| `OLLAMA_URL`            |          | Ollama base URL (default: `http://rpi.home:11434`)     |
| `OLLAMA_MODEL`          |          | Model name (default: `gemma4:e2b`)                     |
| `LOCATION_LAT`          |          | Latitude for Open-Meteo wind fallback                  |
| `LOCATION_LON`          |          | Longitude for Open-Meteo wind fallback                 |
| `HOMECOACH_MAP`         |          | MAC→slug mapping: `AA:BB:CC:DD:EE:FF=pracovna,...`    |

Secrets (`NETATMO_*`, `ZO_IMPORT_KEY`, `OPENAQ_API_KEY`) can be supplied as Docker Swarm secrets under `/run/secrets/<name>` or as plain environment variables.

## Deployment (Docker Swarm)

```yaml
netatmo-to-zivyobraz:
  image: ghcr.io/srameko/netatmo-to-zivyobraz:latest
  volumes:
    - /volume1/docker/netatmo-to-zivyobraz:/data
  environment:
    - OLLAMA_URL=http://rpi.home:11434
    - OLLAMA_MODEL=gemma4:e2b
    - LOCATION_LAT=49.1954
    - LOCATION_LON=16.6287
    - HOMECOACH_MAP=AA:BB:CC:DD:EE:FF=pracovna,11:22:33:44:55:66=obyvak
  secrets:
    - NETATMO_CLIENT_ID
    - NETATMO_CLIENT_SECRET
    - NETATMO_REFRESH_TOKEN
    - ZO_IMPORT_KEY
    - OPENAQ_API_KEY
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