#!/usr/bin/env python3
"""Zendure device simulator.

Simulates: 2× Hyper 2000  +  1× SolarFlow 2400 AC+  with a 5 kW solar field.

Each device publishes realistic MQTT property reports every REPORT_INTERVAL
seconds, and responds to charge/discharge commands sent by Home Assistant.

The net grid power (P1) is pushed to Home Assistant via its REST API so the
Zendure manager can do power-matching.

Configuration via environment variables:
  MQTT_HOST          MQTT broker host             (default: localhost)
  MQTT_PORT          MQTT broker port — "cloud"   (default: 1883)
  MQTT_USER / PASS   Credentials (optional)
  SOLAR_PEAK_W       Peak solar production        (default: 5000)
  REPORT_INTERVAL    Seconds between reports      (default: 5)
  HA_URL             Home Assistant URL           (default: http://localhost:8123)
  HA_TOKEN           Long-lived access token      (required for P1 update)

  --- Local MQTT (Hyper 2000 → status 11 in HA) ---
  LOCAL_MQTT_PORT    Port for "local" MQTT broker (default: 1884, 0 = disabled)
                     Hyper 2000 devices publish here so HA receives them on
                     Api.mqttLocal and auto-sets device.mqtt = localClient.

  --- ZenSDK HTTP (SolarFlow 2400AC+ → status 12 in HA) ---
  HTTP_PORT          Port to expose the ZenSDK HTTP server (default: 8088, 0 = disabled)
                     HA polls GET http://{ipAddress}/properties/report
                     and sends commands via POST http://{ipAddress}/properties/write.
                     The ipAddress is set in mock_api/server.py (SIMULATOR_HOST:HTTP_PORT).

  --- Debugging ---
  VERBOSE            Log every MQTT message received (0/1, default: 0)
                     Subscribes to '#' wildcard on all brokers so no message
                     is missed.  Filters out noisy /report echoes.
"""

import json
import math
import os
import random
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import paho.mqtt.client as mqtt_client

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))  # "cloud" broker port
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

LOCAL_MQTT_PORT = int(os.environ.get("LOCAL_MQTT_PORT", "1884"))  # 0 = disabled
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8088"))  # 0 = disabled

SOLAR_PEAK_W = int(os.environ.get("SOLAR_PEAK_W", "5000"))
REPORT_INTERVAL = float(os.environ.get("REPORT_INTERVAL", "5"))

HA_URL = os.environ.get("HA_URL", "http://localhost:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")  # optional

VERBOSE = os.environ.get("VERBOSE", "0") not in ("", "0")  # log all MQTT RX

# ──────────────────────────────────────────────────────────────────────────────
# Device definitions — must match mock_api/server.py
# ──────────────────────────────────────────────────────────────────────────────
# use_local_mqtt : if True and LOCAL_MQTT_PORT > 0, the device publishes on the
#                  "local" MQTT client (port LOCAL_MQTT_PORT).
#                  HA receives messages on Api.mqttLocal → device.mqtt auto-set
#                  to localClient → connectionStatus = 11 (local).
#
# http_enabled   : if True and HTTP_PORT > 0, this device's state is also served
#                  via the built-in HTTP server for ZenSDK mode.
#                  Toggle "connection → zenSDK" in HA UI to activate.
DEVICES_CONFIG: list[dict[str, Any]] = [
    {
        "device_id": "HYPER2000SIM01",
        "prod_key": "simkey001",
        "sn": "SIM2000001",
        # Two AB2000S packs.  ZendureBattery recognises sn[0]='C' + sn[3]='F'
        # as AB2000S, kWh=1.92 each → device.kWh = 3.84 kWh total.
        "pack_sns": ["CB2FA0A1", "CB2FA0A2"],
        "name": "Hyper 2000 A",
        "type": "hyper2000",
        "kwh": 3840,
        "max_charge": 1200,
        "max_discharge": 1200,
        "max_solar": 1600,
        "solar_fraction": 0.30,
        "initial_soc": 20.0,
        "use_local_mqtt": True,  # publishes to LOCAL_MQTT_PORT → status 11
    },
    {
        "device_id": "HYPER2000SIM02",
        "prod_key": "simkey002",
        "sn": "SIM2000002",
        # Two AB2000S packs → device.kWh = 3.84 kWh total.
        "pack_sns": ["CB2FB0B1", "CB2FB0B2"],
        "name": "Hyper 2000 B",
        "type": "hyper2000",
        "kwh": 3840,
        "max_charge": 1200,
        "max_discharge": 1200,
        "max_solar": 1600,
        "solar_fraction": 0.30,
        "initial_soc": 20.0,
        "use_local_mqtt": True,  # publishes to LOCAL_MQTT_PORT → status 11
    },
    {
        "device_id": "SF2400ACSIM01",
        "prod_key": "simkey003",
        "sn": "SIMSF2400001",
        # One AB3000L pack.  ZendureBattery recognises sn[0]='J' as AB3000L,
        # kWh=2.88 → device.kWh = 2.88 kWh.
        "pack_sns": ["JB3L0001"],
        "name": "SolarFlow 2400AC+",
        "type": "sf2400ac_plus",
        "kwh": 2880,
        "max_charge": 3200,
        "max_discharge": 2400,
        "max_solar": 2400,
        "solar_fraction": 0.40,
        "initial_soc": 20.0,
        "http_enabled": True,  # served via HTTP for ZenSDK mode (status 12)
    },
]

# Typical home consumption by hour (W) — base values, noise added at runtime
HOME_PROFILE: dict[int, int] = {
    0: 200,
    1: 150,
    2: 150,
    3: 150,
    4: 200,
    5: 250,
    6: 600,
    7: 1200,
    8: 800,
    9: 500,
    10: 400,
    11: 450,
    12: 800,
    13: 600,
    14: 400,
    15: 350,
    16: 450,
    17: 800,
    18: 1500,
    19: 1800,
    20: 1200,
    21: 800,
    22: 500,
    23: 300,
}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def solar_power_now(sim_hour: float, peak_w: int = SOLAR_PEAK_W) -> float:
    """Sinusoidal solar curve, 0 W before 6 h and after 20 h, peak at 13 h."""
    if sim_hour < 6.0 or sim_hour > 20.0:
        return 0.0
    angle = math.pi * (sim_hour - 6.0) / 14.0
    variation = 1.0 + random.uniform(-0.05, 0.05)
    return max(0.0, peak_w * math.sin(angle) * variation)


def home_consumption_now(sim_hour: float) -> int:
    """Stochastic home consumption based on hour-of-day profile."""
    base = HOME_PROFILE[int(sim_hour) % 24]
    noise = random.uniform(-0.25, 0.25) * base
    return max(50, int(base + noise))


# ──────────────────────────────────────────────────────────────────────────────
# Device state
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class DeviceState:
    soc: float = 50.0
    solar_power: int = 0
    charge_power: int = 0
    discharge_power: int = 0
    home_power: int = 0
    grid_import: int = 0
    input_limit: int = 0
    output_limit: int = 0
    min_soc: float = 10.0
    soc_target: float = 100.0
    mode: str = "off"  # off | charge | discharge | auto
    # Starts in "off" so the Zendure manager in HA is the sole decision-maker.
    # P1 = home - solar (no battery contribution) → HA sees a real P1 and sends
    # charge/discharge commands via MQTT → closed-loop control.
    commanded_charge_w: int = 0
    commanded_discharge_w: int = 0
    ac_mode: int = 1  # 1=charge, 2=discharge (SF2400AC+)


# ──────────────────────────────────────────────────────────────────────────────
# Per-device simulator
# ──────────────────────────────────────────────────────────────────────────────
class DeviceSimulator:
    def __init__(
        self,
        config: dict[str, Any],
        client_pub: mqtt_client.Client,  # client used to PUBLISH reports
    ) -> None:
        self.config = config
        self.client_pub = client_pub  # may differ from the subscribing client
        self.state = DeviceState(
            soc=float(config["initial_soc"]),
            input_limit=config["max_charge"],
            output_limit=config["max_discharge"],
        )
        self._lock = threading.Lock()
        self._msg_id = 0
        self._pack_sns = config.get("pack_sns", [config["sn"]])
        self._n_packs = len(self._pack_sns)

        pk = config["prod_key"]
        did = config["device_id"]
        self._topic_report = f"iot/{pk}/{did}/properties/report"
        self._topic_read = f"iot/{pk}/{did}/properties/read"
        self._topic_write = f"iot/{pk}/{did}/properties/write"
        self._topic_function = f"iot/{pk}/{did}/function/invoke"

        print(f"  [{config['name']}] ready — SOC {self.state.soc:.0f}%")

    # ── MQTT command handlers ─────────────────────────────────────────────────

    def handle_message(self, topic: str, payload: dict[str, Any]) -> None:
        if topic == self._topic_write:
            self._handle_write(payload)
        elif topic == self._topic_function:
            self._handle_function(payload)
        elif topic == self._topic_read:
            # HA polls for full state after connection — reply immediately.
            print(f"  [{self.config['name']}] properties/read → sending report")
            self.publish_report()

    def _handle_write(self, payload: dict[str, Any]) -> None:
        """Process properties/write from HA (also used by ZenSDK HTTP POST)."""
        props = payload.get("properties", {})
        with self._lock:
            s = self.state
            if "inputLimit" in props:
                s.input_limit = int(props["inputLimit"])
                print(f"  [{self.config['name']}] inputLimit → {s.input_limit} W")
            if "outputLimit" in props:
                s.output_limit = int(props["outputLimit"])
                print(f"  [{self.config['name']}] outputLimit → {s.output_limit} W")
            if "minSoc" in props:
                # HA writes on the 0-1000 scale (factor=10 in ZendureNumber).
                # Convert back to plain percent for internal use.
                s.min_soc = float(props["minSoc"]) / 10
            if "socSet" in props:
                s.soc_target = float(props["socSet"]) / 10
            if "acMode" in props:
                mode = int(props["acMode"])
                s.ac_mode = mode
                if mode == 1:
                    s.mode = "charge"
                    s.commanded_charge_w = s.input_limit
                    s.commanded_discharge_w = 0
                    print(
                        f"  [{self.config['name']}] → CHARGE {s.commanded_charge_w} W"
                    )
                elif mode == 2:
                    s.mode = "discharge"
                    s.commanded_discharge_w = s.output_limit
                    s.commanded_charge_w = 0
                    print(
                        f"  [{self.config['name']}] → DISCHARGE {s.commanded_discharge_w} W"
                    )
            if "smartMode" in props and int(props["smartMode"]) == 0:
                s.mode = "off"
                s.commanded_charge_w = 0
                s.commanded_discharge_w = 0
                print(f"  [{self.config['name']}] → OFF (smartMode=0)")

    def _handle_function(self, payload: dict[str, Any]) -> None:
        """Process function/invoke from HA (Hyper 2000)."""
        for arg in payload.get("arguments", []):
            auto_model = int(arg.get("autoModel", 0))
            program = int(arg.get("autoModelProgram", 0))
            value = arg.get("autoModelValue", {})
            with self._lock:
                s = self.state
                if auto_model == 0:
                    s.mode = "off"
                    s.commanded_charge_w = 0
                    s.commanded_discharge_w = 0
                    print(f"  [{self.config['name']}] → OFF")
                elif auto_model == 8:
                    if program == 1:
                        pwr = abs(int(value.get("chargingPower", 0)))
                        s.mode = "charge"
                        s.commanded_charge_w = min(pwr, s.input_limit)
                        s.commanded_discharge_w = 0
                        print(
                            f"  [{self.config['name']}] → CHARGE {s.commanded_charge_w} W"
                        )
                    elif program == 2:
                        pwr = int(value.get("outPower", 0))
                        s.mode = "discharge"
                        s.commanded_discharge_w = min(pwr, s.output_limit)
                        s.commanded_charge_w = 0
                        print(
                            f"  [{self.config['name']}] → DISCHARGE {s.commanded_discharge_w} W"
                        )

    # ── Simulation tick ───────────────────────────────────────────────────────

    def tick(self, dt: float) -> None:
        """Advance simulation by dt seconds.

        AC-coupled model: solar is external to the batteries (it feeds the
        house AC bus).  Batteries only charge/discharge on HA command.
        P1 is computed at the house level in main().
        """
        with self._lock:
            s = self.state
            s.solar_power = 0  # no direct DC solar into batteries

            match s.mode:
                case "off":
                    s.charge_power = 0
                    s.discharge_power = 0
                case "charge":
                    s.charge_power = min(s.commanded_charge_w, s.input_limit)
                    if s.soc >= s.soc_target:
                        s.charge_power = 0
                    s.discharge_power = 0
                case "discharge":
                    s.discharge_power = min(s.commanded_discharge_w, s.output_limit)
                    if s.soc <= s.min_soc:
                        s.discharge_power = 0
                    s.charge_power = 0
                case _:  # "auto" kept for completeness but not used by default
                    s.charge_power = 0
                    s.discharge_power = 0

            net_w = s.charge_power - s.discharge_power
            delta_soc = (net_w * (dt / 3600.0) / self.config["kwh"]) * 100.0
            s.soc = max(s.min_soc, min(s.soc_target, s.soc + delta_soc))

            # AC-coupled: battery feeds/draws from the AC bus.
            s.home_power = s.discharge_power  # power sent to home AC bus
            s.grid_import = s.charge_power  # power drawn from AC bus to charge

    # ── MQTT publish ──────────────────────────────────────────────────────────

    def publish_report(self) -> None:
        self._msg_id += 1
        s = self.state
        props = self._build_props()
        # Each battery pack SN must have a prefix recognised by ZendureBattery
        # (sn[0]='A' + sn[3]='3' → AIO2400, kWh=2.4).  Power is split evenly.
        pack_sns = self._pack_sns
        n = self._n_packs
        payload = {
            "deviceId": self.config["device_id"],
            "messageId": self._msg_id,
            "timestamp": int(time.time()),
            "properties": props,
            "packData": [
                {
                    "sn": sn,
                    "electricLevel": round(s.soc),
                    "outputPackPower": s.discharge_power // n,
                    "packInputPower": s.charge_power // n,
                    "heatState": 0,
                }
                for sn in pack_sns
            ],
        }
        self.client_pub.publish(self._topic_report, json.dumps(payload))

    def _build_props(self) -> dict[str, Any]:
        s = self.state
        props: dict[str, Any] = {
            "electricLevel": round(s.soc),
            "solarInputPower": s.solar_power,
            "gridInputPower": s.grid_import,
            "outputPackPower": s.discharge_power,
            "packInputPower": s.charge_power,
            "outputHomePower": s.home_power,
            "chargeMaxLimit": self.config["max_charge"],
            "inverseMaxPower": self.config["max_discharge"],
            "outputLimit": s.output_limit,
            "inputLimit": s.input_limit,
            # HA's ZendureNumber for minSoc and socSet uses factor=10, so it
            # divides the received value by 10 before storing.  Real Zendure
            # devices report on a 0-1000 scale (tenths of percent).  We must
            # match that scale so HA sees the correct percentage values.
            # e.g.  min_soc=10.0 %  → send 100  → HA stores 100/10 = 10 %
            #        soc_target=100 % → send 1000 → HA stores 1000/10 = 100 %
            "minSoc": round(s.min_soc * 10),
            "socSet": round(s.soc_target * 10),
            "hemsState": 0,  # MUST be 0: value 1 forces connectionStatus=2 (OFFLINE) in HA
            "socStatus": 0,
            "socLimit": 0,
        }
        if self.config["type"] == "sf2400ac_plus":
            props["gridOffPower"] = 0
            props["acMode"] = s.ac_mode
        return props

    # ── ZenSDK HTTP report ────────────────────────────────────────────────────

    def http_report(self) -> dict[str, Any]:
        """Return the JSON payload expected by ZendureZenSdk.httpGet().

        HA calls mqttProperties(result) with the value from httpGet(), which
        passes the dict directly to mqttProperties.  mqttProperties expects the
        same structure as a properties/report MQTT payload: a "properties" key
        and a "packData" list.  Without packData, device.kWh stays 0 and
        power_get() returns DeviceState.OFFLINE, so the manager ignores the
        device entirely.
        """
        s = self.state
        pack_sns = self._pack_sns
        n = self._n_packs
        return {
            "properties": self._build_props(),
            "packData": [
                {
                    "sn": sn,
                    "electricLevel": round(s.soc),
                    "outputPackPower": s.discharge_power // n,
                    "packInputPower": s.charge_power // n,
                    "heatState": 0,
                }
                for sn in pack_sns
            ],
        }

    # ── Status ────────────────────────────────────────────────────────────────

    def status(self) -> str:
        s = self.state
        lbl = {
            "auto": "AUTO",
            "charge": "CHG ",
            "discharge": "DIS ",
            "off": "OFF ",
        }.get(s.mode, s.mode)
        tag = ""
        if self.config.get("use_local_mqtt") and LOCAL_MQTT_PORT > 0:
            tag = " [local]"
        elif self.config.get("http_enabled") and HTTP_PORT > 0:
            tag = " [zensdk]"
        return (
            f"  {self.config['name']:<22}{tag:<9}| SOC {s.soc:5.1f}% | "
            f"Bat +{s.charge_power:4d}/-{s.discharge_power:4d}W | [{lbl}]"
        )


# ──────────────────────────────────────────────────────────────────────────────
# ZenSDK HTTP server
# ──────────────────────────────────────────────────────────────────────────────
# Shared reference: the HTTP handler needs access to HTTP-enabled simulators.
# Keyed by snNumber so that POST /properties/write can route via the "sn" field
# in the HA payload (body: {"id": N, "sn": "...", "properties": {...}}).
_http_sims: dict[str, "DeviceSimulator"] = {}

# UI-controllable overrides (protected by _ui_lock)
_ui_lock = threading.Lock()
_ui_solar_peak_w: int | None = None  # None → use SOLAR_PEAK_W
_ui_hour_freeze: float | None = None  # None → follow real/auto clock
_ui_home_w: int | None = None  # None → use HOME_PROFILE
_ui_status: dict[str, Any] = {}  # current status snapshot (updated by main loop)
_ui_soc_overrides: dict[str, float] = {}  # one-shot SOC set, keyed by device name


class _ZenSDKHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server mimicking the ZenSDK REST API of a Zendure device."""

    def _send_json(self, code: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/properties/report":
            # If there is only one ZenSDK device, serve it directly.
            # For multiple devices we'd need per-IP routing; one device is enough here.
            if not _http_sims:
                self._send_json(503, {"error": "no device"})
                return
            sim = next(iter(_http_sims.values()))
            self._send_json(200, sim.http_report())
        elif self.path in ("/", "/ui"):
            self._serve_ui()
        elif self.path == "/api/status":
            self._serve_status()
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/properties/write":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
            except Exception:
                self._send_json(400, {"error": "bad json"})
                return
            sn = body.get("sn", "")
            sim = _http_sims.get(sn) or (
                next(iter(_http_sims.values())) if _http_sims else None
            )
            if sim is None:
                self._send_json(404, {"error": "device not found"})
                return
            sim._handle_write({"properties": body.get("properties", {})})
            self._send_json(200, {"success": True, "sn": sn})
        elif self.path == "/api/control":
            self._handle_control()
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        # Suppress frequent /api/status polls to avoid log spam.
        if self.path == "/api/status":
            return
        print(f"[HTTP-ZenSDK] {fmt % args}")

    # ── Web UI ────────────────────────────────────────────────────────────────

    def _serve_ui(self) -> None:
        html = _UI_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_status(self) -> None:
        with _ui_lock:
            status = dict(_ui_status)
        self._send_json(200, status)

    def _handle_control(self) -> None:
        global _ui_solar_peak_w, _ui_hour_freeze, _ui_home_w, _ui_soc_overrides
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._send_json(400, {"ok": False, "error": "bad json"})
            return
        with _ui_lock:
            if "solar_peak_w" in body and body["solar_peak_w"] is not None:
                _ui_solar_peak_w = max(0, int(body["solar_peak_w"]))
            if "hour" in body:
                _ui_hour_freeze = (
                    None if body["hour"] is None else float(body["hour"]) % 24.0
                )
            if "home_w" in body:
                _ui_home_w = (
                    None if body["home_w"] is None else max(0, int(body["home_w"]))
                )
            if "soc_overrides" in body:
                for item in body["soc_overrides"]:
                    name = item.get("name")
                    soc = item.get("soc")
                    if name is not None and soc is not None:
                        _ui_soc_overrides[name] = max(0.0, min(100.0, float(soc)))
        self._send_json(200, {"ok": True})


_UI_HTML = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zendure Simulator</title>
<style>
body{font-family:sans-serif;background:#1a1a2e;color:#ddd;margin:20px;max-width:820px}
h1{color:#a0c4ff;margin-bottom:4px}h2{color:#80b3ff;margin:16px 0 8px}
.card{background:#16213e;border-radius:8px;padding:16px;margin:12px 0}
.row{display:flex;gap:20px;flex-wrap:wrap;margin:8px 0}
.stat{min-width:130px}.stat-val{font-size:1.4em;font-weight:bold;color:#a0c4ff}
.stat-lbl{font-size:.8em;color:#888}
table{border-collapse:collapse;width:100%}
th{color:#80b3ff;text-align:left;padding:6px 10px;border-bottom:1px solid #333}
td{padding:6px 10px;border-bottom:1px solid #222}
.off{color:#888}.charge{color:#7fff7f}.discharge{color:#ff8888}
label{display:block;margin:8px 0;color:#ccc}
input[type=number]{background:#0f3460;color:#eee;border:1px solid #555;
  padding:4px 8px;border-radius:4px;width:110px}
input[type=checkbox]{margin-right:6px}
button{background:#533483;color:#fff;border:none;padding:10px 24px;
  border-radius:6px;cursor:pointer;margin-top:8px;font-size:1em}
button:hover{background:#7c5cbf}
.msg{margin-left:12px;color:#7fff7f}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head><body>
<h1>Zendure Simulator</h1>
<div class="card"><h2>Live Status</h2><div class="row" id="stats">Loading...</div></div>
<div class="card"><h2>Devices</h2>
<table id="devices"><tr><th>Device</th><th>SOC</th><th>Mode</th><th>Power</th></tr></table>
</div>
<div class="card"><h2>Controls</h2>
<div class="grid">
  <div><b>Simulated Hour</b>
    <label><input type="checkbox" id="hour-auto" onchange="syncHour()"> Auto (follow clock)</label>
    <label>Hour (0-23.99): <input type="number" id="hour-val" min="0" max="23.99" step="0.25" value="12"></label>
  </div>
  <div><b>Solar Peak Power</b>
    <label>Peak (W): <input type="number" id="solar-peak" min="0" max="20000" step="100" value="5000"></label>
  </div>
  <div><b>Home Load</b>
    <label><input type="checkbox" id="home-auto" onchange="syncHome()"> Auto (follow profile)</label>
    <label>Load (W): <input type="number" id="home-val" min="0" max="20000" step="50" value="500"></label>
  </div>
</div>
<div id="soc-controls"></div>
<button onclick="applyControls()">Apply</button><span class="msg" id="msg"></span>
</div>
<script>
var inited=false;var _devNames=[];
function syncHour(){document.getElementById('hour-val').disabled=document.getElementById('hour-auto').checked;}
function syncHome(){document.getElementById('home-val').disabled=document.getElementById('home-auto').checked;}
function modeClass(m){return m==='charge'?'charge':m==='discharge'?'discharge':'off';}
async function refresh(){
  try{
    var d=await(await fetch('/api/status')).json();
    var h=Math.floor(d.sim_hour),mn=Math.round((d.sim_hour%1)*60).toString().padStart(2,'0');
    var p1c=d.net_grid_w>0?'#ff8888':'#7fff7f';
    document.getElementById('stats').innerHTML=
      '<div class="stat"><div class="stat-val">'+h+':'+mn+'</div><div class="stat-lbl">Sim time</div></div>'+
      '<div class="stat"><div class="stat-val">'+d.total_solar_w+' W</div><div class="stat-lbl">Solar</div></div>'+
      '<div class="stat"><div class="stat-val">'+d.total_home_w+' W</div><div class="stat-lbl">Home load</div></div>'+
      '<div class="stat"><div class="stat-val" style="color:'+p1c+'">'+(d.net_grid_w>0?'+':'')+d.net_grid_w+' W</div><div class="stat-lbl">Grid (P1)</div></div>'+
      '<div class="stat"><div class="stat-val">'+d.solar_peak_w+' W</div><div class="stat-lbl">Solar peak</div></div>';
    var rows=d.devices.map(function(dev){
      var pwr=dev.charge_w>0?'+'+dev.charge_w+' W':dev.discharge_w>0?'-'+dev.discharge_w+' W':'0 W';
      return '<tr><td>'+dev.name+'</td><td>'+dev.soc+'%</td><td class="'+modeClass(dev.mode)+'">'+dev.mode+'</td><td>'+pwr+'</td></tr>';
    }).join('');
    document.getElementById('devices').innerHTML='<tr><th>Device</th><th>SOC</th><th>Mode</th><th>Power</th></tr>'+rows;
    if(!inited){
      document.getElementById('solar-peak').value=d.solar_peak_w;
      document.getElementById('hour-auto').checked=!d.hour_frozen;
      if(d.hour_frozen)document.getElementById('hour-val').value=d.sim_hour.toFixed(2);
      document.getElementById('home-auto').checked=!d.home_overridden;
      if(d.home_overridden)document.getElementById('home-val').value=d.total_home_w;
      syncHour();syncHome();
      _devNames=d.devices.map(function(dev){return dev.name;});
      var socH='<div style="margin-top:16px"><b>Battery SOC</b><div class="row" style="margin-top:8px">';
      d.devices.forEach(function(dev,i){socH+='<div class="stat"><label>'+dev.name+' (%)<br><input type="number" id="soc-'+i+'" min="0" max="100" step="1" value="'+Math.round(dev.soc)+'"> <button onclick="setSocIdx('+i+')">Set</button></label></div>';});
      socH+='</div></div>';
      document.getElementById('soc-controls').innerHTML=socH;
      inited=true;
    }
  }catch(e){}
}
async function setSocIdx(i){
  var inp=document.getElementById('soc-'+i);
  if(!inp)return;
  var val=parseFloat(inp.value);
  if(isNaN(val)||val<0||val>100)return;
  try{
    var r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({soc_overrides:[{name:_devNames[i],soc:val}]})});
    var d=await r.json();
    var m=document.getElementById('msg');m.textContent=d.ok?' SOC applied':' Error';
    setTimeout(function(){m.textContent='';},3000);
  }catch(e){document.getElementById('msg').textContent=' Error';}
}
async function applyControls(){
  var payload={solar_peak_w:parseInt(document.getElementById('solar-peak').value)};
  payload.hour=document.getElementById('hour-auto').checked?null:parseFloat(document.getElementById('hour-val').value);
  payload.home_w=document.getElementById('home-auto').checked?null:parseInt(document.getElementById('home-val').value);
  try{
    var r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var d=await r.json();
    var m=document.getElementById('msg');
    m.textContent=d.ok?' Applied':' Error';
    setTimeout(function(){m.textContent='';},3000);
  }catch(e){document.getElementById('msg').textContent=' Error';}
}
refresh();setInterval(refresh,2000);
</script>
</body></html>
"""


def _start_http_server() -> None:
    server = HTTPServer(("0.0.0.0", HTTP_PORT), _ZenSDKHandler)
    print(f"[HTTP-ZenSDK] Listening on 0.0.0.0:{HTTP_PORT}")
    print(f"[HTTP-ZenSDK]   GET  /properties/report")
    print(f"[HTTP-ZenSDK]   POST /properties/write")
    server.serve_forever()


# ──────────────────────────────────────────────────────────────────────────────
# P1 push to Home Assistant REST API
# ──────────────────────────────────────────────────────────────────────────────
_p1_push_ok: bool = False  # True after first successful push


def push_p1_to_ha(net_grid_w: int) -> None:
    global _p1_push_ok
    if not HA_TOKEN:
        return

    url = f"{HA_URL}/api/states/input_number.p1_power"
    data = json.dumps({"state": str(net_grid_w)}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            http_status = resp.status
        if not _p1_push_ok:
            print(
                f"[P1] First push OK (HTTP {http_status}) → {url}  value={net_grid_w} W"
            )
            _p1_push_ok = True
    except Exception as exc:
        # Log every failure so the user can diagnose token / URL problems.
        print(f"[P1] Push FAILED: {exc}")
        print(f"[P1]   url   = {url}")
        print(
            f"[P1]   token = {HA_TOKEN[:20]}…  (set HA_TOKEN correctly if this looks wrong)"
        )
        _p1_push_ok = False


# ──────────────────────────────────────────────────────────────────────────────
# MQTT client factory
# ──────────────────────────────────────────────────────────────────────────────
def _make_mqtt_client(
    client_id: str,
    host: str,
    port: int,
    simulators: list[DeviceSimulator],
) -> mqtt_client.Client:
    """Create, configure, and return a connected MQTT client."""

    client = mqtt_client.Client(
        mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt_client.MQTTv311,
    )

    def on_connect(c, _ud, _flags, rc, _props) -> None:
        label = f"{host}:{port}"
        if rc == 0:
            print(f"[MQTT] {label} connected ({client_id}).")
            # Subscribe to write/function/read topics for devices that publish on this client.
            for sim in simulators:
                if sim.client_pub is c:
                    c.subscribe(sim._topic_read)
                    c.subscribe(sim._topic_write)
                    c.subscribe(sim._topic_function)
            # In verbose mode, also subscribe to the wildcard so we catch
            # any topic HA sends that our specific subscriptions might miss.
            if VERBOSE:
                c.subscribe("#")
                print(f"[MQTT] {label} VERBOSE: subscribed to '#' wildcard")
        else:
            print(f"[MQTT] {label} connection failed: rc={rc}")

    def on_message(_c, _ud, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())

            # Verbose: log every inbound message except our own /report echoes.
            if VERBOSE and not msg.topic.endswith("/report"):
                truncated = json.dumps(payload)
                if len(truncated) > 300:
                    truncated = truncated[:300] + "…"
                print(f"[MQTT-RX {host}:{port}] {msg.topic}: {truncated}")

            if "isHA" in payload:
                if VERBOSE:
                    print(f"[MQTT-RX] ↑ filtered (isHA flag set)")
                return
            for sim in simulators:
                if msg.topic in (
                    sim._topic_read,
                    sim._topic_write,
                    sim._topic_function,
                ):
                    sim.handle_message(msg.topic, payload)
                    break
            else:
                # Message arrived on a subscribed topic but matched no device.
                if VERBOSE and not msg.topic.endswith("/report"):
                    print(f"[MQTT-RX] ↑ no handler for this topic")
        except Exception as exc:
            print(f"[MQTT] Error: {exc}")

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[MQTT] Connecting {client_id} to {host}:{port} …")
    client.connect(host, port, keepalive=60)
    client.loop_start()
    return client


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
def main() -> None:
    local_mode = LOCAL_MQTT_PORT > 0
    zensdk = HTTP_PORT > 0

    print("=" * 72)
    print("Zendure device simulator")
    print(f"  Cloud MQTT:     {MQTT_HOST}:{MQTT_PORT}")
    if local_mode:
        print(
            f"  Local MQTT:     {MQTT_HOST}:{LOCAL_MQTT_PORT}  (Hyper 2000 → status 11)"
        )
    if zensdk:
        print(
            f"  ZenSDK HTTP:    0.0.0.0:{HTTP_PORT}  (SF2400AC+ → toggle connection=zenSDK in HA)"
        )
    print(f"  Solar peak:     {SOLAR_PEAK_W} W")
    print(f"  Report every:   {REPORT_INTERVAL} s")
    print(
        f"  HA P1 push:     {'enabled' if HA_TOKEN else 'disabled (set HA_TOKEN to enable)'}"
    )
    print(f"  Verbose MQTT:   {'enabled (VERBOSE=1)' if VERBOSE else 'disabled'}")
    print("=" * 72)

    # ── Build device simulators (two-pass: first create stubs, then wire MQTT) ──
    # We need simulators before we can create MQTT clients (on_connect subscribes
    # to device topics).  Solve with a placeholder list that we fill in-place.
    simulators: list[DeviceSimulator] = []

    # Temporary "dummy" clients just to know which port each device needs.
    # Real clients will be created below.
    _cloud_placeholder: list[mqtt_client.Client] = [None]  # type: ignore[list-item]
    _local_placeholder: list[mqtt_client.Client] = [None]  # type: ignore[list-item]

    # First pass: instantiate simulators with a placeholder client.
    # We will replace client_pub references after the real clients are created.
    for cfg in DEVICES_CONFIG:
        use_local = cfg.get("use_local_mqtt", False) and local_mode
        # Temporarily use None; replaced below.
        sim = DeviceSimulator(cfg, None)  # type: ignore[arg-type]
        sim._use_local = use_local
        simulators.append(sim)
        if cfg.get("http_enabled") and zensdk:
            _http_sims[cfg["sn"]] = sim

    # ── Create MQTT clients ────────────────────────────────────────────────────
    # Cloud client: used by SF2400AC+ (and as fallback if local is disabled).
    client_cloud = _make_mqtt_client(
        "zendure-sim-cloud", MQTT_HOST, MQTT_PORT, simulators
    )
    time.sleep(0.5)

    # Local client: used by Hyper 2000s.
    client_local: mqtt_client.Client | None = None
    if local_mode:
        client_local = _make_mqtt_client(
            "zendure-sim-local", MQTT_HOST, LOCAL_MQTT_PORT, simulators
        )
        time.sleep(0.5)

    # Second pass: assign the real MQTT publishing client to each simulator.
    print("Devices:")
    for sim in simulators:
        use_local = getattr(sim, "_use_local", False)
        sim.client_pub = client_local if (use_local and client_local) else client_cloud
        # Subscribe this device's command topics on its publishing client.
        sim.client_pub.subscribe(sim._topic_read)
        sim.client_pub.subscribe(sim._topic_write)
        sim.client_pub.subscribe(sim._topic_function)
        tag = " (local MQTT)" if (use_local and client_local) else ""
        tag = " (ZenSDK HTTP)" if sim.config.get("http_enabled") and zensdk else tag
        print(f"  [{sim.config['name']}] SOC {sim.state.soc:.0f}%{tag}")

    # ── HTTP server for ZenSDK ─────────────────────────────────────────────────
    if zensdk and _http_sims:
        t = threading.Thread(target=_start_http_server, daemon=True)
        t.start()

    # ── Simulated time tracking ────────────────────────────────────────────────
    now_dt = datetime.now()
    sim_seconds = now_dt.hour * 3600 + now_dt.minute * 60 + now_dt.second

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    def shutdown(sig=None, _frame=None) -> None:
        print("\n[Simulator] Shutting down …")
        client_cloud.loop_stop()
        client_cloud.disconnect()
        if client_local:
            client_local.loop_stop()
            client_local.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Simulation loop ────────────────────────────────────────────────────────
    print("\nRunning — press Ctrl+C to stop.\n")
    last_tick = time.time()

    while True:
        real_dt = time.time() - last_tick
        last_tick = time.time()
        sim_dt = real_dt

        sim_seconds += sim_dt
        sim_hour = (sim_seconds / 3600.0) % 24.0

        # Apply UI overrides.
        with _ui_lock:
            solar_peak = (
                _ui_solar_peak_w if _ui_solar_peak_w is not None else SOLAR_PEAK_W
            )
            hour_freeze = _ui_hour_freeze
            home_override = _ui_home_w
            soc_snap = dict(_ui_soc_overrides)
            _ui_soc_overrides.clear()
        if hour_freeze is not None:
            sim_hour = hour_freeze
        total_solar = solar_power_now(sim_hour, solar_peak)
        total_home = (
            home_override
            if home_override is not None
            else home_consumption_now(sim_hour)
        )

        for sim in simulators:
            if sim.config["name"] in soc_snap:
                with sim._lock:
                    sim.state.soc = max(
                        sim.state.min_soc,
                        min(100.0, soc_snap[sim.config["name"]]),
                    )
            sim.tick(sim_dt)
            sim.publish_report()

        # AC-coupled P1: solar feeds the house AC bus directly.
        # P1 = home load − solar production − battery net (discharge − charge).
        # Positive = importing from grid, negative = exporting.
        bat_net = sum(
            s.state.discharge_power - s.state.charge_power for s in simulators
        )
        net_grid = total_home - total_solar - bat_net
        push_p1_to_ha(int(net_grid))

        # Update the live status snapshot for the web UI.
        with _ui_lock:
            _ui_status.update(
                {
                    "sim_hour": sim_hour,
                    "total_solar_w": int(total_solar),
                    "total_home_w": total_home,
                    "net_grid_w": int(net_grid),
                    "solar_peak_w": solar_peak,
                    "hour_frozen": hour_freeze is not None,
                    "home_overridden": home_override is not None,
                    "devices": [
                        {
                            "name": s.config["name"],
                            "soc": round(s.state.soc, 1),
                            "mode": s.state.mode,
                            "charge_w": s.state.charge_power,
                            "discharge_w": s.state.discharge_power,
                        }
                        for s in simulators
                    ],
                }
            )

        ts = datetime.now().strftime("%H:%M:%S")
        sh = f"{int(sim_hour):02d}:{int((sim_hour % 1) * 60):02d}"
        print(
            f"[{ts}] sim={sh}  Solar {int(total_solar):5d} W | "
            f"Home {total_home:5d} W | P1 {int(net_grid):+6d} W"
        )
        for sim in simulators:
            print(sim.status())
        print()

        time.sleep(REPORT_INTERVAL)


if __name__ == "__main__":
    main()
