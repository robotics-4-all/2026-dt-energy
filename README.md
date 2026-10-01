# Digital Twin — Smart Energy Grid Generator

A tool that automatically generates a complete digital twin for **any**
energy grid described in a `.seg` file.
Each `.seg` file produces its own independent project folder, with
real-time telemetry simulation (voltage, frequency, load, etc.), transport
via MQTT/Kafka, storage in a database, and visualization in dashboards.


## How it works

```
Edge Simulator  →  MQTT  →  Kafka Connect  →  Kafka  →  Consumers  →  Databases  →  Grafana
(generates data)    (message transport)              (processing)     (storage)     (charts)
```
Everything runs automatically inside Docker containers — no need to set
them up one by one.

## Step 1 — Generate the project from your own `.seg` file

```bash
python generate_twin.py path/to/model.seg
```

This creates a new folder `<file_name>_twin/` with all the necessary files
(simulator, consumers, docker-compose, etc.), tailored to the grid
described by your specific `.seg` model. Each `.seg` file produces a
separate, independent project folder.

Optionally, with `-v` you can see in detail where each noise/distribution
setting came from for each grid element (instance/class/global/default):
```bash
python generate_twin.py path/to/model.seg -v
```

## Step 2 — Enter the generated folder and bring up the containers

```bash
cd <file_name>_twin
docker compose up -d --build
```
### Watching live logs

You have two options for watching telemetry and anomaly logs live, once the
containers are up:

**Option A — one command, two windows (Windows only):**
After the first `docker compose up -d --build`, you can instead run:
```powershell
.\start_monitor.ps1
```
This brings everything up *and* automatically opens two separate windows
with live logs (telemetry + anomalies), without needing to type commands
manually every time.

**Option B — two manual terminals (any OS):**
Open two separate terminal windows in the project folder and run one
command in each:
```bash
# Terminal 1 — telemetry (edge simulator)
docker compose logs -f --tail 0 edge-simulator

# Terminal 2 — anomaly detection (analytics consumer)
docker compose logs -f --tail 0 analytics-consumer
```

## How to check that it's working

See if all containers are running:
```bash
docker compose ps -a
```

See live logs from the simulator:
```bash
docker compose logs -f edge-simulator
```

## How to view the charts (Grafana)

1. Open **http://localhost:3000**
2. Login: **admin / admin**

