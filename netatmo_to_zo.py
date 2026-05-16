#!/usr/bin/env python3
"""
Netatmo -> Zivy obraz
Fetches data from the Netatmo API, asks an LLM for clothing and ventilation
advice, and pushes everything to Zivy obraz as custom values.
Intended to run every 15 minutes (Docker CMD loop).
"""

import os
import json
import logging
import requests
import unicodedata

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _secret(name: str) -> str:
    """Read from Docker Swarm secret file if present, fall back to env var."""
    path = f"/run/secrets/{name}"
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    return os.environ[name]


NETATMO_CLIENT_ID     = _secret("NETATMO_CLIENT_ID")
NETATMO_CLIENT_SECRET = _secret("NETATMO_CLIENT_SECRET")
NETATMO_REFRESH_TOKEN = _secret("NETATMO_REFRESH_TOKEN")
ZO_IMPORT_KEY         = _secret("ZO_IMPORT_KEY")

OLLAMA_URL   = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e2b")
LOCATION_LAT = float(os.environ.get("LOCATION_LAT", "0"))
LOCATION_LON = float(os.environ.get("LOCATION_LON", "0"))

# MAC -> room name mapping, e.g. "70:ee:50:83:3e:ce=Pracovna,70:ee:50:83:2e:3e=Obyvak"
HOMECOACH_MAP = {
    k.strip(): v.strip()
    for k, v in (
        pair.split("=", 1)
        for pair in os.environ.get("HOMECOACH_MAP", "").split(",")
        if "=" in pair
    )
}

TOKEN_FILE = "/data/netatmo_tokens.json"


def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {"refresh_token": NETATMO_REFRESH_TOKEN}


def save_tokens(tokens: dict):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)


def refresh_access_token(refresh_token: str) -> dict:
    log.info("Refreshing access token...")
    r = requests.post(
        "https://api.netatmo.com/oauth2/token",
        data={
            "grant_type":    "refresh_token",
            "client_id":     NETATMO_CLIENT_ID,
            "client_secret": NETATMO_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_access_token() -> str:
    tokens = load_tokens()
    new = refresh_access_token(tokens["refresh_token"])
    save_tokens({"refresh_token": new["refresh_token"]})
    return new["access_token"]


def fetch_station_data(access_token: str) -> dict:
    log.info("Fetching station data...")
    r = requests.get(
        "https://api.netatmo.com/api/getstationsdata",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_homecoach_data(access_token: str) -> dict:
    log.info("Fetching Healthy Home Coach data...")
    r = requests.get(
        "https://api.netatmo.com/api/gethomecoachsdata",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def fetch_wind_speed() -> float:
    """Fetch current wind speed (km/h) from Open-Meteo. Returns 0 on failure."""
    if not LOCATION_LAT or not LOCATION_LON:
        log.warning("LOCATION_LAT/LON not set, skipping wind fetch")
        return 0.0
    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  LOCATION_LAT,
                "longitude": LOCATION_LON,
                "current":   "wind_speed_10m",
            },
            timeout=10,
        )
        r.raise_for_status()
        wind = r.json()["current"]["wind_speed_10m"]
        log.info("Wind speed: %s km/h", wind)
        return float(wind)
    except Exception as e:
        log.warning("Failed to fetch wind speed: %s", e)
        return 0.0


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().strip().replace(" ", "_").replace("-", "_")


def feels_like(temp_c: float, humidity: int, wind_kmh: float = 0.0) -> float:
    """
    Wind chill for temp <= 10C and wind > 4.8 km/h (Environment Canada),
    heat index (Steadman) for temp >= 27C,
    otherwise returns temp as-is.
    """
    if temp_c <= 10 and wind_kmh > 4.8:
        wc = (13.12
              + 0.6215 * temp_c
              - 11.37 * wind_kmh ** 0.16
              + 0.3965 * temp_c * wind_kmh ** 0.16)
        return round(wc, 1)
    elif temp_c >= 27:
        t = temp_c
        h = humidity
        hi = (-8.78469475556
              + 1.61139411 * t
              + 2.33854883889 * h
              - 0.14611605 * t * h
              - 0.012308094 * t ** 2
              - 0.0164248277778 * h ** 2
              + 0.002211732 * t ** 2 * h
              + 0.00072546 * t * h ** 2
              - 0.000003582 * t ** 2 * h ** 2)
        return round(hi, 1)
    return temp_c


def parse_measurements(data: dict) -> dict:
    values = {}
    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})

        if "Temperature" in dash:
            values["netatmo_indoor_temp"]     = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values["netatmo_indoor_humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values["netatmo_indoor_co2"]      = dash["CO2"]
        if "Pressure" in dash:
            values["netatmo_pressure"]        = round(dash["Pressure"], 1)

        for module in device.get("modules", []):
            mtype = module.get("type", "")
            mdash = module.get("dashboard_data", {})

            if mtype == "NAModule1":  # Outdoor
                if "Temperature" in mdash:
                    values["netatmo_outdoor_temp"]     = round(mdash["Temperature"], 1)
                if "Humidity" in mdash:
                    values["netatmo_outdoor_humidity"] = mdash["Humidity"]

            elif mtype == "NAModule2":  # Wind
                if "WindStrength" in mdash:
                    values["netatmo_wind_speed"] = mdash["WindStrength"]
                if "GustStrength" in mdash:
                    values["netatmo_wind_gust"]  = mdash["GustStrength"]
                if "WindAngle" in mdash:
                    values["netatmo_wind_angle"] = mdash["WindAngle"]
                if "max_wind_str" in mdash:
                    values["netatmo_wind_max"]   = mdash["max_wind_str"]

            elif mtype == "NAModule3":  # Rain
                if "Rain" in mdash:
                    values["netatmo_rain_current"] = round(mdash["Rain"], 1)
                if "sum_rain_1" in mdash:
                    values["netatmo_rain_1h"]      = round(mdash["sum_rain_1"], 1)
                if "sum_rain_24" in mdash:
                    values["netatmo_rain_24h"]     = round(mdash["sum_rain_24"], 1)

    return values


def parse_homecoach_measurements(data: dict) -> dict:
    """
    Returns measurements keyed by room name (from HOMECOACH_MAP) or MAC suffix.
    Also returns a list of per-device dicts for LLM ventilation queries.
    """
    values = {}
    coaches = []

    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})
        mac  = device.get("_id", "00:00:00:00:00:00")

        # Resolve room name from map, fall back to last 4 MAC chars
        room_raw = HOMECOACH_MAP.get(mac, mac.replace(":", "_")[-4:])
        slug     = _slugify(room_raw)
        log.info("Coach device: id=%s room=%s slug=%s", mac, room_raw, slug)

        entry = {"slug": slug, "room": room_raw}

        if "Temperature" in dash:
            values[f"netatmo_{slug}_temp"]     = round(dash["Temperature"], 1)
            entry["temp"] = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values[f"netatmo_{slug}_humidity"] = dash["Humidity"]
            entry["humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values[f"netatmo_{slug}_co2"]      = dash["CO2"]
            entry["co2"] = dash["CO2"]
        if "Noise" in dash:
            values[f"netatmo_{slug}_noise"]    = dash["Noise"]
        if "health_idx" in dash:
            # 0=Healthy, 1=Fine, 2=Fair, 3=Poor, 4=Unhealthy
            values[f"netatmo_{slug}_health"]   = dash["health_idx"]

        coaches.append(entry)

    return values, coaches


def ask_llm(prompt: str) -> str:
    log.info("Asking LLM (%s)...", OLLAMA_MODEL)
    r = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model":      OLLAMA_MODEL,
            "prompt":     prompt,
            "stream":     False,
            "think":      False,
            "keep_alive": -1,
            "options":    {"temperature": 0.3},
        },
        timeout=120,
    )
    r.raise_for_status()
    response = r.json()["response"].strip()
    log.info("LLM response: %s", response)
    return response


def ask_llm_clothing(values: dict, wind_kmh: float) -> str:
    temp     = values.get("netatmo_outdoor_temp", "?")
    humidity = values.get("netatmo_outdoor_humidity", "?")
    rain_1h  = values.get("netatmo_rain_1h", 0)
    rain_str = f"{rain_1h}mm rain" if rain_1h > 0 else "no rain"

    fl = (feels_like(temp, humidity, wind_kmh)
          if isinstance(temp, (int, float)) and isinstance(humidity, (int, float))
          else temp)

    prompt = (
        f"Outside: {temp}C (feels like {fl}C), humidity {humidity}%, "
        f"wind {wind_kmh}km/h, {rain_str}. "
        "What to wear? One sentence, max 8 words, reply in English."
    )
    return ask_llm(prompt)


def ask_llm_ventilation(coach: dict, outdoor_temp: float) -> str:
    """Ask LLM whether to ventilate for a specific room/coach."""
    co2      = coach.get("co2", "?")
    humidity = coach.get("humidity", "?")
    room     = coach.get("room", "room")

    prompt = (
        f"{room} - Indoor: CO2 {co2}ppm, humidity {humidity}%. "
        f"Outside: {outdoor_temp}C. "
        "Should I ventilate? One sentence, max 8 words, reply in English."
    )
    return ask_llm(prompt)


def push_to_zo(values: dict):
    if not values:
        log.warning("No values to push")
        return
    params = {"import_key": ZO_IMPORT_KEY, **values}
    log.info("Pushing %d values to Zivy obraz...", len(values))
    r = requests.get("https://in.zivyobraz.eu/", params=params, timeout=10)
    r.raise_for_status()
    log.info("OK: %s", r.text.strip())


def main():
    try:
        token = get_access_token()

        station_data = fetch_station_data(token)
        coach_data   = fetch_homecoach_data(token)

        values = {}
        values.update(parse_measurements(station_data))

        coach_values, coaches = parse_homecoach_measurements(coach_data)
        values.update(coach_values)

        log.info("Measurements: %s", values)

        # Use Netatmo wind if available, otherwise fall back to Open-Meteo
        wind_kmh = float(values.get("netatmo_wind_speed", 0) or fetch_wind_speed())

        outdoor_temp = values.get("netatmo_outdoor_temp", 20.0)

        values["llm_obleceni"] = ask_llm_clothing(values, wind_kmh)

        for coach in coaches:
            slug = coach["slug"]
            values[f"llm_vetrani_{slug}"] = ask_llm_ventilation(coach, outdoor_temp)

        push_to_zo(values)
    except requests.HTTPError as e:
        log.error("HTTP %s: %s", e.response.status_code, e.response.text)
        raise


if __name__ == "__main__":
    main()
