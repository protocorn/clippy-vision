"""Restore memory_clusters / memory_facts / identity meta into the live DB.

Sources (merged, live wins on conflict only if newer):
  1. AppData packaged copy (often has older intact memory)
  2. Freelist/WAL carve from RECOVERY_* snapshot
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import struct
import uuid
from pathlib import Path

TS_MIN = 1_700_000_000.0
TS_MAX = 1_800_000_000.0

CLUSTER_COLS = [
    "cluster_id", "label", "description", "centroid",
    "created_at", "updated_at", "fact_count",
]
FACT_COLS = [
    "fact_id", "cluster_id", "text", "vector_embedding",
    "valid_from", "valid_to", "superseded_by", "source", "created_at",
]


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
    if st in (1, 2, 3, 4, 5, 6):
        return int.from_bytes(data, "big", signed=True)
    if st == 7:
        return struct.unpack(">d", data)[0]
    if st == 8:
        return 0
    if st == 9:
        return 1
    if st >= 12 and st % 2 == 0:
        return data
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


def looks_like_uuid(val: object) -> bool:
    if not isinstance(val, str):
        return False
    try:
        uuid.UUID(val)
        return True
    except Exception:
        return False


def looks_like_ts(val: object) -> bool:
    return isinstance(val, (int, float)) and TS_MIN <= float(val) <= TS_MAX


def looks_like_embedding(val: object) -> bool:
    if isinstance(val, (bytes, bytearray)):
        try:
            val = val.decode("utf-8", errors="strict")
        except Exception:
            return False
    if not isinstance(val, str):
        return False
    s = val.strip()
    if not (s.startswith("[") and s.endswith("]")):
        return False
    return len(s) > 40


def as_cluster(values: list[object]) -> dict | None:
    if len(values) < 6:
        return None
    # cluster_id, label, description, centroid, created_at, updated_at, fact_count?
    if not looks_like_uuid(values[0]):
        return None
    if not isinstance(values[1], str) or len(values[1]) < 2 or looks_like_uuid(values[1]):
        return None
    # label is usually snake_case short
    label = values[1]
    if "\n" in label or len(label) > 80:
        return None
    desc = values[2] if isinstance(values[2], str) else None
    centroid = values[3]
    if not looks_like_embedding(centroid):
        return None
    if isinstance(centroid, (bytes, bytearray)):
        centroid = centroid.decode("utf-8", errors="replace")
    if not looks_like_ts(values[4]) or not looks_like_ts(values[5]):
        return None
    fact_count = 0
    if len(values) > 6 and isinstance(values[6], int):
        fact_count = values[6]
    return {
        "cluster_id": values[0],
        "label": label,
        "description": desc,
        "centroid": centroid,
        "created_at": float(values[4]),
        "updated_at": float(values[5]),
        "fact_count": fact_count,
    }


def as_fact(values: list[object]) -> dict | None:
    if len(values) < 8:
        return None
    # fact_id, cluster_id, text, vector_embedding, valid_from, valid_to?, superseded_by?, source, created_at
    if not looks_like_uuid(values[0]) or not looks_like_uuid(values[1]):
        return None
    text = values[2]
    if not isinstance(text, str) or len(text.strip()) < 8:
        return None
    if text.startswith("The user took screenshots"):  # session bleed
        return None
    emb = values[3]
    if not looks_like_embedding(emb):
        # some older rows might have empty/null embedding — skip those (unusable for recall)
        return None
    if isinstance(emb, (bytes, bytearray)):
        emb = emb.decode("utf-8", errors="replace")
    if not looks_like_ts(values[4]):
        return None
    # Flexible tail: valid_to null/float, superseded null/uuid, source str, created_at float
    valid_to = None
    superseded_by = None
    source = "agent"
    created_at = float(values[4])
    tail = values[5:]
    # Find created_at as last timestamp-like
    ts_tail = [float(v) for v in tail if looks_like_ts(v)]
    if ts_tail:
        created_at = ts_tail[-1]
    for v in tail:
        if v is None:
            continue
        if looks_like_ts(v) and valid_to is None and float(v) != float(values[4]):
            # could be valid_to or created_at; prefer earlier non-created as valid_to if source not yet seen
            pass
        if looks_like_uuid(v) and v != values[0] and v != values[1]:
            superseded_by = v
        if isinstance(v, str) and v in {"agent", "user", "distiller", "system"}:
            source = v
        if isinstance(v, str) and not looks_like_uuid(v) and v not in {"agent", "user", "distiller", "system"}:
            if len(v) < 40 and "_" not in v and " " not in v and source == "agent":
                # possible source-like token
                if v.isalpha():
                    source = v
    # valid_to: nullable float between valid_from and created_at+slack, or null
    if len(values) > 5 and looks_like_ts(values[5]) and float(values[5]) != created_at:
        # only treat as valid_to if it's not the only remaining ts (created_at)
        if len(ts_tail) >= 2:
            valid_to = float(values[5])

    return {
        "fact_id": values[0],
        "cluster_id": values[1],
        "text": text.strip(),
        "vector_embedding": emb,
        "valid_from": float(values[4]),
        "valid_to": valid_to,
        "superseded_by": superseded_by,
        "source": source,
        "created_at": created_at,
    }


def iter_pages(db_path: Path, wal_path: Path, page_size: int) -> list[bytes]:
    data = db_path.read_bytes()
    pages = [data[100:page_size]] if len(data) >= page_size else []
    for start in range(page_size, len(data), page_size):
        pages.append(data[start:start + page_size])
    if wal_path.exists():
        wal = wal_path.read_bytes()
        pos = 32
        while pos + 24 + page_size <= len(wal):
            pages.append(wal[pos + 24:pos + 24 + page_size])
            pos += 24 + page_size
    return pages


def detect_page_size(db_path: Path) -> int:
    header = db_path.read_bytes()[:100]
    page_size = struct.unpack(">H", header[16:18])[0]
    return 65536 if page_size == 1 else page_size


def carve_memory(src_dir: Path) -> tuple[dict, dict]:
    db_path = src_dir / "events.db"
    wal_path = src_dir / "events.db-wal"
    page_size = detect_page_size(db_path)
    pages = iter_pages(db_path, wal_path, page_size)
    clusters: dict[str, dict] = {}
    facts: dict[str, dict] = {}
    for page in pages:
        for rec in parse_table_leaf_page(page):
            cl = as_cluster(rec)
            if cl:
                clusters[cl["cluster_id"]] = cl
                continue
            fact = as_fact(rec)
            if fact:
                facts[fact["fact_id"]] = fact
    return clusters, facts


def load_table(conn: sqlite3.Connection, table: str, cols: list[str]) -> dict[str, dict]:
    pk = cols[0]
    rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    out = {}
    for row in rows:
        d = dict(zip(cols, row))
        out[d[pk]] = d
    return out


def upsert_rows(conn: sqlite3.Connection, table: str, cols: list[str], rows: dict[str, dict]) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})"
    )
    n = 0
    for row in rows.values():
        conn.execute(sql, [row.get(c) for c in cols])
        n += 1
    return n


def copy_meta(src: sqlite3.Connection, dst: sqlite3.Connection, only_missing: bool = True) -> int:
    n = 0
    for key, value in src.execute("SELECT key, value FROM memory_meta").fetchall():
        if only_missing:
            exists = dst.execute("SELECT 1 FROM memory_meta WHERE key=?", (key,)).fetchone()
            if exists:
                # Prefer restoring identity.* from backup if live is empty/thin
                if key.startswith("identity."):
                    live_val = dst.execute("SELECT value FROM memory_meta WHERE key=?", (key,)).fetchone()[0]
                    try:
                        live_obj = json.loads(live_val)
                        bak_obj = json.loads(value)
                        live_empty = not str(live_obj.get("value") or "").strip()
                        bak_rich = bool(str(bak_obj.get("value") or "").strip())
                        if live_empty and bak_rich:
                            dst.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES(?, ?)", (key, value))
                            n += 1
                    except Exception:
                        pass
                continue
        dst.execute("INSERT OR REPLACE INTO memory_meta(key, value) VALUES(?, ?)", (key, value))
        n += 1
    return n


def recompute_fact_counts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE memory_clusters
        SET fact_count = COALESCE((
            SELECT COUNT(*) FROM memory_facts f
            WHERE f.cluster_id = memory_clusters.cluster_id
              AND f.valid_to IS NULL
        ), 0)
        """
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", type=Path, default=Path("core/data/events.db"))
    ap.add_argument("--recovery-dir", type=Path, required=True)
    ap.add_argument("--appdata-db", type=Path, default=None)
    args = ap.parse_args()

    appdata = args.appdata_db
    if appdata is None:
        appdata = Path(os.environ.get("APPDATA", "")) / "Clippy Vision" / "data" / "events.db"

    carved_clusters, carved_facts = carve_memory(args.recovery_dir)
    print(f"[memory] carved clusters={len(carved_clusters)} facts={len(carved_facts)}")

    live = sqlite3.connect(str(args.live))
    live.execute("PRAGMA foreign_keys=OFF")

    merged_clusters: dict[str, dict] = {}
    merged_facts: dict[str, dict] = {}

    if appdata.exists():
        src = sqlite3.connect(str(appdata))
        app_clusters = load_table(src, "memory_clusters", CLUSTER_COLS)
        app_facts = load_table(src, "memory_facts", FACT_COLS)
        print(f"[memory] appdata clusters={len(app_clusters)} facts={len(app_facts)}")
        merged_clusters.update(app_clusters)
        merged_facts.update(app_facts)
        meta_n = copy_meta(src, live, only_missing=True)
        print(f"[memory] restored/filled meta keys: {meta_n}")
        src.close()
    else:
        print("[memory] no AppData DB found")

    # Freelist carve overlays (may include newer facts than AppData)
    merged_clusters.update(carved_clusters)
    merged_facts.update(carved_facts)

    # Keep any existing live rows (College Park etc.) — merge without dropping
    live_clusters = load_table(live, "memory_clusters", CLUSTER_COLS)
    live_facts = load_table(live, "memory_facts", FACT_COLS)
    for cid, row in live_clusters.items():
        merged_clusters.setdefault(cid, row)
    for fid, row in live_facts.items():
        merged_facts.setdefault(fid, row)

    # Ensure every fact has a cluster row (orphan facts get a placeholder)
    for fact in list(merged_facts.values()):
        cid = fact["cluster_id"]
        if cid not in merged_clusters:
            merged_clusters[cid] = {
                "cluster_id": cid,
                "label": f"recovered_{cid[:8]}",
                "description": "Recovered cluster placeholder",
                "centroid": fact["vector_embedding"],
                "created_at": fact["created_at"],
                "updated_at": fact["created_at"],
                "fact_count": 0,
            }

    n_c = upsert_rows(live, "memory_clusters", CLUSTER_COLS, merged_clusters)
    n_f = upsert_rows(live, "memory_facts", FACT_COLS, merged_facts)
    recompute_fact_counts(live)
    live.commit()

    print(f"[memory] upserted clusters={n_c} facts={n_f}")
    print(
        "[memory] live now:",
        "clusters", live.execute("SELECT COUNT(*) FROM memory_clusters").fetchone()[0],
        "facts", live.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0],
        "active facts", live.execute("SELECT COUNT(*) FROM memory_facts WHERE valid_to IS NULL").fetchone()[0],
        "identity keys", live.execute("SELECT COUNT(*) FROM memory_meta WHERE key LIKE 'identity.%'").fetchone()[0],
    )
    print("[memory] sample facts:")
    for r in live.execute(
        "SELECT substr(text,1,100) FROM memory_facts WHERE valid_to IS NULL ORDER BY created_at DESC LIMIT 8"
    ):
        print(" ", r[0])
    live.close()


if __name__ == "__main__":
    main()
