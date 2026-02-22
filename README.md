# Zendure Simulator

A fully self-contained Docker stack to test the [Zendure-HA](https://github.com/FireFly177/Zendure-HA) Home Assistant integration **without any real Zendure hardware or cloud account**.

The stack simulates two Hyper 2000 and one SolarFlow 2400 AC+, a solar field, home consumption, and a grid meter (P1). Home Assistant runs inside the stack with the integration mounted directly from the source repo.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Docker network                        │
│                                                          │
│  ┌─────────────┐   MQTT :1883 (cloud)   ┌────────────┐  │
│  │  simulator  │ ─────────────────────► │            │  │
│  │  simulate.py│   MQTT :1884 (local)   │ mosquitto  │  │
│  │             │ ─────────────────────► │            │  │
│  │  HTTP :8088 │ ◄────────────────────  │            │  │
│  │  (ZenSDK +  │   subscribe commands   └─────┬──────┘  │
│  │   Web UI)   │                              │         │
│  └──────┬──────┘              MQTT subscribe  │         │
│         │ REST (P1 push)               ┌──────▼──────┐  │
│         └────────────────────────────► │             │  │
│                                        │ homeassist. │  │
│  ┌─────────────┐   HTTP (device list)  │  :8123      │  │
│  │  mock-api   │ ◄─────────────────────│             │  │
│  │  :5000      │                       └─────────────┘  │
│  └─────────────┘                                        │
└──────────────────────────────────────────────────────────┘
```

### Simulated devices

| Device | Connection mode | MQTT broker | HA status |
|---|---|---|---|
| Hyper 2000 A | Local MQTT | port 1884 | 11 (local) |
| Hyper 2000 B | Local MQTT | port 1884 | 11 (local) |
| SolarFlow 2400 AC+ | Cloud MQTT + ZenSDK HTTP | port 1883 + :8088 | 12 (ZenSDK) |

The **Hyper 2000** devices publish on the local MQTT broker (port 1884). When HA receives a message on that broker, the integration automatically marks the device as locally connected (status 11).

The **SolarFlow 2400 AC+** publishes on the cloud MQTT broker (port 1883) and additionally exposes a ZenSDK-compatible HTTP endpoint on port 8088. Toggling "connection → ZenSDK" in the HA device page switches HA to poll `GET http://simulator:8088/properties/report` instead (status 12).

### Mock cloud API

`mock-api` is a minimal HTTP server that responds to the Zendure cloud API calls made by HA during integration setup. It returns the fake device list and points HA to the local MQTT broker. The integration is never aware it is talking to a simulator.

---

## Prerequisites

- Docker + Docker Compose v2
- The [Zendure-HA](https://github.com/Zendure/Zendure-HA) repository cloned **inside** this directory:

```bash
git clone https://github.com/Zendure/Zendure-HA.git
```

The `docker-compose.yml` mounts `./Zendure-HA/custom_components/zendure_ha` directly into the HA container, so any local change to the integration is picked up on restart without rebuilding.

---

## Quick start

### 1 — Start the stack

```bash
docker compose up -d
```

All four services start: `mosquitto`, `simulator`, `mock-api`, `homeassistant`.

### 2 — Retrieve the integration token

```bash
docker compose logs mock-api
```

Look for a line like:

```
Use this token when configuring the Zendure HA integration:
  aabbCCdd112233
```

Copy the token (base64 string). It encodes the internal API URL used by the integration.

### 3 — Open Home Assistant

Navigate to [http://localhost:8123](http://localhost:8123) and complete the onboarding (create a local account).

### 4 — Install and configure the Zendure-HA integration

1. **Settings → Integrations → Add integration** → search for **Zendure HA**.
2. Paste the token from step 2.
3. The integration will discover the three simulated devices automatically.

### 5 — Configure the local MQTT server

In the Zendure-HA integration options, set the **local MQTT server** to:

```
mosquitto:1884
```

This allows the Hyper 2000 devices to be recognised as locally connected (status 11).

### 6 — Enable P1 push (optional)

The simulator can update the `input_number.p1_power` entity in HA so the Zendure manager has a real grid meter value to react to.

Generate a **Long-Lived Access Token** in HA (Profile → Security) and pass it to the stack:

```bash
HA_TOKEN=<your_token> docker compose up -d
```

Or add it to a `.env` file at the root:

```dotenv
HA_TOKEN=<your_token>
```

In the Zendure integration, select `input_number.p1_power` as the P1 source.

---

## Web UI

The simulator exposes a control panel at **[http://localhost:8088](http://localhost:8088)**.

| Section | Description |
|---|---|
| **Live Status** | Simulated time, solar production, home load, P1 grid power |
| **Devices** | Per-device SOC, mode (off / charge / discharge), power |
| **Simulated Hour** | Fix the time of day or let it follow the real clock |
| **Solar Peak Power** | Adjust the peak of the sinusoidal solar curve |
| **Home Load** | Fix home consumption or let it follow the hourly profile |
| **Battery SOC** | Set the state of charge of any device instantly (one-shot) |

Controls take effect within the next report cycle (default 5 s). SOC overrides are applied once and then the simulation resumes from the new value.

---

## Environment variables

All variables have sensible defaults; none are required for basic use.

### Simulator (`simulator` service)

| Variable | Default | Description |
|---|---|---|
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | Cloud MQTT port |
| `LOCAL_MQTT_PORT` | `1884` | Local MQTT port (0 = disabled) |
| `HTTP_PORT` | `8088` | ZenSDK HTTP + Web UI port (0 = disabled) |
| `SOLAR_PEAK_W` | `5000` | Peak solar production in watts |
| `REPORT_INTERVAL` | `5` | Seconds between MQTT property reports |
| `HA_URL` | `http://localhost:8123` | Home Assistant base URL |
| `HA_TOKEN` | *(empty)* | Long-lived access token for P1 push |
| `VERBOSE` | `0` | Log every MQTT message received (1 = enabled) |

### Mock API (`mock-api` service)

| Variable | Default | Description |
|---|---|---|
| `SIMULATOR_HOST` | `simulator` | Hostname/IP of the simulator, returned to HA as the ZenSDK address |
| `SIMULATOR_HTTP_PORT` | `8088` | ZenSDK HTTP port, returned alongside the host |

---

## Exposed ports

| Port | Service | Purpose |
|---|---|---|
| `1883` | mosquitto | Cloud MQTT (simulator + HA) |
| `1884` | mosquitto | Local MQTT (Hyper 2000 + HA) |
| `9001` | mosquitto | WebSocket MQTT (optional, browser debugging) |
| `8088` | simulator | ZenSDK HTTP API + Web UI |
| `8123` | homeassistant | Home Assistant UI |
| `10001` | mock-api | Mock cloud API (host access for debugging) |

---

## Project structure

```
.
├── simulate.py              # Device simulator (MQTT + ZenSDK HTTP + Web UI)
├── Dockerfile               # Image for the simulator service
├── requirements.txt         # Python deps (paho-mqtt)
│
├── mock_api/
│   ├── server.py            # Fake Zendure cloud API
│   └── Dockerfile
│
├── mosquitto/
│   └── mosquitto.conf       # Dual-port MQTT broker config
│
├── homeassistant/
│   └── configuration.yaml   # HA config (P1 input_number, logging)
│
├── docker-compose.yml
└── Zendure-HA/              # Cloned separately — not tracked by git
    └── custom_components/
        └── zendure_ha/      # Mounted read-only into the HA container
```

---

## Troubleshooting

**Devices appear offline in HA**
- Check that the integration token was copied correctly from `docker compose logs mock-api`.
- Verify that MQTT messages arrive: `docker compose logs simulator | grep publish` or subscribe with `mosquitto_sub -h localhost -p 1883 -t '#' -v`.

**Hyper 2000 stays on cloud MQTT (status ≠ 11)**
- Confirm the local MQTT server is set to `mosquitto:1884` in the integration options, not the cloud port.

**SolarFlow 2400 AC+ not switching to ZenSDK (status ≠ 12)**
- In the HA device page, toggle **connection → ZenSDK**. HA will then poll `http://simulator:8088/properties/report`.

**P1 push fails**
- The `HA_TOKEN` must be a valid Long-Lived Access Token. Regenerate one in HA under **Profile → Security → Long-Lived Access Tokens**.
- The `input_number.p1_power` entity must exist — it is defined in `homeassistant/configuration.yaml`.

**Web UI not loading**
- The Web UI is served by the ZenSDK HTTP server. It only starts if `HTTP_PORT > 0` (default: 8088). Check `docker compose logs simulator`.
