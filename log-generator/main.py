import os
import random
import time
from datetime import datetime, timedelta, timezone

from elasticsearch import Elasticsearch, helpers
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Log Generator")

ES_HOST = os.getenv("ES_HOST", "http://localhost:9201")
ES_INDEX = os.getenv("ES_INDEX", "app-logs")

es = Elasticsearch(ES_HOST)

# --- Config for realistic log generation ---

SERVICES = ["auth-service", "api-gateway", "order-service", "payment-service", "user-service"]
ENDPOINTS = ["/api/login", "/api/users", "/api/orders", "/api/payments", "/api/health", "/api/products"]
TENANTS = ["acme-corp", "globex", "initech", "umbrella", "wayne-ent"]
STATUS_CODES = [200, 200, 200, 200, 200, 201, 204, 301, 400, 401, 403, 404, 500, 502, 503]
LEVELS = ["INFO", "INFO", "INFO", "INFO", "WARN", "ERROR"]

last_batch: dict = {}


class GenerateRequest(BaseModel):
    count: int = 1000


def generate_log_entry(ts: datetime) -> dict:
    status = random.choice(STATUS_CODES)
    level = "ERROR" if status >= 500 else ("WARN" if status >= 400 else random.choice(LEVELS))
    base_latency = random.uniform(5, 50)
    latency = base_latency * (random.uniform(3, 20) if status >= 500 else 1)

    return {
        "timestamp": ts.isoformat(),
        "service": random.choice(SERVICES),
        "endpoint": random.choice(ENDPOINTS),
        "status_code": status,
        "response_time_ms": round(latency, 2),
        "tenant": random.choice(TENANTS),
        "level": level,
        "message": f"{level}: {random.choice(ENDPOINTS)} responded {status}",
    }


def ensure_index():
    if not es.indices.exists(index=ES_INDEX):
        es.indices.create(
            index=ES_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "timestamp": {"type": "date"},
                        "service": {"type": "keyword"},
                        "endpoint": {"type": "keyword"},
                        "status_code": {"type": "integer"},
                        "response_time_ms": {"type": "float"},
                        "tenant": {"type": "keyword"},
                        "level": {"type": "keyword"},
                        "message": {"type": "text"},
                    }
                }
            },
        )


@app.post("/generate")
def generate_logs(req: GenerateRequest):
    global last_batch
    start = time.time()

    ensure_index()

    # Spread logs across the last 24 hours for realistic dashboards
    now = datetime.now(timezone.utc)
    actions = []
    for _ in range(req.count):
        ts = now - timedelta(seconds=random.randint(0, 86400))
        doc = generate_log_entry(ts)
        actions.append({"_index": ES_INDEX, "_source": doc})

    success, errors = helpers.bulk(es, actions, raise_on_error=False)
    duration = round(time.time() - start, 2)

    last_batch = {
        "count_requested": req.count,
        "count_ingested": success,
        "errors": len(errors) if isinstance(errors, list) else 0,
        "duration_seconds": duration,
        "index": ES_INDEX,
        "generated_at": now.isoformat(),
    }
    return last_batch


@app.get("/status")
def get_status():
    if not last_batch:
        return {"message": "No batches sent yet"}
    return last_batch


@app.get("/", response_class=HTMLResponse)
def ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Log Generator</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e1e4e8; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
  .card { background: #1c1f26; border-radius: 12px; padding: 32px; width: 420px; box-shadow: 0 4px 24px rgba(0,0,0,0.4); }
  h1 { font-size: 20px; margin-bottom: 24px; color: #58a6ff; }
  label { display: block; font-size: 13px; color: #8b949e; margin-bottom: 6px; }
  input { width: 100%; padding: 10px 12px; border-radius: 6px; border: 1px solid #30363d; background: #0d1117; color: #e1e4e8; font-size: 16px; margin-bottom: 16px; }
  input:focus { outline: none; border-color: #58a6ff; }
  button { width: 100%; padding: 12px; border-radius: 6px; border: none; background: #238636; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; }
  button:hover { background: #2ea043; }
  button:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  .status { margin-top: 20px; padding: 16px; border-radius: 8px; background: #161b22; font-size: 13px; line-height: 1.7; }
  .status .label { color: #8b949e; }
  .status .value { color: #e1e4e8; font-weight: 500; }
  .status .highlight { color: #3fb950; font-weight: 700; }
  .error { color: #f85149; }
</style>
</head>
<body>
<div class="card">
  <h1>Log Generator</h1>
  <label for="count">Batch size (number of logs)</label>
  <input type="number" id="count" value="1000" min="1" max="100000">
  <button id="btn" onclick="send()">Send Batch</button>
  <div class="status" id="status">No batches sent yet.</div>
</div>
<script>
async function send() {
  const btn = document.getElementById('btn');
  const status = document.getElementById('status');
  const count = parseInt(document.getElementById('count').value);
  if (!count || count < 1) { status.innerHTML = '<span class="error">Enter a valid count.</span>'; return; }
  btn.disabled = true;
  btn.textContent = 'Sending...';
  status.innerHTML = 'Generating ' + count.toLocaleString() + ' logs...';
  try {
    const res = await fetch('/generate', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({count}) });
    const data = await res.json();
    status.innerHTML =
      '<span class="label">Ingested:</span> <span class="highlight">' + data.count_ingested.toLocaleString() + '</span> logs<br>' +
      '<span class="label">Errors:</span> <span class="value">' + data.errors + '</span><br>' +
      '<span class="label">Duration:</span> <span class="value">' + data.duration_seconds + 's</span><br>' +
      '<span class="label">Index:</span> <span class="value">' + data.index + '</span><br>' +
      '<span class="label">Time:</span> <span class="value">' + new Date(data.generated_at).toLocaleString() + '</span>';
  } catch (e) {
    status.innerHTML = '<span class="error">Failed: ' + e.message + '</span>';
  }
  btn.disabled = false;
  btn.textContent = 'Send Batch';
}
</script>
</body>
</html>"""
