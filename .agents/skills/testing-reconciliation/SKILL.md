---
name: testing-reconciliation
description: Test the PPIS head count reconciliation system end-to-end. Use when verifying reconciliation report changes, live dashboard updates, or formula modifications.
---

# Testing the Head Count Reconciliation System

## Quick Start

```bash
# Start backend locally (minimal env vars needed for API testing)
cd /home/ubuntu/repos/whatsapp-bot-backend
WHATSAPP_CLOUD_TOKEN=test WHATSAPP_PHONE_ID=test REPORT_RECIPIENTS=test@test.com \
  poetry run uvicorn app.main:app --host 0.0.0.0 --port 8002
```

The server seeds test data automatically on startup (4 staff, 3 visitors, 10 gate entries).

## Key API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/gate/reconciliation/{date}` | GET | Full reconciliation JSON for a date (YYYY-MM-DD) |
| `/api/gate/live-data` | GET | Live dashboard JSON with spec-compliant fields |
| `/live` | GET | Live dashboard HTML page (browser) |
| `/api/gate/status` | GET | Quick today's head count totals |
| `/api/gate/send-report` | POST | Trigger email report (needs real email creds) |
| `/api/gate/vehicle-entry` | POST | Seed vehicle entries for testing |

## Key Formulas to Verify

These are the core formulas from the school spec:

```
Unrecognized Persons = Total Entries - Total Recognized
Current Occupancy = Total Entries - Total Exits
Total Recognized = Recognized Students + Recognized Staff
```

## Test Data Tables

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `gate_entries` | People entering/exiting gates | date, timestamp, camera, direction (IN/OUT), attire_color |
| `trueface_attendance` | Recognized staff (biometric) | pin, name, date, arrival_time, departure_time |
| `visitor_dvr_sightings` | Unknown faces on cameras | date, timestamp, camera, classification, direction |
| `vehicle_entries` | Vehicles (separate from head count) | date, timestamp, camera, direction, vehicle_type |
| `teacher_dvr_sightings` | Staff seen on DVR cameras | date, person_id, name, camera, timestamp |

## What to Test

### 1. Formula Verification (API)
```bash
curl -s http://localhost:8002/api/gate/reconciliation/YYYY-MM-DD | python3 -c "
import json, sys
d = json.load(sys.stdin)
te, tr, tu = d['total_entries'], d['total_recognized'], d['total_unrecognized']
co, tx = d['current_occupancy'], d['total_exits']
print(f'Unrecognized: {tu} == {te} - {tr} = {te-tr}: {tu == te-tr}')
print(f'Occupancy: {co} == {te} - {tx} = {te-tx}: {co == te-tx}')
"
```

### 2. U-### ID Format
All unrecognized persons must use U-001, U-002 format (not UNKNOWN-### or UNREC-###).
Check both `unknown_persons` and `unreconciled_gate_entries` arrays.

### 3. No Classification
Unrecognized persons must NOT have Parent/Vendor/Visitor labels.
The `classification` field should be absent from unknown/unreconciled entries.

### 4. Vehicle Separation
Vehicle entries must NOT inflate `total_entries`. Seed vehicles via POST and verify `total_entries` doesn't change.

### 5. Live Dashboard (Browser)
Open `http://localhost:8002/live` and verify:
- "Total Persons Entered" card with correct number
- "Current Occupancy" card with formula display
- "Unrecognized Persons" card showing formula (Entries - Recognized)
- "Vehicles" card separate from head count
- Unrecognized persons table with U-### IDs and "Unrecognized" status

### 6. Edge Cases
Test with a future date (no data) to verify no division-by-zero:
```bash
curl -s http://localhost:8002/api/gate/reconciliation/2099-01-01 | python3 -m json.tool
```

### 7. PDF Report (Code Inspection)
The PDF generation is at `app/routes/gate.py` in `_generate_reconciliation_pdf()`. Verify sections:
1. ENTRY SUMMARY
2. RECOGNITION SUMMARY  
3. UNRECOGNIZED SUMMARY
4. EXIT SUMMARY
5. CURRENT OCCUPANCY (with formula)
6. RECONCILIATION CHECK (with explanation text)
7. UNRECOGNIZED PERSON DETAILS
8. VEHICLE COUNT (labeled "Separate from Head Count")
9. STAFF RECONCILIATION DETAIL
10. AI CONCLUSION

### 8. Email Body (Code Inspection)
Email body is at `app/routes/gate.py` in `send_reconciliation_report()`. Verify it has all spec sections.

## Common Pitfalls

- **Port conflicts**: The server may fail to start if a previous instance is still running. Use `fuser -k PORT/tcp` to free the port.
- **Live dashboard API path**: The endpoint is `/api/gate/live-data` (not `/api/gate/live-dashboard`).
- **Email/PDF testing**: Cannot send real emails or generate PDFs without production credentials. Use code inspection for these.
- **Vehicle data**: Use `POST /api/gate/vehicle-entry` with JSON body to seed vehicle entries.
- **Date format**: API expects YYYY-MM-DD format for the date parameter.
- **Auto-refresh**: The live dashboard auto-refreshes every 30 seconds, so data may update while testing.

## Devin Secrets Needed

For full testing (including email reports):
- `REPORT_RECIPIENTS` — email addresses for report delivery
- `GOOGLE_EMAIL` — Gmail account for sending reports
- `GOOGLE_APP_PASSWORD` — Gmail app password
- `WHATSAPP_CLOUD_TOKEN` — Meta Cloud API token (for WhatsApp alerts)
- `WHATSAPP_PHONE_ID` — WhatsApp phone number ID

For API-only testing, dummy values work: `WHATSAPP_CLOUD_TOKEN=test WHATSAPP_PHONE_ID=test REPORT_RECIPIENTS=test@test.com`
