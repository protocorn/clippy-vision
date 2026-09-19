"""Trusted workspace roots for filesystem find + open.

Activity memory knows *what* the user touched. Roots tell Clippy *where*
it is allowed to look on disk. Paths outside these roots are never searched.
"""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from core.storage import conn

_MARKERS = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "composer.json",
    "*.sln",
)

_LABEL_CLEAN_RE = re.compile(r"[^a-zA-Z0-9._\-\s]+")


def _ensure_table() -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_roots (
            root_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT 'user',
            created_at REAL NOT NULL,
            last_used_at REAL,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()


_ensure_table()


def _normalize_path(raw: str | Path) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise ValueError("path is empty")
    p = Path(text).expanduser()
    try:
        p = p.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"could not resolve path: {exc}") from exc
    return p


def _default_label(path: Path) -> str:
    name = path.name.strip() or str(path)
    cleaned = _LABEL_CLEAN_RE.sub("", name).strip() or "workspace"
    return cleaned[:80]


def _row_to_dict(row) -> dict:
    return {
        "root_id": row[0],
        "label": row[1],
        "path": row[2],
        "source": row[3],
        "created_at": row[4],
        "last_used_at": row[5],
        "enabled": bool(row[6]),
    }


def list_roots(*, enabled_only: bool = True) -> list[dict]:
    _ensure_table()
    if enabled_only:
        rows = conn.execute(
            """SELECT root_id, label, path, source, created_at, last_used_at, enabled
               FROM workspace_roots WHERE enabled = 1
               ORDER BY COALESCE(last_used_at, created_at) DESC"""
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT root_id, label, path, source, created_at, last_used_at, enabled
               FROM workspace_roots
               ORDER BY COALESCE(last_used_at, created_at) DESC"""
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_root_by_label(label: str) -> dict | None:
    needle = (label or "").strip().casefold()
    if not needle:
        return None
    for root in list_roots(enabled_only=True):
        if root["label"].casefold() == needle:
            return root
    return None


def remember_root(
    path: str | Path,
    *,
    label: str | None = None,
    source: str = "user",
) -> dict:
    """Add or refresh a trusted folder. Returns the stored root dict."""
    _ensure_table()
    p = _normalize_path(path)
    if not p.exists():
        raise ValueError(f"path does not exist: {p}")
    if not p.is_dir():
        p = p.parent
        if not p.is_dir():
            raise ValueError(f"not a directory: {path}")

    resolved = str(p)
    label_text = (label or "").strip() or _default_label(p)
    label_text = label_text[:80]
    source_text = (source or "user").strip() or "user"
    now = time.time()

    existing = conn.execute(
        "SELECT root_id FROM workspace_roots WHERE path = ?",
        (resolved,),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE workspace_roots
               SET label = ?, source = CASE WHEN source = 'user' THEN source ELSE ? END,
                   last_used_at = ?, enabled = 1
               WHERE root_id = ?""",
            (label_text, source_text, now, existing[0]),
        )
        conn.commit()
        row = conn.execute(
            """SELECT root_id, label, path, source, created_at, last_used_at, enabled
               FROM workspace_roots WHERE root_id = ?""",
            (existing[0],),
        ).fetchone()
        return _row_to_dict(row)

    root_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO workspace_roots
           (root_id, label, path, source, created_at, last_used_at, enabled)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        (root_id, label_text, resolved, source_text, now, now),
    )
    conn.commit()
    return {
        "root_id": root_id,
        "label": label_text,
        "path": resolved,
        "source": source_text,
        "created_at": now,
        "last_used_at": now,
        "enabled": True,
    }


def remove_root(root_id: str) -> bool:
    _ensure_table()
    rid = (root_id or "").strip()
    if not rid:
        return False
    cur = conn.execute("DELETE FROM workspace_roots WHERE root_id = ?", (rid,))
    conn.commit()
    return (cur.rowcount or 0) > 0


def touch_root(path: str | Path) -> None:
    """Bump last_used_at when a path under this root is opened."""
    _ensure_table()
    try:
        target = _normalize_path(path)
    except ValueError:
        return
    now = time.time()
    for root in list_roots(enabled_only=True):
        root_path = Path(root["path"])
        try:
            target.relative_to(root_path)
        except ValueError:
            continue
        conn.execute(
            "UPDATE workspace_roots SET last_used_at = ? WHERE root_id = ?",
            (now, root["root_id"]),
        )
        conn.commit()
        return


def infer_project_root(file_path: str | Path) -> Path | None:
    """Walk up from a file/folder looking for a project marker; else parent dir."""
    try:
        p = _normalize_path(file_path)
    except ValueError:
        return None
    start = p if p.is_dir() else p.parent
    home = Path.home().resolve()
    current = start
    for _ in range(14):
        for marker in _MARKERS:
            if "*" in marker:
                if any(current.glob(marker)):
                    return current
            elif (current / marker).exists():
                return current
        if current == home or current.parent == current:
            break
        current = current.parent
    return start if start.is_dir() else None


def maybe_learn_root_from_open(opened_path: str | Path) -> dict | None:
    """After a successful open, remember the project root if under the user home."""
    try:
        opened = _normalize_path(opened_path)
    except ValueError:
        return None
    inferred = infer_project_root(opened)
    if inferred is None:
        return None
    home = Path.home().resolve()
    try:
        inferred.relative_to(home)
    except ValueError:
        # Outside home — only learn if already inside a trusted root's parent chain.
        under_trusted = False
        for root in list_roots(enabled_only=True):
            try:
                inferred.relative_to(Path(root["path"]))
                under_trusted = True
                break
            except ValueError:
                try:
                    Path(root["path"]).relative_to(inferred)
                    under_trusted = True
                    break
                except ValueError:
                    continue
        if not under_trusted:
            return None

    # Prefer the nearest existing root; only insert a new one for distinct projects.
    for root in list_roots(enabled_only=True):
        root_path = Path(root["path"])
        if root_path == inferred:
            touch_root(inferred)
            return root
        try:
            opened.relative_to(root_path)
            touch_root(opened)
            return root
        except ValueError:
            continue

    try:
        return remember_root(inferred, source="inferred_from_open")
    except ValueError:
        return None


def format_roots_for_prompt(limit: int = 8) -> str:
    roots = list_roots(enabled_only=True)[:limit]
    if not roots:
        return (
            "No trusted workspace folders yet. If the user gives a project/root path, "
            "call remember_workspace_root before find_files."
        )
    lines = ["Trusted workspace folders (search only under these):"]
    for root in roots:
        lines.append(f"- {root['label']}: {root['path']}")
    return "\n".join(lines)
