import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

import aiosqlite

from app.routes import gate


class C1GateIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    def _signal(self, event_id="signal-1", **overrides):
        signal = {
            "event_id": event_id,
            "timestamp": "2026-07-23 09:15:00",
            "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
            "event_type": "congestion_started",
            "severity": "warning",
            "verification_only": True,
            "metadata": {"people": 7, "threshold": 6},
        }
        signal.update(overrides)
        return signal

    def test_signal_validation_rejects_identity_and_biometric_data(self):
        for metadata in (
            {"name": "Person"},
            {"person_crop": "base64"},
            {"face_embedding": [0.1]},
            {"pin": "123"},
        ):
            with self.assertRaises(ValueError):
                gate._normalize_c1_intelligence_event(self._signal(metadata=metadata))

    def test_signal_validation_requires_non_additive_semantics(self):
        with self.assertRaises(ValueError):
            gate._normalize_c1_intelligence_event(self._signal(verification_only=False))

    async def test_signal_storage_is_idempotent_by_event_id(self):
        db = await aiosqlite.connect(":memory:")
        try:
            await db.executescript(
                """
                CREATE TABLE c1_intelligence_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    camera TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    verification_only INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            signal = gate._normalize_c1_intelligence_event(self._signal())
            self.assertEqual(
                await gate._store_c1_intelligence_events(db, [signal]),
                1,
            )
            self.assertEqual(
                await gate._store_c1_intelligence_events(db, [signal]),
                0,
            )
            cursor = await db.execute("SELECT COUNT(*) FROM c1_intelligence_events")
            self.assertEqual((await cursor.fetchone())[0], 1)
        finally:
            await db.close()

    async def test_recipient_delivery_claim_is_persistent(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            db = await aiosqlite.connect(path)
            await db.execute(
                "CREATE TABLE c1_alert_deliveries ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_id TEXT NOT NULL, recipient TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'claimed', "
                "claimed_at TEXT NOT NULL, sent_at TEXT NOT NULL DEFAULT '', "
                "UNIQUE(event_id, recipient))"
            )
            await db.commit()
            await db.close()

            async def connect():
                return await aiosqlite.connect(path)

            with patch.object(gate, "_get_db", AsyncMock(side_effect=connect)):
                self.assertTrue(
                    await gate._claim_c1_alert(
                        "signal-1", "919999999999", "23-07-2026 09:15:00 IST"
                    )
                )
                await gate._finish_c1_alert(
                    "signal-1",
                    "919999999999",
                    True,
                    "23-07-2026 09:15:01 IST",
                )
                self.assertFalse(
                    await gate._claim_c1_alert(
                        "signal-1", "919999999999", "23-07-2026 09:16:00 IST"
                    )
                )
        finally:
            os.unlink(path)

    def test_report_keeps_signals_and_vehicles_non_additive(self):
        entries = [
            {
                "event_id": "person-1",
                "timestamp": "2026-07-23 09:10:00",
                "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
                "direction": "IN",
            }
        ]
        report = gate._build_event_id_headcount_report(
            entries,
            datetime(2026, 7, 23, 10, 0, tzinfo=gate.IST),
        )
        vehicles = [
            {
                "event_id": "vehicle-1",
                "timestamp": "2026-07-23 09:20:00",
                "camera": "ENTRY GATE-OUTSIDE (CP Plus)",
                "direction": "IN",
                "vehicle_type": "bus",
                "dwell_seconds": 25,
            }
        ]
        signals = [gate._normalize_c1_intelligence_event(self._signal())]
        enriched = gate._enrich_event_id_headcount_report(
            report,
            vehicles,
            signals,
        )

        self.assertEqual(enriched["total_in"], 1)
        self.assertEqual(enriched["total_out"], 0)
        self.assertEqual(enriched["total_vehicles"]["in"], 1)
        self.assertEqual(enriched["total_signals"]["congestion_started"], 1)
        self.assertEqual(enriched["replay_discrepancies"], 0)
        self.assertTrue(
            gate._generate_event_id_headcount_pdf(enriched).startswith(b"%PDF")
        )

    def test_replay_discrepancy_is_reported_without_changing_official_total(self):
        report = gate._build_event_id_headcount_report(
            [],
            datetime(2026, 7, 23, 10, 0, tzinfo=gate.IST),
        )
        discrepancy = gate._normalize_c1_intelligence_event(
            self._signal(
                event_id="replay-1",
                event_type="replay_discrepancy",
                metadata={
                    "live_in": 2,
                    "replay_in": 3,
                    "difference": 1,
                    "official_count_changed": False,
                },
            )
        )
        enriched = gate._enrich_event_id_headcount_report(
            report,
            [],
            [discrepancy],
        )
        self.assertEqual(enriched["total_in"], 0)
        self.assertEqual(enriched["replay_discrepancies"], 1)
        self.assertIn("official total unchanged", enriched["replay_status"])


if __name__ == "__main__":
    unittest.main()
