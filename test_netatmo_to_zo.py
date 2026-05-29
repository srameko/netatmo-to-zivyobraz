#!/usr/bin/env python3
"""
Tests for netatmo_to_zo.py.
Run with: pytest test_netatmo_to_zo.py
"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

# Stub required secrets before the module is imported
os.environ.setdefault("NETATMO_CLIENT_ID", "test_id")
os.environ.setdefault("NETATMO_CLIENT_SECRET", "test_secret")
os.environ.setdefault("NETATMO_REFRESH_TOKEN", "test_refresh")
os.environ.setdefault("ZO_IMPORT_KEY", "test_zo_key")

import netatmo_to_zo as ntz  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _mock_response(json_data, status=200):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.raise_for_status.return_value = None
    m.text = "OK"
    return m


# ── _eaqi ─────────────────────────────────────────────────────────────────────

class TestEaqi:
    @pytest.mark.parametrize("pm25,expected", [
        (0.0, 1), (10.0, 1),
        (10.1, 2), (20.0, 2),
        (20.1, 3), (25.0, 3),
        (25.1, 4), (50.0, 4),
        (50.1, 5),
    ])
    def test_pm25_only(self, pm25, expected):
        assert ntz._eaqi(pm25, None) == expected

    @pytest.mark.parametrize("pm10,expected", [
        (0.0, 1), (20.0, 1),
        (20.1, 2), (40.0, 2),
        (40.1, 3), (50.0, 3),
        (50.1, 4), (100.0, 4),
        (100.1, 5),
    ])
    def test_pm10_only(self, pm10, expected):
        assert ntz._eaqi(None, pm10) == expected

    def test_worst_of_both_wins(self):
        assert ntz._eaqi(5.0, 150.0) == 5   # pm25=1, pm10=5
        assert ntz._eaqi(60.0, 10.0) == 5   # pm25=5, pm10=1

    def test_both_none_returns_zero(self):
        assert ntz._eaqi(None, None) == 0


# ── _eaqi_label ───────────────────────────────────────────────────────────────

class TestEaqiLabel:
    @pytest.mark.parametrize("score,label", [
        (1, "Velmi dobrá"),
        (2, "Dobrá"),
        (3, "Přijatelná"),
        (4, "Špatná"),
        (5, "Nezdravá"),
    ])
    def test_known_scores(self, score, label):
        assert ntz._eaqi_label(score) == label

    def test_unknown_score_returns_stringified(self):
        assert ntz._eaqi_label(99) == "99"


# ── feels_like ────────────────────────────────────────────────────────────────

class TestFeelsLike:
    def test_wind_chill_lowers_apparent_temp(self):
        assert ntz.feels_like(-5.0, 50, 30.0) < -5.0

    def test_wind_chill_ignored_above_14c(self):
        assert ntz.feels_like(15.0, 50, 30.0) == 15.0

    def test_wind_chill_ignored_below_4_8_kmh(self):
        assert ntz.feels_like(0.0, 50, 4.0) == 0.0

    def test_wind_chill_not_applied_when_formula_is_warmer(self):
        # At 14°C and low wind, formula can produce wc > temp; raw temp is returned
        result = ntz.feels_like(14.0, 50, 5.0)
        assert result == 14.0

    def test_heat_index_raises_apparent_temp(self):
        assert ntz.feels_like(35.0, 80, 0.0) > 35.0

    def test_heat_index_ignored_below_27c(self):
        assert ntz.feels_like(26.0, 80, 0.0) == 26.0

    def test_heat_index_ignored_below_40_percent_humidity(self):
        assert ntz.feels_like(30.0, 39, 0.0) == 30.0

    def test_passthrough_moderate_conditions(self):
        assert ntz.feels_like(20.0, 50, 0.0) == 20.0


# ── _health_score ─────────────────────────────────────────────────────────────

class TestHealthScore:
    @pytest.mark.parametrize("co2,expected", [
        (799, 0), (800, 1), (999, 1),
        (1000, 2), (1399, 2),
        (1400, 3), (1999, 3),
        (2000, 4),
    ])
    def test_co2_thresholds(self, co2, expected):
        # humidity=45 → score 0, so CO2 dominates
        assert ntz._health_score(co2, 45) == expected

    @pytest.mark.parametrize("hum,expected", [
        (30, 0), (45, 0), (60, 0),
        (25, 1), (29, 1), (61, 1), (70, 1),
        (20, 2), (24, 2), (71, 2), (75, 2),
        (15, 3), (19, 3), (76, 3), (80, 3),
        (10, 4), (85, 4),
    ])
    def test_humidity_thresholds(self, hum, expected):
        # CO2=500 → score 0, so humidity dominates
        assert ntz._health_score(500, hum) == expected

    def test_worst_of_co2_and_humidity_wins(self):
        assert ntz._health_score(500, 15) == 3   # co2=0, hum=3
        assert ntz._health_score(1400, 45) == 3  # co2=3, hum=0


# ── health_label ──────────────────────────────────────────────────────────────

class TestHealthLabel:
    def test_score_0_and_1_have_no_ventilation_advice(self):
        assert ntz.health_label(0, 20.0) == "Zdravá"
        assert ntz.health_label(1, 20.0) == "Dobrá"

    def test_normal_ventilation_advice(self):
        assert ntz.health_label(2, 20.0) == "Přijatelná – větrejte"
        assert ntz.health_label(3, 20.0) == "Špatná – větrejte"

    def test_score_4_urgent_ventilation(self):
        assert ntz.health_label(4, 20.0) == "Nezdravá – větrejte ihned"

    def test_extreme_cold_gives_short_ventilation(self):
        assert ntz.health_label(3, -11.0) == "Špatná – větrejte krátce"

    def test_extreme_heat_gives_short_ventilation(self):
        assert ntz.health_label(2, 36.0) == "Přijatelná – větrejte krátce"

    def test_extreme_cold_overrides_score_4_urgency(self):
        assert ntz.health_label(4, -15.0) == "Nezdravá – větrejte krátce"

    def test_boundary_minus_10_is_not_extreme(self):
        # < -10 required; exactly -10 is not extreme
        assert ntz.health_label(3, -10.0) == "Špatná – větrejte"

    def test_boundary_35_is_not_extreme(self):
        # > 35 required; exactly 35 is not extreme
        assert ntz.health_label(2, 35.0) == "Přijatelná – větrejte"


# ── _slugify ──────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_simple_ascii(self):
        assert ntz._slugify("Pracovna") == "pracovna"

    def test_czech_diacritics_stripped(self):
        assert ntz._slugify("Ložnice") == "loznice"
        assert ntz._slugify("Obývák") == "obyvak"

    def test_spaces_become_underscores(self):
        assert ntz._slugify("Obyvak Velky") == "obyvak_velky"

    def test_dashes_become_underscores(self):
        assert ntz._slugify("patro-2") == "patro_2"

    def test_leading_trailing_whitespace_stripped(self):
        assert ntz._slugify("  room  ") == "room"


# ── parse_measurements ────────────────────────────────────────────────────────

_STATION_PAYLOAD = {
    "body": {
        "devices": [{
            "dashboard_data": {
                "Temperature": 21.3,
                "Humidity": 45,
                "CO2": 850,
                "Pressure": 1013.0,
            },
            "modules": [
                {
                    "type": "NAModule1",
                    "dashboard_data": {"Temperature": 8.7, "Humidity": 72},
                },
                {
                    "type": "NAModule2",
                    "dashboard_data": {
                        "WindStrength": 12, "GustStrength": 18,
                        "WindAngle": 270, "max_wind_str": 22,
                    },
                },
                {
                    "type": "NAModule3",
                    "dashboard_data": {"Rain": 0.5, "sum_rain_1": 1.2, "sum_rain_24": 5.8},
                },
            ],
        }]
    }
}


class TestParseMeasurements:
    def setup_method(self):
        self.v = ntz.parse_measurements(_STATION_PAYLOAD)

    def test_indoor_fields(self):
        assert self.v["netatmo_indoor_temp"] == 21.3
        assert self.v["netatmo_indoor_humidity"] == 45
        assert self.v["netatmo_indoor_co2"] == 850
        assert self.v["netatmo_pressure"] == 1013.0

    def test_outdoor_fields(self):
        assert self.v["netatmo_outdoor_temp"] == 8.7
        assert self.v["netatmo_outdoor_humidity"] == 72

    def test_wind_fields(self):
        assert self.v["netatmo_wind_speed"] == 12
        assert self.v["netatmo_wind_gust"] == 18
        assert self.v["netatmo_wind_angle"] == 270
        assert self.v["netatmo_wind_max"] == 22

    def test_rain_fields(self):
        assert self.v["netatmo_rain_current"] == 0.5
        assert self.v["netatmo_rain_1h"] == 1.2
        assert self.v["netatmo_rain_24h"] == 5.8

    def test_empty_payload_returns_empty_dict(self):
        assert ntz.parse_measurements({}) == {}

    def test_partial_data_no_missing_key_errors(self):
        payload = {"body": {"devices": [{"dashboard_data": {"Temperature": 20.0}, "modules": []}]}}
        v = ntz.parse_measurements(payload)
        assert "netatmo_indoor_temp" in v
        assert "netatmo_outdoor_temp" not in v


# ── parse_homecoach_measurements ──────────────────────────────────────────────

_COACH_PAYLOAD = {
    "body": {
        "devices": [{
            "_id": "70:ee:50:83:3e:ce",
            "dashboard_data": {
                "Temperature": 22.0,
                "Humidity": 48,
                "CO2": 900,
                "Noise": 35,
                "health_idx": 1,
            },
        }]
    }
}


class TestParseHomecoachMeasurements:
    def test_known_mac_uses_room_name(self):
        with patch.object(ntz, "HOMECOACH_MAP", {"70:ee:50:83:3e:ce": "Pracovna"}):
            v, coaches = ntz.parse_homecoach_measurements(_COACH_PAYLOAD)
        assert v["netatmo_pracovna_temp"] == 22.0
        assert v["netatmo_pracovna_humidity"] == 48
        assert v["netatmo_pracovna_co2"] == 900
        assert v["netatmo_pracovna_noise"] == 35
        assert coaches[0]["slug"] == "pracovna"
        assert coaches[0]["health_idx"] == 1

    def test_unknown_mac_falls_back_to_mac_suffix(self):
        with patch.object(ntz, "HOMECOACH_MAP", {}):
            v, coaches = ntz.parse_homecoach_measurements(_COACH_PAYLOAD)
        # "70:ee:50:83:3e:ce".replace(":", "_")[-4:] = "e_ce", slugified = "e_ce"
        assert any("e_ce" in k for k in v)

    def test_empty_payload(self):
        v, coaches = ntz.parse_homecoach_measurements({})
        assert v == {}
        assert coaches == []


# ── fetch_openaq ──────────────────────────────────────────────────────────────

class TestFetchOpenaq:
    def test_returns_pm25_pm10_and_aqi(self):
        loc_resp = _mock_response({"results": [{"id": 42, "name": "Brno-Svatoplukova"}]})
        latest_resp = _mock_response({"results": [
            {"parameter": {"name": "pm25"}, "value": 12.0},
            {"parameter": {"name": "pm10"}, "value": 25.0},
        ]})
        with patch("requests.get", side_effect=[loc_resp, latest_resp]):
            result = ntz.fetch_openaq()
        assert result["openaq_pm25"] == 12.0
        assert result["openaq_pm10"] == 25.0
        assert result["openaq_aqi"] == "Dobrá"   # pm25→2, pm10→2 → max=2

    def test_station_not_found_returns_empty(self):
        resp = _mock_response({"results": []})
        with patch("requests.get", return_value=resp):
            assert ntz.fetch_openaq() == {}

    def test_none_values_are_skipped(self):
        loc_resp = _mock_response({"results": [{"id": 1, "name": "X"}]})
        latest_resp = _mock_response({"results": [
            {"parameter": {"name": "pm25"}, "value": None},
            {"parameter": {"name": "pm10"}, "value": 30.0},
        ]})
        with patch("requests.get", side_effect=[loc_resp, latest_resp]):
            result = ntz.fetch_openaq()
        assert "openaq_pm25" not in result
        assert result["openaq_pm10"] == 30.0

    def test_no_sensors_returns_empty(self):
        loc_resp = _mock_response({"results": [{"id": 1, "name": "X"}]})
        latest_resp = _mock_response({"results": []})
        with patch("requests.get", side_effect=[loc_resp, latest_resp]):
            assert ntz.fetch_openaq() == {}

    def test_network_error_returns_empty(self):
        with patch("requests.get", side_effect=Exception("timeout")):
            assert ntz.fetch_openaq() == {}

    def test_very_high_pm_gives_score_5(self):
        loc_resp = _mock_response({"results": [{"id": 1, "name": "X"}]})
        latest_resp = _mock_response({"results": [
            {"parameter": {"name": "pm25"}, "value": 60.0},
        ]})
        with patch("requests.get", side_effect=[loc_resp, latest_resp]):
            result = ntz.fetch_openaq()
        assert result["openaq_aqi"] == "Nezdravá"


# ── fetch_wind_speed ──────────────────────────────────────────────────────────

class TestFetchWindSpeed:
    def test_returns_wind_speed_from_api(self):
        resp = _mock_response({"current": {"wind_speed_10m": 15.5}})
        with patch("requests.get", return_value=resp), \
             patch.object(ntz, "LOCATION_LAT", 49.2), \
             patch.object(ntz, "LOCATION_LON", 16.6):
            assert ntz.fetch_wind_speed() == 15.5

    def test_returns_zero_on_network_error(self):
        with patch("requests.get", side_effect=Exception("err")), \
             patch.object(ntz, "LOCATION_LAT", 49.2), \
             patch.object(ntz, "LOCATION_LON", 16.6):
            assert ntz.fetch_wind_speed() == 0.0

    def test_returns_zero_when_coords_not_set(self):
        with patch.object(ntz, "LOCATION_LAT", 0.0), \
             patch.object(ntz, "LOCATION_LON", 0.0):
            assert ntz.fetch_wind_speed() == 0.0


# ── push_to_zo ────────────────────────────────────────────────────────────────

class TestPushToZo:
    def test_sends_values_with_import_key(self):
        with patch("requests.get", return_value=_mock_response("OK")) as mock_get:
            ntz.push_to_zo({"foo": 1, "bar": 2})
        params = mock_get.call_args[1]["params"]
        assert params["foo"] == 1
        assert params["bar"] == 2
        assert params["import_key"] == ntz.ZO_IMPORT_KEY

    def test_skips_request_on_empty_values(self):
        with patch("requests.get") as mock_get:
            ntz.push_to_zo({})
        mock_get.assert_not_called()


# ── token management ──────────────────────────────────────────────────────────

class TestTokenManagement:
    def test_load_tokens_reads_from_file(self, tmp_path):
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({"refresh_token": "file_token"}))
        with patch.object(ntz, "TOKEN_FILE", str(token_file)):
            assert ntz.load_tokens()["refresh_token"] == "file_token"

    def test_load_tokens_falls_back_to_env_when_file_missing(self, tmp_path):
        with patch.object(ntz, "TOKEN_FILE", str(tmp_path / "nonexistent.json")):
            tokens = ntz.load_tokens()
        assert tokens["refresh_token"] == ntz.NETATMO_REFRESH_TOKEN

    def test_save_tokens_writes_json(self, tmp_path):
        token_file = tmp_path / "tokens.json"
        with patch.object(ntz, "TOKEN_FILE", str(token_file)):
            ntz.save_tokens({"refresh_token": "saved_token"})
        assert json.loads(token_file.read_text())["refresh_token"] == "saved_token"

    def test_refresh_access_token_posts_correct_payload(self):
        resp = _mock_response({"access_token": "new_access", "refresh_token": "new_refresh"})
        with patch("requests.post", return_value=resp) as mock_post:
            result = ntz.refresh_access_token("old_refresh")
        assert result["access_token"] == "new_access"
        data = mock_post.call_args[1]["data"]
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "old_refresh"
        assert data["client_id"] == ntz.NETATMO_CLIENT_ID


# ── ask_llm ───────────────────────────────────────────────────────────────────

class TestAskLlm:
    def test_returns_stripped_response(self):
        resp = _mock_response({"response": "  Wear a coat.  "})
        with patch("requests.post", return_value=resp):
            assert ntz.ask_llm("some prompt") == "Wear a coat."

    def test_propagates_http_error(self):
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        with patch("requests.post", return_value=resp):
            with pytest.raises(requests.HTTPError):
                ntz.ask_llm("prompt")
