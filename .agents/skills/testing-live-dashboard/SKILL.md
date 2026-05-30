---
name: testing-live-dashboard
description: Test the live campus monitor dashboard at /live. Use when verifying dashboard UI, reconciliation data, camera feeds, or the Additional Information tab.
---

# Testing the Live Campus Dashboard

## Overview

The live dashboard at `/live` has two tabs:
1. **Overview** — reconciliation cards (Total Entered, Occupancy, Recognized, Unrecognized, Reconciliation Rate, Vehicles) + tables
2. **Additional Information** — live camera feeds from 6 DVRs + recognized staff photos + unrecognized person snapshots

## Prerequisites

### Start the backend server
```bash
cd /home/ubuntu/repos/whatsapp-bot-backend
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8002 &
```

Wait for startup, then verify:
```bash
curl -s --max-time 3 http://localhost:8002/live | head -5
```

### Seed test data

The database is `app.db` in the repo root (created by the server on startup). Seed data must match today's date.

**Important:** The server must be running before seeding — tables are created on first startup.

Seed recognized staff (requires both `trueface_attendance` and `agent_registered_faces` rows with matching names):
```python
import sqlite3, base64
from PIL import Image
import io

def make_jpeg(r, g, b):
    img = Image.new('RGB', (80, 80), (r, g, b))
    buf = io.BytesIO()
    img.save(buf, 'JPEG')
    return buf.getvalue()

db = sqlite3.connect('app.db')
today = '2026-05-30'  # Use current date

# Insert face photos for recognized staff
for pid, name, color in [('T1', 'Name1', (0,180,80)), ('T2', 'Name2', (50,50,220))]:
    db.execute(
        'INSERT OR REPLACE INTO agent_registered_faces (person_id, name, role, angle, image_data) VALUES (?, ?, ?, ?, ?)',
        (pid, name, 'Teacher', 'front', make_jpeg(*color))
    )

# Insert visitor snapshots
db.execute('UPDATE visitor_dvr_sightings SET snapshot = ? WHERE id = ?', (base64.b64encode(make_jpeg(220,50,50)).decode(), 4))
db.commit()
db.close()
```

### Important: Server restart after code changes

If you modify `gate.py` and need to test, **restart the server** — it caches the Python modules:
```bash
pkill -f "uvicorn app.main"; sleep 2
cd /home/ubuntu/repos/whatsapp-bot-backend && python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8002 &
```

## Testing the Additional Information Tab

### API endpoint
`GET /api/gate/live-snapshots` returns:
- `agent_online` (bool) — whether campus agent WebSocket is connected
- `camera_snapshots` (array of 6) — live camera frames (empty when agent offline)
- `recognized` (array) — staff with face photos from `agent_registered_faces`
- `unrecognized` (array) — visitors with U-### IDs from `visitor_dvr_sightings` + `gate_entries`

Verify API response:
```bash
curl -s http://localhost:8002/api/gate/live-snapshots | python3 -c "import json,sys; d=json.load(sys.stdin); print('agent_online:', d.get('agent_online')); print('recognized:', d['total_recognized']); print('unrecognized:', d['total_unrecognized'])"
```

### Browser tests
1. Open `http://localhost:8002/live`
2. Verify Overview tab is active with dashboard cards
3. Click "Additional Information" tab
4. Verify agent offline banner (red) when campus agent not connected
5. Verify 6 camera cards: Entry Gate 1, Entry Gate 2, Basement Main Gate, Reception C1, Reception C2, Dispersal Exit
6. Verify recognized staff photos with names and arrival times
7. Verify unrecognized persons with U-### IDs, timestamps, camera names, IN badges
8. Click "Refresh Snapshots" — should re-fetch without errors
9. Switch back to Overview — original data intact

## Testing the Overview Tab (Reconciliation)

Key formulas to verify:
- Unrecognized = Total Entries - Total Recognized
- Occupancy = Entries - Exits
- Reconciliation Rate = Recognized / Entries * 100

API endpoint: `GET /api/gate/live-data`

## Known Quirks

- Campus agent is never connected locally — all camera feeds show "Agent not connected". This is expected.
- The `agent_online` field requires the server to be running the latest code. If it returns `null`/missing, restart the server.
- Recognized staff requires BOTH `trueface_attendance` (today's date) AND `agent_registered_faces` (with `angle='front'`) rows with matching `name` fields.
- PIL (Pillow) is needed for generating test images. If not available, use raw JPEG bytes.

## Devin Secrets Needed

No secrets needed for local testing. Production dashboard at https://ppis-whatsapp-bot.fly.dev/live uses the Fly.io deployment.
