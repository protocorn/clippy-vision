"""Bounded filesystem search under trusted workspace roots."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from core.workspace_roots import get_root_by_label, list_roots, remember_root

# Directories we never descend into (name match, case-insensitive).
_SKIP_DIR_NAMES = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".idea",
    ".vs",
    ".vscode",
    "dist",
    "build",
    "target",
    "coverage",
    ".next",
    ".nuxt",
    ".cache",
    "eggs",
    ".eggs",
    "site-packages",
})

# Absolute prefix deny — never search even if somehow listed as a root.
def _deny_prefixes() -> list[Path]:
    prefixes: list[Path] = []
    for key in ("WINDIR", "SystemRoot"):
        val = os.environ.get(key)
        if val:
            prefixes.append(Path(val))
    for raw in (
        r"C:\Windows",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        r"C:\ProgramData",
    ):
        prefixes.append(Path(raw))
    # Deduplicate resolved where possible
    out: list[Path] = []
    seen: set[str] = set()
    for p in prefixes:
        try:
            key = str(p.resolve(strict=False)).casefold()
        except OSError:
            key = str(p).casefold()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


_DENY_PREFIXES = _deny_prefixes()
_MAX_DEPTH_DEFAULT = 10
_MAX_RESULTS_DEFAULT = 15
_MAX_SCAN_FILES = 40_000


def _is_denied(path: Path, *, search_root: Path | None = None) -> bool:
    """Block OS/system areas. Temp is skipped when walking from home, not when it *is* the root."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    text = str(resolved)
    low = text.casefold()

    root_low = ""
    if search_root is not None:
        try:
            root_low = str(search_root.resolve(strict=False)).casefold()
        except OSError:
            root_low = str(search_root).casefold()

    in_temp = "\\appdata\\local\\temp" in low or "/appdata/local/temp" in low
    root_in_temp = "\\appdata\\local\\temp" in root_low or "/appdata/local/temp" in root_low
    if in_temp and not root_in_temp:
        return True

    for prefix in _DENY_PREFIXES:
        try:
            resolved.relative_to(prefix)
            return True
        except ValueError:
            continue
        except Exception:
            pref = str(prefix).casefold()
            if low.startswith(pref):
                return True
    return False


def _resolve_search_roots(
    under: str | None,
) -> tuple[list[Path], str | None]:
    """Return (roots_to_search, error_message)."""
    under = (under or "").strip()
    trusted = list_roots(enabled_only=True)
    if not under:
        if not trusted:
            return [], (
                "No trusted workspace folders. Ask the user for a project root "
                "(e.g. C:\\Users\\proto\\Clippy_Vision) and call remember_workspace_root, "
                "or pass under= with that path once."
            )
        paths = [Path(r["path"]) for r in trusted]
        return [p for p in paths if p.is_dir() and not _is_denied(p, search_root=p)], None

    # Label match
    by_label = get_root_by_label(under)
    if by_label:
        p = Path(by_label["path"])
        if not p.is_dir():
            return [], f"Trusted folder '{by_label['label']}' path missing: {p}"
        if _is_denied(p, search_root=p):
            return [], f"Path is not allowed for search: {p}"
        return [p], None

    # Absolute / relative path
    candidate = Path(under).expanduser()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError as exc:
        return [], f"Could not resolve under={under!r}: {exc}"

    if not candidate.exists():
        return [], f"under path does not exist: {candidate}"
    if not candidate.is_dir():
        candidate = candidate.parent

    if _is_denied(candidate, search_root=candidate):
        return [], f"Path is not allowed for search: {candidate}"

    # Must be inside a trusted root, OR equal a new user-provided path that we
    # auto-remember when it is under the user home (first-time bootstrap).
    for root in trusted:
        root_path = Path(root["path"])
        try:
            candidate.relative_to(root_path)
            return [candidate], None
        except ValueError:
            try:
                root_path.relative_to(candidate)
                # Searching a parent of a trusted root — only if that parent is trusted
                # or we allow home-level search when it's listed.
                continue
            except ValueError:
                continue

    home = Path.home().resolve()
    try:
        candidate.relative_to(home)
    except ValueError:
        return [], (
            f"{candidate} is outside trusted folders and outside your home directory. "
            "Add it via remember_workspace_root or Settings → Trusted folders first."
        )

    # Bootstrap: remember this folder so future finds work without re-prompting.
    try:
        remember_root(candidate, source="agent")
    except ValueError as exc:
        return [], str(exc)
    return [candidate], None


def _name_matches(filename: str, query: str) -> bool:
    q = query.casefold()
    name = filename.casefold()
    if q in name:
        return True
    # Allow simple globs like *.py handled by caller; here substring is enough.
    return False


def find_files(
    name: str,
    *,
    under: str | None = None,
    limit: int = _MAX_RESULTS_DEFAULT,
    max_depth: int = _MAX_DEPTH_DEFAULT,
) -> dict:
    """Search trusted folders for files/folders matching *name*.

    Returns a JSON-serializable dict for the agent tool.
    """
    query = (name or "").strip()
    if not query:
        return {
            "ok": False,
            "error": "name is required (filename or substring, e.g. screenshot_processor.py)",
            "matches": [],
            "roots_searched": [],
        }

    limit = max(1, min(int(limit or _MAX_RESULTS_DEFAULT), 40))
    max_depth = max(1, min(int(max_depth or _MAX_DEPTH_DEFAULT), 16))

    roots, err = _resolve_search_roots(under)
    if err:
        return {
            "ok": False,
            "error": err,
            "matches": [],
            "roots_searched": [],
            "trusted_roots": [
                {"label": r["label"], "path": r["path"]} for r in list_roots(enabled_only=True)
            ],
        }

    matches: list[dict] = []
    scanned = 0
    truncated = False
    t0 = time.time()

    for root in roots:
        if len(matches) >= limit:
            break
        root_resolved = root.resolve(strict=False)
        for dirpath, dirnames, filenames in os.walk(root_resolved, topdown=True, followlinks=False):
            rel = Path(dirpath).relative_to(root_resolved)
            depth = len(rel.parts) if str(rel) != "." else 0
            if depth >= max_depth:
                dirnames[:] = []
                continue

            # Prune heavy/tooling directories in-place
            dirnames[:] = [
                d for d in dirnames
                if d.casefold() not in _SKIP_DIR_NAMES
            ]

            # Match directories themselves
            for d in list(dirnames):
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    truncated = True
                    break
                if _name_matches(d, query):
                    full = Path(dirpath) / d
                    if _is_denied(full, search_root=root_resolved):
                        continue
                    try:
                        st = full.stat()
                        mtime = st.st_mtime
                    except OSError:
                        mtime = None
                    matches.append({
                        "path": str(full),
                        "name": d,
                        "kind": "dir",
                        "root": str(root_resolved),
                        "mtime": mtime,
                    })
                    if len(matches) >= limit:
                        break

            if truncated or len(matches) >= limit:
                break

            for filename in filenames:
                scanned += 1
                if scanned > _MAX_SCAN_FILES:
                    truncated = True
                    break
                if not _name_matches(filename, query):
                    continue
                full = Path(dirpath) / filename
                if _is_denied(full, search_root=root_resolved):
                    continue
                try:
                    st = full.stat()
                    mtime = st.st_mtime
                    size = st.st_size
                except OSError:
                    mtime = None
                    size = None
                matches.append({
                    "path": str(full),
                    "name": filename,
                    "kind": "file",
                    "root": str(root_resolved),
                    "mtime": mtime,
                    "size": size,
                })
                if len(matches) >= limit:
                    break

            if truncated or len(matches) >= limit:
                break
        if truncated or len(matches) >= limit:
            break

    # Prefer exact basename matches first, then shorter paths
    q_exact = query.casefold()

    def sort_key(item: dict):
        exact = 0 if item["name"].casefold() == q_exact else 1
        return (exact, len(item["path"]), -(item.get("mtime") or 0))

    matches.sort(key=sort_key)
    matches = matches[:limit]

    return {
        "ok": True,
        "query": query,
        "matches": matches,
        "count": len(matches),
        "roots_searched": [str(r) for r in roots],
        "truncated": truncated or len(matches) >= limit,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "hint": (
            "Call open_path with one absolute path from matches. "
            "If multiple matches, ask the user which one."
            if matches
            else "No matches. Try a shorter substring, or remember_workspace_root with a better folder."
        ),
    }


def find_files_json(name: str, under: str | None = None, limit: int = _MAX_RESULTS_DEFAULT) -> str:
    return json.dumps(find_files(name, under=under, limit=limit), default=str)
