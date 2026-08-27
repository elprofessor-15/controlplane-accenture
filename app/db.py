import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "controlplane.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trust_ledger (
                id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                delta REAL NOT NULL,
                resulting_score REAL NOT NULL,
                request_id TEXT,
                prev_hash TEXT NOT NULL,
                current_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                request_id TEXT PRIMARY KEY,
                use_case TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                raw_response TEXT NOT NULL,
                final_response TEXT NOT NULL,
                lane TEXT NOT NULL,
                risk_tier TEXT NOT NULL,
                risk_category TEXT NOT NULL,
                deterministic_checks TEXT NOT NULL,
                heavy_checks TEXT NOT NULL,
                decision_action TEXT NOT NULL,
                decision_justification TEXT,
                latency_ms REAL NOT NULL,
                tamper_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor = await db.execute("PRAGMA table_info(audit_log)")
        audit_columns = {row[1] for row in await cursor.fetchall()}
        if "prev_hash" not in audit_columns:
            await db.execute("ALTER TABLE audit_log ADD COLUMN prev_hash TEXT")
        if "chain_hash" not in audit_columns:
            await db.execute("ALTER TABLE audit_log ADD COLUMN chain_hash TEXT")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS review_queue (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                use_case TEXT NOT NULL,
                model_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                response_preview TEXT NOT NULL,
                risk_tier TEXT NOT NULL,
                lane TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                flag_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor = await db.execute(
            "SELECT 1 FROM trust_ledger WHERE model_id = ? LIMIT 1",
            ("claude-sonnet-3-5",),
        )
        if not await cursor.fetchone():
            await db.execute(
                """
                INSERT INTO trust_ledger
                (id, model_id, event_type, delta, resulting_score, request_id, prev_hash, current_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "demo-genesis", "claude-sonnet-3-5", "GENESIS", 0.0, 0.86,
                    "system", "GENESIS_HASH_0000000000000000", "GENESIS_HASH_DEMO_86",
                ),
            )
        await db.commit()

async def get_db_connection():
    return await aiosqlite.connect(DB_PATH)