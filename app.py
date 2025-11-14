import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3
from threading import Thread, Event
import time, random, json, hashlib, os, datetime

DB_PATH = "pharmadb.sqlite"
LEDGER_PATH = "ledger.json"

app = FastAPI(title="PharmaChain Local (Simulated Blockchain)")

# Serve static dashboard
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Blockchain Ledger Simulation ---
def init_ledger():
    if not os.path.exists(LEDGER_PATH):
        genesis = {
            "index": 0,
            "timestamp": time.time(),
            "prev_hash": "0"*64,
            "data": {"genesis": True},
        }
        genesis["hash"] = hashlib.sha256(
            json.dumps(genesis["data"], sort_keys=True).encode()
        ).hexdigest()
        with open(LEDGER_PATH, "w") as f:
            json.dump([genesis], f, indent=2)

def add_block(event_type, payload):
    if not os.path.exists(LEDGER_PATH):
        init_ledger()
    with open(LEDGER_PATH, "r+") as f:
        chain = json.load(f)
        last = chain[-1]
        block = {
            "index": last["index"] + 1,
            "timestamp": time.time(),
            "prev_hash": last["hash"],
            "data": {"event": event_type, "payload": payload},
        }
        block["hash"] = hashlib.sha256(
            json.dumps(block["data"], sort_keys=True).encode()
            + str(block["timestamp"]).encode()
            + block["prev_hash"].encode()
        ).hexdigest()
        chain.append(block)
        f.seek(0)
        json.dump(chain, f, indent=2)
        f.truncate()
    return block

# --- SQLite Database ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id TEXT UNIQUE,
            batch_id TEXT,
            status TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id TEXT,
            ts TEXT,
            lat REAL,
            lon REAL,
            temp REAL,
            humidity REAL,
            pressure REAL,
            tamper INTEGER
        )
    """)
    conn.commit()
    conn.close()

def insert_shipment(shipment_id, batch_id, status="In Transit"):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = datetime.datetime.utcnow().isoformat()
    try:
        c.execute(
            "INSERT INTO shipments (shipment_id,batch_id,status,created_at) VALUES (?,?,?,?)",
            (shipment_id, batch_id, status, now),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()

def insert_telemetry(shipment_id, lat, lon, temp, humidity, pressure, tamper):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    ts = datetime.datetime.utcnow().isoformat()
    c.execute(
        "INSERT INTO telemetry (shipment_id,ts,lat,lon,temp,humidity,pressure,tamper) VALUES (?,?,?,?,?,?,?,?)",
        (shipment_id, ts, lat, lon, temp, humidity, pressure, 1 if tamper else 0),
    )
    conn.commit()
    conn.close()

def get_shipments():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT shipment_id,batch_id,status,created_at FROM shipments ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return [
        {"shipment_id": r[0], "batch_id": r[1], "status": r[2], "created_at": r[3]}
        for r in rows
    ]

def get_telemetry_latest(shipment_id, limit=100):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT ts,lat,lon,temp,humidity,pressure,tamper FROM telemetry WHERE shipment_id=? ORDER BY id DESC LIMIT ?",
        (shipment_id, limit),
    )
    rows = c.fetchall()
    conn.close()
    return [
        {
            "ts": r[0],
            "lat": r[1],
            "lon": r[2],
            "temp": r[3],
            "humidity": r[4],
            "pressure": r[5],
            "tamper": bool(r[6]),
        }
        for r in rows[::-1]
    ]

# --- Telemetry Simulation Thread ---
sim_thread = None
sim_stop_event = Event()

def simulation_loop(shipment_ids, interval=2.0):
    base_lat, base_lon = 12.9716, 77.5946
    while not sim_stop_event.is_set():
        for sid in shipment_ids:
            lat = base_lat + random.uniform(-0.02, 0.02)
            lon = base_lon + random.uniform(-0.02, 0.02)
            temp = round(20 + random.gauss(0, 1) + (0 if random.random() > 0.02 else random.uniform(6, 12)), 2)
            humidity = round(40 + random.gauss(0, 3), 2)
            pressure = round(1008 + random.gauss(0, 2), 2)
            tamper = random.random() < 0.01
            insert_telemetry(sid, lat, lon, temp, humidity, pressure, tamper)
            if tamper or temp < 2 or temp > 30:
                payload = {"shipment_id": sid, "temp": temp, "tamper": tamper, "ts": datetime.datetime.utcnow().isoformat()}
                add_block("ALERT", payload)
        time.sleep(interval)

# --- Pydantic models ---
class BatchRegister(BaseModel):
    batch_id: str
    manufacturer: str

class SimulateStart(BaseModel):
    shipment_count: int = 3
    interval_sec: float = 2.0

# --- API Routes ---
@app.on_event("startup")
def startup():
    init_db()
    init_ledger()
    for i in range(1, 4):
        insert_shipment(f"SHIP-100{i}", f"BATCH-00{i}")
        for j in range(6):
            insert_telemetry(f"SHIP-100{i}", 12.9716+random.random()*0.01, 77.5946+random.random()*0.01, 22+random.random(), 45+random.random()*3, 1013+random.random()*2, False)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/api/register_batch")
def register_batch(b: BatchRegister):
    block = add_block("REGISTER_BATCH", {"batch_id": b.batch_id, "manufacturer": b.manufacturer})
    return {"status": "ok", "block": block}

@app.get("/api/shipments")
def api_shipments():
    return {"shipments": get_shipments()}

@app.get("/api/telemetry/{shipment_id}")
def api_telemetry(shipment_id: str, limit: int = 100):
    return {"shipment_id": shipment_id, "data": get_telemetry_latest(shipment_id, limit)}

@app.get("/api/ledger")
def api_ledger(limit: int = 50):
    if not os.path.exists(LEDGER_PATH):
        return {"chain": []}
    with open(LEDGER_PATH) as f:
        chain = json.load(f)
    return {"chain": chain[-limit:]}

@app.post("/api/simulate/start")
def api_simulate_start(s: SimulateStart, background_tasks: BackgroundTasks):
    global sim_thread, sim_stop_event
    if sim_thread and sim_thread.is_alive():
        raise HTTPException(status_code=400, detail="Simulation already running")
    shipment_ids = [f"SHIP-SIM-{i}" for i in range(1, s.shipment_count + 1)]
    for sid in shipment_ids:
        insert_shipment(sid, f"BATCH-SIM-{sid[-1]}")
    sim_stop_event.clear()
    sim_thread = Thread(target=simulation_loop, args=(shipment_ids, s.interval_sec), daemon=True)
    sim_thread.start()
    return {"status": "started", "shipments": shipment_ids}

@app.post("/api/simulate/stop")
def api_simulate_stop():
    global sim_stop_event
    sim_stop_event.set()
    return {"status": "stopping"}

@app.get("/")
def index():
    return FileResponse("static/index.html")

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
