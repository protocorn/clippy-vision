"""Recover deleted sessions/events rows from an unvacuumed SQLite DB + WAL.

Works on a frozen RECOVERY_* copy. Parses table-leaf pages (including freelist
pages that still hold old payloads) and WAL frames, then writes a recovered DB.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import struct
import uuid
from pathlib import Path

UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Plausible Clippy activity window (roughly 2024-01 → 2027-01 local/unix)
TS_MIN = 1_700_000_000.0
TS_MAX = 1_800_000_000.0

SESSION_COLS = [
    "session_id", "summary_id", "created_at", "window_start", "window_end",
    "summary", "active_task", "entities", "event_count", "expires_at",
    "vision_enriched", "summary_embedding",
]
# Older rows may lack summary_embedding / vision_enriched
SESSION_MIN_COLS = 10

EVENT_REQUIRED = {"event_id", "session_id", "timestamp", "event_type", "expires_at"}


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    for n in range(9):
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        if n == 8:
            value = (value << 8) | b
            break
        value = (value << 7) | (b & 0x7F)
        if b < 0x80:
            break
    return value, i


def serial_type_size(st: int) -> int | None:
    if st == 0:
        return 0
    if st in (1, 2, 3, 4, 5, 6, 7):
        return {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8}[st]
    if st in (8, 9):
        return 0
    if st >= 12 and st % 2 == 0:
        return (st - 12) // 2
    if st >= 13 and st % 2 == 1:
        return (st - 13) // 2
    return None


def decode_value(st: int, data: bytes) -> object:
    if st == 0:
        return None
    if st == 1:
        return int.from_bytes(data, "big", signed=True)
    if st == 2:
        return int.from_bytes(data, "big", signed=True)
    if st == 3:
        return int.from_bytes(data, "big", signed=True)
    if st == 4:
        return int.from_bytes(data, "big", signed=True)
    if st == 5:
        return int.from_bytes(data, "big", signed=True)
    if st == 6:
        return int.from_bytes(data, "big", signed=True)
    if st == 7:
        return struct.unpack(">d", data)[0]
    if st == 8:
        return 0
    if st == 9:
        return 1
    if st >= 12 and st % 2 == 0:
        return data  # blob
    if st >= 13 and st % 2 == 1:
        return data.decode("utf-8", errors="replace")
    return None


def parse_record(payload: bytes) -> list[object] | None:
    try:
        header_size, i = read_varint(payload, 0)
    except ValueError:
        return None
    if header_size < 1 or header_size > len(payload):
        return None
    serials: list[int] = []
    while i < header_size:
        st, i = read_varint(payload, i)
        serials.append(st)
    values: list[object] = []
    cursor = header_size
    for st in serials:
        size = serial_type_size(st)
        if size is None or cursor + size > len(payload):
            return None
        values.append(decode_value(st, payload[cursor:cursor + size]))
        cursor += size
    return values


def parse_table_leaf_page(page: bytes) -> list[list[object]]:
    if len(page) < 8 or page[0] != 0x0D:
        return []
    cell_count = struct.unpack(">H", page[3:5])[0]
    if cell_count == 0 or cell_count > 2000:
        return []
    records: list[list[object]] = []
    for n in range(cell_count):
        off = 8 + 2 * n
        if off + 2 > len(page):
            break
        ptr = struct.unpack(">H", page[off:off + 2])[0]
        if ptr == 0 or ptr >= len(page):
            continue
        try:
            payload_len, i = read_varint(page, ptr)
            _rowid, i = read_varint(page, i)
        except ValueError:
            continue
        end = i + payload_len
        if end > len(page) or payload_len < 2:
            continue
        rec = parse_record(page[i:end])
        if rec:
            records.append(rec)
    return records


def iter_db_pages(db_path: Path, page_size: int) -> list[bytes]:
    data = db_path.read_bytes()
    # page 1 has 100-byte file header
    pages = []
    if len(data) < page_size:
        return pages
    pages.append(data[100:page_size])  # page 1 body after header — usually not freelist content we need
    for start in range(page_size, len(data), page_size):
        pages.append(data[start:start + page_size])
    return pages


def iter_wal_pages(wal_path: Path, page_size: int) -> list[bytes]:
    if not wal_path.exists():
        return []
    data = wal_path.read_bytes()
    # WAL header 32 bytes; each frame = 24-byte header + page_size + 4-byte checksum tail? 
    # Frame: 24-byte header + page contents (page_size bytes)
    pages = []
    pos = 32
    frame_hdr = 24
    while pos + frame_hdr + page_size <= len(data):
        pages.append(data[pos + frame_hdr:pos + frame_hdr + page_size])
        pos += frame_hdr + page_size
    return pages


def looks_like_uuid(val: object) -> bool:
    if not isinstance(val, str):
        return False
    try:
        uuid.UUID(val)
        return True
    except Exception:
        return False


def looks_like_ts(val: object) -> bool:
    if isinstance(val, (int, float)):
        return TS_MIN <= float(val) <= TS_MAX
    return False


def as_session(values: list[object]) -> dict | None:
    if len(values) < SESSION_MIN_COLS:
        return None
    # Map by position for current schema; tolerate missing trailing cols
    padded = list(values) + [None] * max(0, len(SESSION_COLS) - len(values))
    row = dict(zip(SESSION_COLS, padded[: len(SESSION_COLS)]))
    if not looks_like_uuid(row["session_id"]) or not looks_like_uuid(row["summary_id"]):
        return None
    if not looks_like_ts(row["created_at"]) or not looks_like_ts(row["window_start"]):
        return None
    if not looks_like_ts(row["window_end"]) or not looks_like_ts(row["expires_at"]):
        return None
    summary = row["summary"]
    if not isinstance(summary, str) or len(summary.strip()) < 8:
        return None
    # Reject obvious LLM-thought pollution sometimes stored wrongly
    if summary.startswith("The user might be juggling") and "asking for confirmation" in summary:
        # keep it anyway — might be real weird summary; filter later if needed
        pass
    if row["event_count"] is not None and not isinstance(row["event_count"], int):
        try:
            row["event_count"] = int(row["event_count"])
        except Exception:
            row["event_count"] = 0
    if row["vision_enriched"] is not None and not isinstance(row["vision_enriched"], int):
        try:
            row["vision_enriched"] = int(row["vision_enriched"])
        except Exception:
            row["vision_enriched"] = 0
    if isinstance(row.get("summary_embedding"), (bytes, bytearray)):
        row["summary_embedding"] = None
    if isinstance(row.get("entities"), (bytes, bytearray)):
        try:
            row["entities"] = row["entities"].decode("utf-8", errors="replace")
        except Exception:
            row["entities"] = None
    if isinstance(row.get("active_task"), (bytes, bytearray)):
        row["active_task"] = row["active_task"].decode("utf-8", errors="replace")
    return row


def as_event(values: list[object]) -> dict | None:
    """Best-effort events row. Schema has grown; detect by uuid + timestamp + event_type text."""
    if len(values) < 8:
        return None
    # Heuristic: first text that is uuid = event_id, second uuid = session_id
    texts = [v for v in values if isinstance(v, str)]
    uuids = [t for t in texts if looks_like_uuid(t)]
    if len(uuids) < 2:
        return None
    floats = [float(v) for v in values if looks_like_ts(v)]
    if not floats:
        return None
    # event_type: shortish non-uuid string among early text fields
    event_type = None
    for v in values:
        if isinstance(v, str) and not looks_like_uuid(v) and 2 <= len(v) <= 40 and " " not in v:
            if v in {
                "context_change", "screenshot_analysis", "paste", "clipboard_change",
                "typing_burst", "deviation", "mouse_burst",
            } or v.endswith("_change") or "_" in v:
                event_type = v
                break
    if event_type is None:
        for v in values:
            if isinstance(v, str) and not looks_like_uuid(v) and 3 <= len(v) <= 48 and "\n" not in v:
                if re.fullmatch(r"[a-z][a-z0-9_]*", v):
                    event_type = v
                    break
    if not event_type:
        return None

    event_id, session_id = uuids[0], uuids[1]
    timestamp = floats[0]
    expires_at = floats[-1] if len(floats) > 1 else timestamp + 7 * 86400

    def pick_text(*preds):
        for v in values:
            if isinstance(v, str) and not looks_like_uuid(v) and v != event_type:
                for pred in preds:
                    if pred(v):
                        return v
        return None

    summary = pick_text(lambda s: len(s) >= 12)
    process_name = pick_text(lambda s: s.endswith(".exe") or s in {"chrome", "Cursor", "TestApp"} or (3 <= len(s) <= 64 and " " not in s and "." in s))
    window_title = pick_text(lambda s: len(s) > 8 and (" - " in s or s.endswith(".py") or "http" in s.lower() or len(s) > 20))
    active_url = pick_text(lambda s: s.startswith("http://") or s.startswith("https://"))
    ocr = pick_text(lambda s: len(s) > 80)
    activity = pick_text(lambda s: 8 <= len(s) <= 200 and s[0].isupper())

    # Skip pure test fixtures if somehow carved
    if process_name == "TestApp" and event_id in {
        "keyword-rag", "export-screenshot", "retention-existing", "retention-new",
        "vision-race", "topic-event-fallback", "normal-classification", "screenshot-event",
        "defer-ambiguous",
    }:
        return None
    # event_id in this DB is TEXT PK — test ones aren't UUIDs; real ones are UUIDs
    if not looks_like_uuid(event_id):
        return None

    return {
        "event_id": event_id,
        "session_id": session_id,
        "timestamp": timestamp,
        "event_type": event_type,
        "process_name": process_name,
        "current_window_title": window_title,
        "active_url": active_url,
        "summary": summary,
        "expires_at": expires_at,
        "vision_ocr_text": ocr if ocr and ocr != summary else None,
        "vision_activity": activity if activity and activity != summary else None,
        "interesting": 1,
        "classification_status": "done",
    }


def collect_records(pages: list[bytes]) -> tuple[dict[str, dict], dict[str, dict]]:
    sessions: dict[str, dict] = {}
    events: dict[str, dict] = {}
    for page in pages:
        for rec in parse_table_leaf_page(page):
            sess = as_session(rec)
            if sess:
                sessions[sess["summary_id"]] = sess
                continue
            ev = as_event(rec)
            if ev:
                events[ev["event_id"]] = ev
    return sessions, events


def detect_page_size(db_path: Path) -> int:
    header = db_path.read_bytes()[:100]
    if header[0:16] != b"SQLite format 3\x00":
        raise SystemExit(f"Not a SQLite DB: {db_path}")
    page_size = struct.unpack(">H", header[16:18])[0]
    if page_size == 1:
        page_size = 65536
    return page_size


def create_recovered_db(out_path: Path, sessions: dict, events: dict) -> None:
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))
    conn.execute(
        """
        CREATE TABLE sessions(
            session_id TEXT NOT NULL,
            summary_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            window_start REAL NOT NULL,
            window_end REAL NOT NULL,
            summary TEXT NOT NULL,
            active_task TEXT,
            entities TEXT,
            event_count INTEGER,
            expires_at REAL NOT NULL,
            vision_enriched INTEGER DEFAULT 0,
            summary_embedding TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE events(
            event_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            process_name TEXT,
            current_window_title TEXT,
            active_url TEXT,
            previous_process_name TEXT,
            previous_window_title TEXT,
            summary TEXT,
            payload TEXT,
            interesting INTEGER,
            interest_score REAL,
            interest_reason TEXT,
            vector_embedding TEXT,
            image_embedding TEXT,
            image_embedding_model TEXT,
            screenshot_filename TEXT,
            expires_at REAL NOT NULL,
            classification_status TEXT DEFAULT 'pending',
            vision_ocr_text TEXT,
            vision_activity TEXT,
            vision_suggested_action TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO sessions(
            session_id, summary_id, created_at, window_start, window_end,
            summary, active_task, entities, event_count, expires_at,
            vision_enriched, summary_embedding
        ) VALUES (
            :session_id, :summary_id, :created_at, :window_start, :window_end,
            :summary, :active_task, :entities, :event_count, :expires_at,
            :vision_enriched, :summary_embedding
        )
        """,
        list(sessions.values()),
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO events(
            event_id, session_id, timestamp, event_type, process_name,
            current_window_title, active_url, summary, expires_at,
            interesting, classification_status, vision_ocr_text, vision_activity
        ) VALUES (
            :event_id, :session_id, :timestamp, :event_type, :process_name,
            :current_window_title, :active_url, :summary, :expires_at,
            :interesting, :classification_status, :vision_ocr_text, :vision_activity
        )
        """,
        list(events.values()),
    )
    conn.commit()
    conn.close()


def merge_into_live(live_path: Path, recovered_path: Path) -> tuple[int, int]:
    """Insert recovered rows into live DB without wiping conversations/memory."""
    live = sqlite3.connect(str(live_path))
    rec = sqlite3.connect(str(recovered_path))
    live.execute("PRAGMA foreign_keys=OFF")
    sess_rows = rec.execute(
        f"SELECT {', '.join(SESSION_COLS)} FROM sessions"
    ).fetchall()
    # Drop test fixture events from live first
    live.execute("DELETE FROM events WHERE process_name = 'TestApp'")
    live.execute(
        "DELETE FROM events WHERE event_id IN "
        "('keyword-rag','export-screenshot','defer-ambiguous','normal-classification',"
        "'screenshot-event','retention-existing','retention-new','topic-event-fallback','vision-race')"
    )
    inserted_s = 0
    for row in sess_rows:
        try:
            live.execute(
                f"INSERT OR REPLACE INTO sessions ({', '.join(SESSION_COLS)}) "
                f"VALUES ({', '.join('?' for _ in SESSION_COLS)})",
                row,
            )
            inserted_s += 1
        except sqlite3.Error as e:
            print(f"[warn] session insert: {e}")
    ev_cols = [
        "event_id", "session_id", "timestamp", "event_type", "process_name",
        "current_window_title", "active_url", "summary", "expires_at",
        "interesting", "classification_status", "vision_ocr_text", "vision_activity",
    ]
    ev_rows = rec.execute(
        f"SELECT {', '.join(ev_cols)} FROM events"
    ).fetchall()
    inserted_e = 0
    for row in ev_rows:
        try:
            live.execute(
                f"INSERT OR REPLACE INTO events ({', '.join(ev_cols)}) "
                f"VALUES ({', '.join('?' for _ in ev_cols)})",
                row,
            )
            inserted_e += 1
        except sqlite3.Error as e:
            print(f"[warn] event insert: {e}")
    # Rebuild FTS
    for table in ("sessions_fts", "events_fts"):
        try:
            live.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except sqlite3.Error as e:
            print(f"[warn] fts rebuild {table}: {e}")
    live.commit()
    live.close()
    rec.close()
    return inserted_s, inserted_e


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", type=Path, required=True, help="RECOVERY_* folder with events.db")
    ap.add_argument("--out", type=Path, default=None, help="Output recovered.db path")
    ap.add_argument("--merge-live", type=Path, default=None, help="Optional live events.db to merge into")
    args = ap.parse_args()

    db_path = args.src_dir / "events.db"
    wal_path = args.src_dir / "events.db-wal"
    out_path = args.out or (args.src_dir / "recovered.db")

    page_size = detect_page_size(db_path)
    print(f"[recover] page_size={page_size}")
    pages = iter_db_pages(db_path, page_size) + iter_wal_pages(wal_path, page_size)
    print(f"[recover] scanning {len(pages)} pages...")
    sessions, events = collect_records(pages)
    print(f"[recover] carved sessions={len(sessions)} events={len(events)}")
    create_recovered_db(out_path, sessions, events)
    print(f"[recover] wrote {out_path}")

    # Sanity sample
    c = sqlite3.connect(str(out_path))
    print("[recover] sample sessions:")
    for r in c.execute(
        "SELECT datetime(window_start,'unixepoch','localtime'), substr(summary,1,90) "
        "FROM sessions ORDER BY window_start DESC LIMIT 5"
    ):
        print(" ", r[0], "|", r[1])
    print("[recover] sample events:")
    for r in c.execute(
        "SELECT datetime(timestamp,'unixepoch','localtime'), event_type, substr(coalesce(summary,''),1,70) "
        "FROM events ORDER BY timestamp DESC LIMIT 5"
    ):
        print(" ", r[0], r[1], "|", r[2])
    c.close()

    if args.merge_live:
        s, e = merge_into_live(args.merge_live, out_path)
        print(f"[recover] merged into live: sessions={s} events={e}")
        live = sqlite3.connect(str(args.merge_live))
        print(
            "[recover] live counts now:",
            "sessions", live.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "events", live.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        live.close()


if __name__ == "__main__":
    main()
