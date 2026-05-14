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

            -- ============================================================
            -- Two-Way Relay Messaging System tables
            -- ============================================================

            -- Core relay messages: every message in the relay system
            CREATE TABLE IF NOT EXISTS relay_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL DEFAULT '',
                sender_phone TEXT NOT NULL,
                sender_role TEXT NOT NULL DEFAULT 'parent',
                receiver_phone TEXT NOT NULL,
                receiver_role TEXT NOT NULL DEFAULT 'teacher',
                direction TEXT NOT NULL DEFAULT 'parent_to_teacher',
                message_text TEXT NOT NULL DEFAULT '',
                message_type TEXT NOT NULL DEFAULT 'text',
                grade TEXT NOT NULL DEFAULT '',
                student_name TEXT NOT NULL DEFAULT '',
                delivery_status TEXT NOT NULL DEFAULT 'pending',
                wa_message_id TEXT DEFAULT '',
                email_sent INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP DEFAULT NULL,
                read_at TIMESTAMP DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_relay_msg_sender ON relay_messages(sender_phone);
            CREATE INDEX IF NOT EXISTS idx_relay_msg_receiver ON relay_messages(receiver_phone);
            CREATE INDEX IF NOT EXISTS idx_relay_msg_conv ON relay_messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_relay_msg_grade ON relay_messages(grade);
            CREATE INDEX IF NOT EXISTS idx_relay_msg_status ON relay_messages(delivery_status);
            CREATE INDEX IF NOT EXISTS idx_relay_msg_direction ON relay_messages(direction);

            -- Relay attachments: files sent in relay messages
            CREATE TABLE IF NOT EXISTS relay_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relay_message_id INTEGER NOT NULL,
                file_type TEXT NOT NULL DEFAULT 'document',
                file_name TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
                file_size INTEGER NOT NULL DEFAULT 0,
                storage_path TEXT NOT NULL DEFAULT '',
                cloud_media_id TEXT NOT NULL DEFAULT '',
                validation_status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (relay_message_id) REFERENCES relay_messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_att_msg ON relay_attachments(relay_message_id);

            -- Message queue: messages waiting to be sent (retry, offline, etc.)
            CREATE TABLE IF NOT EXISTS relay_message_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                relay_message_id INTEGER DEFAULT NULL,
                recipient_phone TEXT NOT NULL,
                recipient_role TEXT NOT NULL DEFAULT 'parent',
                message_text TEXT NOT NULL DEFAULT '',
                media_info TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT 'whatsapp',
                priority INTEGER NOT NULL DEFAULT 5,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                last_error TEXT NOT NULL DEFAULT '',
                next_retry_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP DEFAULT NULL,
                FOREIGN KEY (relay_message_id) REFERENCES relay_messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_relay_queue_status ON relay_message_queue(status);
            CREATE INDEX IF NOT EXISTS idx_relay_queue_retry ON relay_message_queue(next_retry_at);

            -- Audit log: all communication events
            CREATE TABLE IF NOT EXISTS relay_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                actor_phone TEXT NOT NULL DEFAULT '',
                actor_role TEXT NOT NULL DEFAULT '',
                target_phone TEXT NOT NULL DEFAULT '',
                grade TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                relay_message_id INTEGER DEFAULT NULL,
                ip_address TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_relay_audit_type ON relay_audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_relay_audit_actor ON relay_audit_log(actor_phone);
            CREATE INDEX IF NOT EXISTS idx_relay_audit_time ON relay_audit_log(created_at);

            -- Teacher-class permissions: which teachers can message which classes
            CREATE TABLE IF NOT EXISTS teacher_class_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_phone TEXT NOT NULL,
                teacher_name TEXT NOT NULL DEFAULT '',
                grade TEXT NOT NULL,
                permission_type TEXT NOT NULL DEFAULT 'class_teacher',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_tcp_unique
                ON teacher_class_permissions(teacher_phone, grade);

            -- Blocked file types configuration
            CREATE TABLE IF NOT EXISTS relay_blocked_file_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                extension TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL DEFAULT 'Security risk',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
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

        # Do NOT overwrite the system prompt — it is managed via the API
        await db.commit()

        # ------------------------------------------------------------------
        # Auto-seed teacher-class permissions from TEACHER_DATA
        # ------------------------------------------------------------------
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM teacher_class_permissions")
            row = await cursor.fetchone()
            tcp_count = row[0] if row else 0
            if tcp_count == 0:
                try:
                    from app.services.openai_service import TEACHER_DATA as _TD
                    for entry in _TD:
                        t_phone = entry.get("whatsapp", "")
                        t_name = entry.get("teacher", "").split("/")[0].strip()
                        t_grade = entry.get("grade", "")
                        if t_phone and t_grade:
                            await db.execute(
                                "INSERT OR IGNORE INTO teacher_class_permissions "
                                "(teacher_phone, teacher_name, grade, permission_type) "
                                "VALUES (?, ?, ?, 'class_teacher')",
                                (t_phone, t_name, t_grade),
                            )
                    await db.commit()
                    logger.info("Auto-seeded teacher_class_permissions from TEACHER_DATA")
                except Exception as e:
                    logger.error(f"Failed to seed teacher permissions: {e}")
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Auto-seed blocked file types
        # ------------------------------------------------------------------
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM relay_blocked_file_types")
            row = await cursor.fetchone()
            if (row[0] if row else 0) == 0:
                blocked = [
                    ("exe", "Executable file"), ("bat", "Batch script"),
                    ("cmd", "Command script"), ("msi", "Installer"),
                    ("ps1", "PowerShell script"), ("vbs", "VBScript"),
                    ("js", "JavaScript file"), ("jar", "Java archive"),
                    ("scr", "Screensaver/executable"), ("com", "DOS executable"),
                    ("sh", "Shell script"), ("py", "Python script"),
                    ("php", "PHP script"), ("dll", "Dynamic library"),
                ]
                for ext, reason in blocked:
                    await db.execute(
                        "INSERT OR IGNORE INTO relay_blocked_file_types (extension, reason) "
                        "VALUES (?, ?)", (ext, reason),
                    )
                await db.commit()
                logger.info("Auto-seeded relay_blocked_file_types")
        except Exception:
            pass

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

        if dvr_count == 0 and SEED_DVRS:
            logger.info("agent_dvrs table is empty — auto-seeding DVRs")
            try:
                for dvr in SEED_DVRS:
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
                await db.commit()
                logger.info(f"Auto-seeded {len(SEED_DVRS)} DVRs OK")
            except Exception as e:
                logger.error(f"Auto-seed DVRs failed: {e}", exc_info=True)

        # --- Auto-seed camera mappings ---
        cursor = await db.execute("SELECT COUNT(*) FROM agent_camera_mapping")
        row = await cursor.fetchone()
        mapping_count = row[0] if row else 0
        logger.info(f"agent_camera_mapping count on startup: {mapping_count}")

        if mapping_count == 0 and SEED_CAMERA_MAPPING:
            logger.info(
                f"agent_camera_mapping table is empty — auto-seeding "
                f"{len(SEED_CAMERA_MAPPING)} camera mappings"
            )
            try:
                seeded = 0
                for location, data in SEED_CAMERA_MAPPING.items():
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
                # Verify the seed worked
                cursor2 = await db.execute(
                    "SELECT COUNT(*) FROM agent_camera_mapping"
                )
                verify_row = await cursor2.fetchone()
                verify_count = verify_row[0] if verify_row else 0
                logger.info(
                    f"Auto-seeded camera mappings: inserted {seeded}, "
                    f"verified {verify_count} in DB"
                )
            except Exception as e:
                logger.error(
                    f"Auto-seed camera mappings failed: {e}", exc_info=True
                )

    finally:
        await db.close()
