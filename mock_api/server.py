#!/usr/bin/env python3
"""Mock Zendure cloud API server.

Returns a fake device list so Home Assistant can be configured without
a real Zendure account or physical devices.

Device definitions must match simulator/simulate.py (same device_id, prod_key, sn).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "5000"))

# The MQTT broker reachable from inside the Docker network.
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# ZenSDK HTTP server: where HA (inside Docker) can reach the simulator's HTTP server.
# HA uses this IP (+ optional port) to poll http://{ip}/properties/report.
# Format: "host:port" — "simulator" is the Docker service name when using docker-compose.
SIMULATOR_HOST = os.environ.get("SIMULATOR_HOST", "simulator")
SIMULATOR_HTTP_PORT = int(os.environ.get("SIMULATOR_HTTP_PORT", "8088"))
# Combined "ip" field: ZendureZenSdk builds URLs as http://{ipAddress}/{endpoint}
SF_IP_ADDRESS = f"{SIMULATOR_HOST}:{SIMULATOR_HTTP_PORT}"

# ──────────────────────────────────────────────────────────────────────────────
# Fake device list
# productModel MUST match (case-insensitive) a key in Api.createdevice (api.py).
# Note the trailing space in "SolarFlow 2400 AC+ " — it is intentional.
# ──────────────────────────────────────────────────────────────────────────────
DEVICE_LIST = [
    {
        "deviceKey": "HYPER2000SIM01",
        "productModel": "Hyper 2000",
        "productKey": "simkey001",
        "deviceName": "Hyper 2000 Sim A",
        "snNumber": "SIM2000001",
    },
    {
        "deviceKey": "HYPER2000SIM02",
        "productModel": "Hyper 2000",
        "productKey": "simkey002",
        "deviceName": "Hyper 2000 Sim B",
        "snNumber": "SIM2000002",
    },
    {
        # ZendureZenSdk device: HA polls http://{ip}/properties/report for ZenSDK mode.
        # The "ip" field is used by ZendureZenSdk.httpGet() to build the URL.
        "deviceKey": "SF2400ACSIM01",
        "productModel": "SolarFlow 2400 AC+",
        "productKey": "simkey003",
        "deviceName": "SolarFlow 2400AC+ Sim",
        "snNumber": "SIMSF2400001",
        "ip": SF_IP_ADDRESS,
    },
]

RESPONSE = {
    "code": 200,
    "success": True,
    "msg": "Simulator mock response",
    "data": {
        "deviceList": DEVICE_LIST,
        "mqtt": {
            # HA (inside Docker) connects to mosquitto by service name.
            "clientId": "ha-sim-cloud-client",
            "url": f"{MQTT_HOST}:{MQTT_PORT}",
            "username": MQTT_USER,
            "password": MQTT_PASS,
        },
    },
}


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        # Drain the request body
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

        body = json.dumps(RESPONSE).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self.do_POST()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[MockAPI] {fmt % args}")


def main() -> None:
    # Print the token so the user can copy-paste it into the HA UI.
    import base64

    token_bytes = f"http://mock-api:{PORT}.SIMAPPKEY".encode()
    token = base64.b64encode(token_bytes).decode()
    print("=" * 60)
    print("Mock Zendure API started.")
    print(f"Listening on http://0.0.0.0:{PORT}")
    print()
    print("Use this token when configuring the Zendure HA integration:")
    print(f"  {token}")
    print("=" * 60)

    server = HTTPServer((HOST, PORT), MockHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
