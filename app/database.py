import json
import logging

import aiosqlite
import os

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "/data/app.db")

# Fallback for local development
if not os.path.exists(os.path.dirname(DB_PATH)):
    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "app.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS allowlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT UNIQUE NOT NULL,
                label TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                content TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                direction TEXT NOT NULL DEFAULT 'incoming',
                wa_message_id TEXT DEFAULT '',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            INSERT OR IGNORE INTO settings (key, value) VALUES (
                'system_prompt',
                'You are a helpful AI assistant responding via WhatsApp/SMS for PP International School (PPIS), a CBSE affiliated Senior Secondary School in Pitampura, New Delhi. Keep your responses concise and friendly. Use simple formatting suitable for messaging apps. You are bilingual — you can understand and respond in both English and Hindi. If the parent writes in Hindi (Devanagari script or Hinglish/romanized Hindi), respond in Hindi. If they write in English, respond in English. Always be polite and helpful.'
            );

            CREATE TABLE IF NOT EXISTS forwarded_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_phone TEXT NOT NULL,
                teacher_name TEXT NOT NULL,
                teacher_grade TEXT NOT NULL,
                original_chat_id TEXT NOT NULL,
                sender_phone TEXT NOT NULL,
                original_message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_phone TEXT NOT NULL,
                reply_to TEXT NOT NULL,
                original_query TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pi_sheet_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                father_name TEXT DEFAULT '',
                mother_name TEXT DEFAULT '',
                father_mobile TEXT DEFAULT '',
                mother_mobile TEXT DEFAULT '',
                address TEXT DEFAULT '',
                transport TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS snapshot_access_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                father_mobile TEXT DEFAULT '',
                mother_mobile TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(student_name, grade)
            );

            CREATE TABLE IF NOT EXISTS leave_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_phone TEXT NOT NULL,
                child_name TEXT NOT NULL,
                grade TEXT DEFAULT '',
                leave_date TEXT NOT NULL,
                reason TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                teacher_name TEXT DEFAULT '',
                teacher_phone TEXT DEFAULT '',
                teacher_response TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS student_birthdays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL,
                dob TEXT NOT NULL,
                father_phone TEXT DEFAULT '',
                mother_phone TEXT DEFAULT '',
                last_wish_sent TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Campus Agent cloud config tables
            CREATE TABLE IF NOT EXISTS agent_dvrs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 80,
                username TEXT NOT NULL DEFAULT 'admin',
                password TEXT NOT NULL DEFAULT '',
                channels INTEGER NOT NULL DEFAULT 64
            );

            CREATE TABLE IF NOT EXISTS agent_camera_mapping (
                location TEXT PRIMARY KEY,
                dvr_index INTEGER NOT NULL,
                channel INTEGER NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                cam_type TEXT DEFAULT '',
                all_cameras TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attendance_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL DEFAULT '',
                camera_label TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'present',
                notification_sent INTEGER NOT NULL DEFAULT 0,
                parent_phones TEXT NOT NULL DEFAULT '',
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'attendance',
                student_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                wa_message_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS agent_registered_faces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                angle TEXT NOT NULL DEFAULT 'front',
                image_data BLOB NOT NULL,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS meal_monitoring_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grade TEXT NOT NULL,
                camera_key TEXT NOT NULL DEFAULT '',
                break_type TEXT NOT NULL DEFAULT '',
                capture_time TEXT NOT NULL DEFAULT '',
                parents_sent INTEGER NOT NULL DEFAULT 0,
                parents_failed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS homework_docs (
                grade TEXT PRIMARY KEY,
                doc_id TEXT NOT NULL,
                doc_url TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS homework_doc_state (
                grade TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL DEFAULT '',
                last_content TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS homework_delivery_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grade TEXT NOT NULL,
                period INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL DEFAULT '',
                parents_sent INTEGER NOT NULL DEFAULT 0,
                parents_failed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                delivery_time TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS school_holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL DEFAULT 'Holiday',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Manual review queue for low-confidence detections
            CREATE TABLE IF NOT EXISTS manual_review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                matched_name TEXT NOT NULL DEFAULT '',
                grade TEXT NOT NULL DEFAULT '',
                camera_label TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                snapshot_path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by TEXT DEFAULT NULL,
                reviewed_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Camera health status tracking
            CREATE TABLE IF NOT EXISTS camera_status_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_label TEXT NOT NULL,
                dvr_ip TEXT NOT NULL DEFAULT '',
                channel INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'online',
                error_code TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_success_at TIMESTAMP DEFAULT NULL,
                last_failure_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Notification delivery tracking with retry support
            CREATE TABLE IF NOT EXISTS notification_delivery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                student_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL,
                template_name TEXT NOT NULL DEFAULT 'ppis_attendance_alert',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TIMESTAMP DEFAULT NULL,
                delivered_at TIMESTAMP DEFAULT NULL,
                error_message TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Daily attendance summary reports
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                total_present INTEGER NOT NULL DEFAULT 0,
                total_absent INTEGER NOT NULL DEFAULT 0,
                total_teachers_present INTEGER NOT NULL DEFAULT 0,
                total_notifications_sent INTEGER NOT NULL DEFAULT 0,
                total_notifications_failed INTEGER NOT NULL DEFAULT 0,
                cameras_online INTEGER NOT NULL DEFAULT 0,
                cameras_offline INTEGER NOT NULL DEFAULT 0,
                manual_reviews_pending INTEGER NOT NULL DEFAULT 0,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Audit trail for data modifications (security)
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                table_name TEXT NOT NULL,
                record_id TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                performed_by TEXT NOT NULL DEFAULT 'system',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS summer_camp_students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                grade TEXT NOT NULL DEFAULT '',
                school_name TEXT NOT NULL DEFAULT '',
                parent_name TEXT NOT NULL DEFAULT '',
                contact_no TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                is_outsider INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── TrueFace attendance tracking ──────────────────────────
            CREATE TABLE IF NOT EXISTS trueface_teachers (
                pin TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trueface_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pin TEXT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                category TEXT DEFAULT 'staff',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trueface_attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pin TEXT NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                arrival_time TEXT,
                departure_time TEXT,
                arrival_whatsapp INTEGER DEFAULT 0,
                departure_whatsapp INTEGER DEFAULT 0,
                arrival_report_sent INTEGER DEFAULT 0,
                departure_report_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trueface_att_date
                ON trueface_attendance (date);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trueface_att_pin_date
                ON trueface_attendance (pin, date);

            -- ── Gate Head Count ──────────────────────────────────────
            CREATE TABLE IF NOT EXISTS gate_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                camera TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'IN',
                attire_color TEXT DEFAULT 'unknown',
                person_crop TEXT DEFAULT '',
                reconciled INTEGER DEFAULT 0,
                matched_pin TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_gate_entries_date
                ON gate_entries (date);
            CREATE INDEX IF NOT EXISTS idx_gate_entries_date_dir
                ON gate_entries (date, direction);

            CREATE TABLE IF NOT EXISTS cpplus_hourly_recounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hour_start TEXT NOT NULL,
                hour_end TEXT NOT NULL,
                in_count INTEGER NOT NULL,
                processed_frames INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'camera_recording',
                verified_at TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, hour_start)
            );

            CREATE INDEX IF NOT EXISTS idx_cpplus_recounts_date
                ON cpplus_hourly_recounts (date);

            CREATE TABLE IF NOT EXISTS vehicle_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                camera TEXT NOT NULL,
                direction TEXT NOT NULL DEFAULT 'IN',
                vehicle_type TEXT NOT NULL DEFAULT 'car',
                snapshot TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_vehicle_entries_date
                ON vehicle_entries (date);

            CREATE TABLE IF NOT EXISTS gate_daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                total_in INTEGER DEFAULT 0,
                total_out INTEGER DEFAULT 0,
                trueface_matched INTEGER DEFAULT 0,
                unreconciled INTEGER DEFAULT 0,
                report_sent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- ── Mood tracking (multi-person) ─────────────────────────
            CREATE TABLE IF NOT EXISTS chairman_mood_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person TEXT NOT NULL DEFAULT 'Chairman',
                date TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                camera TEXT NOT NULL,
                dominant_emotion TEXT NOT NULL,
                emotions_json TEXT DEFAULT '{}',
                temperament TEXT NOT NULL DEFAULT 'neutral',
                intensity REAL DEFAULT 0.0,
                face_distance REAL DEFAULT 0.0,
                face_confidence REAL DEFAULT 0.0,
                face_crop TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_chairman_mood_date
                ON chairman_mood_log (date);
            CREATE INDEX IF NOT EXISTS idx_chairman_mood_person
                ON chairman_mood_log (person);

            -- ── DVR Teacher Sightings (for head count reconciliation) ──
            CREATE TABLE IF NOT EXISTS teacher_dvr_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                person_id TEXT NOT NULL,
                name TEXT NOT NULL,
                camera TEXT NOT NULL,
                confidence REAL DEFAULT 0.0,
                outfit_color TEXT DEFAULT '',
                outfit_description TEXT DEFAULT '',
                outfit_colors_json TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_dvr_sightings_date
                ON teacher_dvr_sightings (date);
            CREATE INDEX IF NOT EXISTS idx_dvr_sightings_person
                ON teacher_dvr_sightings (date, person_id);

            -- ── DVR Visitor Sightings (unknown faces on gate/reception cameras) ──
            CREATE TABLE IF NOT EXISTS visitor_dvr_sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                camera TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_visitor_sightings_date
                ON visitor_dvr_sightings (date);

            -- ── Performance indexes ─────────────────────────────────
            CREATE INDEX IF NOT EXISTS idx_attendance_person_date
                ON attendance_records (person_id, date(logged_at));
            CREATE INDEX IF NOT EXISTS idx_attendance_logged_at
                ON attendance_records (logged_at);
            CREATE INDEX IF NOT EXISTS idx_attendance_grade
                ON attendance_records (grade);
            CREATE INDEX IF NOT EXISTS idx_messages_sender
                ON messages (sender);
            CREATE INDEX IF NOT EXISTS idx_messages_receiver
                ON messages (receiver);
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages (timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_direction
                ON messages (direction);
            CREATE INDEX IF NOT EXISTS idx_notification_log_phone
                ON notification_log (phone);
            CREATE INDEX IF NOT EXISTS idx_notification_log_created
                ON notification_log (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_processed_messages_created
                ON processed_messages (created_at);
            CREATE INDEX IF NOT EXISTS idx_pi_sheet_father_mobile
                ON pi_sheet_students (father_mobile);
            CREATE INDEX IF NOT EXISTS idx_pi_sheet_mother_mobile
                ON pi_sheet_students (mother_mobile);
            CREATE INDEX IF NOT EXISTS idx_pi_sheet_grade
                ON pi_sheet_students (grade);
            CREATE INDEX IF NOT EXISTS idx_faces_person_id
                ON agent_registered_faces (person_id);
            CREATE INDEX IF NOT EXISTS idx_leave_parent_phone
                ON leave_applications (parent_phone);
            CREATE INDEX IF NOT EXISTS idx_forwarded_teacher_phone
                ON forwarded_conversations (teacher_phone);
            CREATE INDEX IF NOT EXISTS idx_forwarded_sender_phone
                ON forwarded_conversations (sender_phone);
            CREATE INDEX IF NOT EXISTS idx_audit_log_created
                ON audit_log (created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_log_table
                ON audit_log (table_name, action);
            CREATE INDEX IF NOT EXISTS idx_manual_review_status
                ON manual_review_queue (status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_camera_status_label
                ON camera_status_log (camera_label);
            CREATE INDEX IF NOT EXISTS idx_notification_delivery_status
                ON notification_delivery (status, created_at);
            CREATE INDEX IF NOT EXISTS idx_daily_summary_date
                ON daily_summary (report_date);
            CREATE INDEX IF NOT EXISTS idx_summer_camp_contact
                ON summer_camp_students (contact_no);
            CREATE INDEX IF NOT EXISTS idx_summer_camp_name
                ON summer_camp_students (student_name);
        """)

        # ------------------------------------------------------------------
        # Schema migrations — add columns that may not exist in older DBs
        # ------------------------------------------------------------------
        try:
            await db.execute(
                "ALTER TABLE pi_sheet_students ADD COLUMN class_teacher TEXT DEFAULT ''"
            )
        except Exception:
            pass  # column already exists

        try:
            await db.execute(
                "ALTER TABLE messages ADD COLUMN wa_message_id TEXT DEFAULT ''"
            )
        except Exception:
            pass  # column already exists

        try:
            await db.execute(
                "ALTER TABLE trueface_contacts ADD COLUMN pin TEXT"
            )
        except Exception:
            pass  # column already exists

        try:
            await db.execute(
                "ALTER TABLE chairman_mood_log ADD COLUMN person TEXT NOT NULL DEFAULT 'Chairman'"
            )
        except Exception:
            pass  # column already exists

        for col_name, col_def in [
            ("outfit_color", "TEXT DEFAULT ''"),
            ("outfit_description", "TEXT DEFAULT ''"),
            ("outfit_colors_json", "TEXT DEFAULT '[]'"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE teacher_dvr_sightings ADD COLUMN {col_name} {col_def}"
                )
            except Exception:
                pass  # column already exists

        # Enhanced mood fields
        mood_new_cols = [
            ("mood_category", "TEXT DEFAULT 'Neutral'"),
            ("mood_label", "TEXT DEFAULT 'Normal'"),
            ("frame_count", "INTEGER DEFAULT 1"),
            ("agreement", "REAL DEFAULT 0.0"),
            ("negative_ratio", "REAL DEFAULT 0.0"),
            ("description", "TEXT DEFAULT ''"),
        ]
        for col_name, col_def in mood_new_cols:
            try:
                await db.execute(
                    f"ALTER TABLE chairman_mood_log ADD COLUMN {col_name} {col_def}"
                )
            except Exception:
                pass  # column already exists

        try:
            await db.execute(
                "ALTER TABLE vehicle_entries ADD COLUMN snapshot TEXT DEFAULT ''"
            )
        except Exception:
            pass  # column already exists

        # visitor_dvr_sightings: add classification + snapshot + direction columns
        for col_name, col_def in [
            ("classification", "TEXT DEFAULT 'Visitor'"),
            ("snapshot", "TEXT DEFAULT ''"),
            ("direction", "TEXT DEFAULT 'IN'"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE visitor_dvr_sightings ADD COLUMN {col_name} {col_def}"
                )
            except Exception:
                pass  # column already exists

        # teacher_dvr_sightings: add direction column for entry-exit pairing
        try:
            await db.execute(
                "ALTER TABLE teacher_dvr_sightings ADD COLUMN direction TEXT DEFAULT 'IN'"
            )
        except Exception:
            pass  # column already exists

        # Do NOT overwrite the system prompt — it is managed via the API
        await db.commit()

        # -----------------------------------------------------------------
        # Auto-seed agent DVRs & camera mappings when empty.
        # Fly.io's ephemeral filesystem wipes the DB on every restart/OOM.
        # This ensures the Campus Agent always gets valid config on startup.
        # -----------------------------------------------------------------
        try:
            from app.seed_data import SEED_DVRS, SEED_CAMERA_MAPPING
            logger.info(
                f"SEED DATA loaded: {len(SEED_DVRS)} DVRs, "
                f"{len(SEED_CAMERA_MAPPING)} camera mappings"
            )
        except Exception as e:
            logger.error(f"Failed to import seed_data: {e}", exc_info=True)
            SEED_DVRS = []
            SEED_CAMERA_MAPPING = {}

        # --- Auto-seed DVRs ---
        cursor = await db.execute("SELECT COUNT(*) FROM agent_dvrs")
        row = await cursor.fetchone()
        dvr_count = row[0] if row else 0
        logger.info(f"agent_dvrs count on startup: {dvr_count}")

        if SEED_DVRS:
            cursor2 = await db.execute("SELECT ip, password FROM agent_dvrs")
            existing = {r[0]: r[1] for r in await cursor2.fetchall()}
            missing = [d for d in SEED_DVRS if d.get("ip", "") not in existing]
            empty_pw = [
                d for d in SEED_DVRS
                if d.get("ip", "") in existing
                and not existing[d["ip"]]
                and d.get("password", "")
            ]
            try:
                if missing:
                    logger.info("Seeding %d missing DVR(s): %s",
                                len(missing), [d["ip"] for d in missing])
                    for dvr in missing:
                        await db.execute(
                            "INSERT INTO agent_dvrs (name, ip, port, username, password, channels) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                dvr.get("name", ""),
                                dvr.get("ip", ""),
                                dvr.get("port", 80),
                                dvr.get("username", "admin"),
                                dvr.get("password", ""),
                                dvr.get("channels", 64),
                            ),
                        )
                if empty_pw:
                    logger.info("Updating password for %d DVR(s) with empty passwords: %s",
                                len(empty_pw), [d["ip"] for d in empty_pw])
                    for dvr in empty_pw:
                        await db.execute(
                            "UPDATE agent_dvrs SET password = ? WHERE ip = ?",
                            (dvr["password"], dvr["ip"]),
                        )
                if missing or empty_pw:
                    await db.commit()
                    logger.info("DVR seed/update complete: %d inserted, %d updated",
                                len(missing), len(empty_pw))
            except Exception as e:
                logger.error(f"Auto-seed DVRs failed: {e}", exc_info=True)

        # --- Auto-seed camera mappings ---
        cursor = await db.execute("SELECT COUNT(*) FROM agent_camera_mapping")
        row = await cursor.fetchone()
        mapping_count = row[0] if row else 0
        logger.info(f"agent_camera_mapping count on startup: {mapping_count}")

        if SEED_CAMERA_MAPPING:
            cursor3 = await db.execute(
                "SELECT location FROM agent_camera_mapping"
            )
            existing_locations = {r[0] for r in await cursor3.fetchall()}
            missing_cams = {
                k: v for k, v in SEED_CAMERA_MAPPING.items()
                if k not in existing_locations
            }
            if missing_cams:
                logger.info(
                    "Seeding %d missing camera mapping(s)", len(missing_cams)
                )
                try:
                    seeded = 0
                    for location, data in missing_cams.items():
                        all_cameras = data.get("all_cameras")
                        await db.execute(
                            "INSERT OR REPLACE INTO agent_camera_mapping "
                            "(location, dvr_index, channel, description, cam_type, all_cameras) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                location,
                                data.get("dvr_index", 0),
                                data.get("channel", 1),
                                data.get("description", ""),
                                data.get("cam_type", ""),
                                json.dumps(all_cameras) if all_cameras else None,
                            ),
                        )
                        seeded += 1
                    await db.commit()
                    logger.info("Seeded %d camera mapping(s) OK", seeded)
                except Exception as e:
                    logger.error(
                        f"Auto-seed camera mappings failed: {e}", exc_info=True
                    )

        # --- Auto-seed homework docs ---
        try:
            from app.seed_data import SEED_HOMEWORK_DOCS
        except ImportError:
            SEED_HOMEWORK_DOCS = {}

        cursor = await db.execute("SELECT COUNT(*) FROM homework_docs")
        row = await cursor.fetchone()
        hw_count = row[0] if row else 0
        logger.info(f"homework_docs count on startup: {hw_count}")

        if hw_count == 0 and SEED_HOMEWORK_DOCS:
            logger.info(
                f"homework_docs table is empty — auto-seeding "
                f"{len(SEED_HOMEWORK_DOCS)} homework docs"
            )
            try:
                seeded = 0
                for grade, doc_info in SEED_HOMEWORK_DOCS.items():
                    await db.execute(
                        "INSERT OR REPLACE INTO homework_docs "
                        "(grade, doc_id, doc_url) VALUES (?, ?, ?)",
                        (grade, doc_info["doc_id"], doc_info.get("url", "")),
                    )
                    seeded += 1
                await db.commit()
                logger.info(f"Auto-seeded {seeded} homework docs")
            except Exception as e:
                logger.error(f"Auto-seed homework docs failed: {e}", exc_info=True)

    finally:
        await db.close()
