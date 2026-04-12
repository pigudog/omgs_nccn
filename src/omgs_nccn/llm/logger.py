import json
import sqlite3


class DBLogger:
    def __init__(self, db_path: str = "omgs_nccn_api_trace.db"):
        self.conn = sqlite3.connect(db_path)
        self.create_table()

    def create_table(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='api_logs'
            """
        )
        table_exists = cursor.fetchone() is not None

        if not table_exists:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    model TEXT,
                    temperature REAL,
                    input_text TEXT,
                    output_text TEXT,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    raw_request TEXT,
                    raw_response TEXT,
                    latency_ms REAL,
                    extra_body TEXT,
                    reasoning_details TEXT
                )
                """
            )
        else:
            cursor.execute("PRAGMA table_info(api_logs)")
            columns = [row[1] for row in cursor.fetchall()]

            if "extra_body" not in columns:
                self.conn.execute("ALTER TABLE api_logs ADD COLUMN extra_body TEXT")
            if "reasoning_details" not in columns:
                self.conn.execute(
                    "ALTER TABLE api_logs ADD COLUMN reasoning_details TEXT"
                )

        self.conn.commit()

    def log(self, **data) -> None:
        self.conn.execute(
            """
            INSERT INTO api_logs (
                timestamp, model, temperature, input_text, output_text,
                input_tokens, output_tokens, total_tokens,
                raw_request, raw_response, latency_ms,
                extra_body, reasoning_details
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("timestamp"),
                data.get("model"),
                data.get("temperature"),
                data.get("input_text"),
                data.get("output_text"),
                data.get("input_tokens"),
                data.get("output_tokens"),
                data.get("total_tokens"),
                json.dumps(data.get("raw_request")),
                json.dumps(data.get("raw_response")),
                data.get("latency_ms"),
                json.dumps(data.get("extra_body"))
                if data.get("extra_body")
                else None,
                json.dumps(data.get("reasoning_details"))
                if data.get("reasoning_details")
                else None,
            ),
        )
        self.conn.commit()

