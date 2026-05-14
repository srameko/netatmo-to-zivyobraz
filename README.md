# netatmo-to-zo

Fetches data from a [Netatmo](https://dev.netatmo.com) weather station and pushes it to [Živý obraz](https://zivyobraz.eu) as custom values.

## Values pushed to Živý obraz

| Key | Description |
|-----|-------------|
| `netatmo_outdoor_temp` | Outdoor temperature (°C) |
| `netatmo_outdoor_humidity` | Outdoor humidity (%) |
| `netatmo_indoor_temp` | Indoor temperature (°C) |
| `netatmo_indoor_humidity` | Indoor humidity (%) |
| `netatmo_indoor_co2` | CO₂ (ppm) |
| `netatmo_pressure` | Pressure (mbar) |
| `netatmo_rain_current` | Current rainfall (mm/h) |
| `netatmo_rain_1h` | Rainfall last hour (mm) |
| `netatmo_rain_24h` | Rainfall last 24 h (mm) |

## Deployment (Docker Swarm)

Add to `stacks/home/docker-compose.yml`:

```yaml
  netatmo-to-zo:
    image: ghcr.io/srameko/netatmo-to-zo:latest
    volumes:
      - /volume1/docker/netatmo-to-zo:/data
    environment:
      - NETATMO_CLIENT_ID=${NETATMO_CLIENT_ID}
      - NETATMO_CLIENT_SECRET=${NETATMO_CLIENT_SECRET}
      - NETATMO_REFRESH_TOKEN=${NETATMO_REFRESH_TOKEN}
      - ZO_IMPORT_KEY=${ZO_IMPORT_KEY}
    restart: unless-stopped
    deploy:
      replicas: 1
      placement:
        constraints:
          - node.hostname == rpi
```

Add secrets in Portainer — never in git.

## Environment variables

| Variable | Description |
|----------|-------------|
| `NETATMO_CLIENT_ID` | From dev.netatmo.com → your app |
| `NETATMO_CLIENT_SECRET` | From dev.netatmo.com → your app |
| `NETATMO_REFRESH_TOKEN` | Refresh token (auto-renewed on each run) |
| `ZO_IMPORT_KEY` | From zivyobraz.eu → Values → Import key |

## CI/CD

- GitHub Actions: build → Trivy scan → push to `ghcr.io`
- Platform: `linux/arm64` (RPi 5)
- Dependabot: weekly updates of pinned action hashes
