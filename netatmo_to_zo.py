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


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_name.lower().strip().replace(" ", "_").replace("-", "_")


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

            elif mtype == "NAModule3":  # Rain
                if "Rain" in mdash:
                    values["netatmo_rain_current"] = round(mdash["Rain"], 1)
                if "sum_rain_1" in mdash:
                    values["netatmo_rain_1h"]      = round(mdash["sum_rain_1"], 1)
                if "sum_rain_24" in mdash:
                    values["netatmo_rain_24h"]     = round(mdash["sum_rain_24"], 1)

    return values


def parse_homecoach_measurements(data: dict) -> dict:
    values = {}
    for device in data.get("body", {}).get("devices", []):
        dash = device.get("dashboard_data", {})
        slug = _slugify(device.get("name", "homecoach"))

        if "Temperature" in dash:
            values[f"netatmo_{slug}_temp"]     = round(dash["Temperature"], 1)
        if "Humidity" in dash:
            values[f"netatmo_{slug}_humidity"] = dash["Humidity"]
        if "CO2" in dash:
            values[f"netatmo_{slug}_co2"]      = dash["CO2"]
        if "Noise" in dash:
            values[f"netatmo_{slug}_noise"]    = dash["Noise"]
        if "health_idx" in dash:
            # 0=Healthy, 1=Fine, 2=Fair, 3=Poor, 4=Unhealthy
            values[f"netatmo_{slug}_health"]   = dash["health_idx"]

    return values


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


def ask_llm_clothing(values: dict) -> str:
    temp     = values.get("netatmo_outdoor_temp", "?")
    humidity = values.get("netatmo_outdoor_humidity", "?")
    rain_1h  = values.get("netatmo_rain_1h", 0)
    rain_str = f"{rain_1h}mm rain" if rain_1h > 0 else "no rain"

    prompt = (
        f"Outside: {temp}C, humidity {humidity}%, {rain_str}. "
        "What to wear? One sentence, max 8 words, reply in English."
    )
    return ask_llm(prompt)


def ask_llm_ventilation(values: dict) -> str:
    co2 = next(
        (v for k, v in values.items() if k.endswith("_co2") and "indoor" not in k),
        values.get("netatmo_indoor_co2", "?"),
    )
    humidity = next(
        (v for k, v in values.items() if k.endswith("_humidity") and "outdoor" not in k),
        values.get("netatmo_indoor_humidity", "?"),
    )
    outdoor_temp = values.get("netatmo_outdoor_temp", "?")

    prompt = (
        f"Indoor: CO2 {co2}ppm, humidity {humidity}%. Outside: {outdoor_temp}C. "
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
        values.update(parse_homecoach_measurements(coach_data))

        log.info("Measurements: %s", values)

        values["llm_obleceni"] = ask_llm_clothing(values)
        values["llm_vetrani"]  = ask_llm_ventilation(values)

        push_to_zo(values)
    except requests.HTTPError as e:
        log.error("HTTP %s: %s", e.response.status_code, e.response.text)
        raise


if __name__ == "__main__":
    main()
