"""Download Clippy Vision ML weights from Hugging Face on first use.

Embeddings use the public sentence-transformers MiniLM. The checkpoint lives
in one stable per-user folder (Electron userData/models/embeddings/…, or the
source-tree models/ folder during development). CLIPPY_EMBEDDING_MODEL_DIR
overrides that location.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

EMBEDDING_REPO = os.environ.get(
    "CLIPPY_EMBEDDING_HF_REPO",
    "sentence-transformers/all-MiniLM-L6-v2",
).strip()

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_MODEL_RELATIVE = Path("models") / "embeddings" / EMBEDDING_MODEL_NAME
APP_USER_DATA_NAME = "Clippy Vision"

_embedding_lock = threading.Lock()


def looks_like_ephemeral_code_root(root: Path | None = None) -> bool:
    """True when the running code folder is an AppImage mount or MCP copy."""
    path = Path(root) if root is not None else _PROJECT_ROOT
    text = str(path).replace("\\", "/")
    if "/tmp/.mount_" in text:
        return True
    if path.name.startswith("mcp-runtime"):
        return True
    try:
        return not os.access(path, os.W_OK)
    except OSError:
        return True


def packaged_user_data_dir(
    *,
    platform: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Electron userData for productName 'Clippy Vision'."""
    plat = platform or sys.platform
    environ = env if env is not None else os.environ
    home_path = Path(home) if home is not None else Path.home()
    if plat == "darwin":
        return home_path / "Library" / "Application Support" / APP_USER_DATA_NAME
    if plat in {"win32", "cygwin"}:
        appdata = (environ.get("APPDATA") or "").strip()
        base = Path(appdata) if appdata else home_path / "AppData" / "Roaming"
        return base / APP_USER_DATA_NAME
    xdg = (environ.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg else home_path / ".config"
    return base / APP_USER_DATA_NAME


def _user_data_from_data_dir(data_dir: str, *, project_root: Path) -> Path | None:
    """Map CLIPPY_DATA_DIR to Electron userData when it looks packaged.

    Packaged: ``<userData>/data`` → ``<userData>``.
    Source checkout: ``<repo>/core/data`` stays on the repo ``models/`` folder.
    """
    text = (data_dir or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.name != "data":
        return None
    parent = path.parent
    if parent.name == "core" and (parent.parent / "mcp_server.py").is_file():
        return None
    if parent.resolve() == (project_root / "core").resolve():
        return None
    return parent


def resolve_embedding_model_dir(
    *,
    override: str = "",
    data_dir: str = "",
    project_root: Path | None = None,
    platform: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the MiniLM folder without downloading or migrating."""
    if (override or "").strip():
        return Path(override).expanduser()
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    user_data = _user_data_from_data_dir(data_dir, project_root=root)
    if user_data is not None:
        return user_data / EMBEDDING_MODEL_RELATIVE
    if looks_like_ephemeral_code_root(root):
        return packaged_user_data_dir(platform=platform, home=home, env=env) / EMBEDDING_MODEL_RELATIVE
    return root / EMBEDDING_MODEL_RELATIVE


def embedding_model_dir() -> Path:
    return resolve_embedding_model_dir(
        override=os.environ.get("CLIPPY_EMBEDDING_MODEL_DIR", ""),
        data_dir=os.environ.get("CLIPPY_DATA_DIR", ""),
    )


def managed_embedding_model_dir(
    *,
    data_dir: str = "",
    project_root: Path | None = None,
    platform: str | None = None,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """The folder the app manages, ignoring CLIPPY_EMBEDDING_MODEL_DIR."""
    return resolve_embedding_model_dir(
        override="",
        data_dir=data_dir if data_dir else os.environ.get("CLIPPY_DATA_DIR", ""),
        project_root=project_root,
        platform=platform,
        home=home,
        env=env,
    )


def _paths_equal(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(
            os.path.normpath(str(right))
        )


def is_user_embedding_override(
    *,
    override: str | None = None,
    data_dir: str | None = None,
    project_root: Path | None = None,
) -> bool:
    """True when CLIPPY_EMBEDDING_MODEL_DIR is a user-picked folder.

    Electron and clippy-mcp set the env var to the computed default. That is
    still app-managed. A path that differs from the default is a user override
    and must not be deleted, cleaned, or refilled.
    """
    text = override if override is not None else os.environ.get("CLIPPY_EMBEDDING_MODEL_DIR", "")
    text = (text or "").strip()
    if not text:
        return False
    default = managed_embedding_model_dir(
        data_dir=data_dir if data_dir is not None else os.environ.get("CLIPPY_DATA_DIR", ""),
        project_root=project_root,
    )
    return not _paths_equal(Path(text).expanduser(), default)


def is_protected_override_dir(path: Path | None = None) -> bool:
    """True when *path* is the user-picked override folder."""
    if not is_user_embedding_override():
        return False
    target = Path(path) if path is not None else embedding_model_dir()
    return _paths_equal(target, embedding_model_dir())


def embedding_revision() -> str:
    """Pinned HF revision for MiniLM, or ``main`` when unset."""
    return (os.environ.get("CLIPPY_EMBEDDING_HF_REVISION") or "main").strip() or "main"


def embedding_model_identity() -> dict[str, str]:
    return {"model_id": EMBEDDING_REPO, "revision": embedding_revision()}


def refill_marker_path(model_dir: Path) -> Path:
    """Sibling JSON next to the managed folder, not inside it (rmtree-safe)."""
    return model_dir.with_name(f"{model_dir.name}.refill.json")


def model_files_fingerprint(model_dir: Path) -> str:
    """Stable id for the files currently in *model_dir* (size + mtime)."""
    parts: list[str] = []
    for name in (
        "model.safetensors",
        "pytorch_model.bin",
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
    ):
        path = model_dir / name
        try:
            if path.is_file():
                stat = path.stat()
                parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            continue
    return ";".join(parts)


def read_refill_marker(model_dir: Path) -> dict | None:
    path = refill_marker_path(model_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _marker_fingerprints(marker: dict) -> set[str]:
    recorded: set[str] = set()
    for key in ("fingerprint", "installed_fingerprint"):
        value = str(marker.get(key) or "")
        if value:
            recorded.add(value)
    extra = marker.get("fingerprints")
    if isinstance(extra, list):
        for item in extra:
            text = str(item or "")
            if text:
                recorded.add(text)
    return recorded


def refill_recorded_for_current_model(model_dir: Path | None = None) -> bool:
    """True when a refill was already tried for this identity and these files.

    A later launch may refill again only when the model id/revision changes or
    the files on disk have a different fingerprint.
    """
    dest = model_dir if model_dir is not None else embedding_model_dir()
    if is_protected_override_dir(dest):
        return False
    marker = read_refill_marker(dest)
    if not marker:
        return False
    identity = embedding_model_identity()
    if (
        str(marker.get("model_id") or "") != identity["model_id"]
        or str(marker.get("revision") or "") != identity["revision"]
    ):
        return False
    current = model_files_fingerprint(dest)
    recorded = _marker_fingerprints(marker)
    if not recorded:
        return True
    return current in recorded


def record_refill_attempt(model_dir: Path, *, failed_fingerprint: str | None = None) -> None:
    """Atomically write the refill marker. No-op for a user override."""
    if is_protected_override_dir(model_dir):
        return
    dest = refill_marker_path(model_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{dest.name}.tmp-"
    if dest.parent.is_dir():
        for child in dest.parent.iterdir():
            if child.name.startswith(prefix) and child.is_file():
                child.unlink(missing_ok=True)
    failed = failed_fingerprint if failed_fingerprint is not None else model_files_fingerprint(model_dir)
    installed = model_files_fingerprint(model_dir)
    fingerprints = [item for item in (failed, installed) if item]
    payload = {
        **embedding_model_identity(),
        "fingerprint": failed,
        "installed_fingerprint": installed,
        "fingerprints": fingerprints,
        "attempted": True,
        "attempted_at": time.time(),
    }
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(dest))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def clear_refill_marker(model_dir: Path) -> None:
    """Remove the refill marker after a successful load. No-op for overrides."""
    if is_protected_override_dir(model_dir):
        return
    dest = refill_marker_path(model_dir)
    try:
        dest.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def safetensors_file_valid(path: Path) -> bool:
    """True when *path* is a complete safetensors file the loader can open.

    The loader prefers ``model.safetensors`` over ``pytorch_model.bin``. A
    truncated safetensors next to a good ``.bin`` still fails at load time, so
    this checks the 8-byte header length, JSON metadata, and data offsets.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < 8:
        return False
    try:
        with path.open("rb") as handle:
            raw_len = handle.read(8)
            if len(raw_len) < 8:
                return False
            header_len = int.from_bytes(raw_len, "little")
            max_header = min(size - 8, 100 * 1024 * 1024)
            if header_len < 2 or header_len > max_header:
                return False
            header_bytes = handle.read(header_len)
            if len(header_bytes) != header_len:
                return False
            header = json.loads(header_bytes)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(header, dict):
        return False
    data_size = size - 8 - header_len
    for key, meta in header.items():
        if key == "__metadata__":
            continue
        if not isinstance(meta, dict):
            return False
        offsets = meta.get("data_offsets")
        if not isinstance(offsets, (list, tuple)) or len(offsets) != 2:
            return False
        start, end = offsets
        if not isinstance(start, int) or not isinstance(end, int):
            return False
        if start < 0 or end < start or end > data_size:
            return False
    return True


def _weights_ready(root: Path) -> bool:
    safetensors = root / "model.safetensors"
    if safetensors.is_file():
        return safetensors_file_valid(safetensors)
    return _nonempty_file(root / "pytorch_model.bin")


def embedding_ready(path: Path | None = None) -> bool:
    """True only when the folder has a usable MiniLM checkpoint.

    If ``model.safetensors`` exists it must be a valid safetensors file — a
    sibling ``pytorch_model.bin`` does not count. The ``.bin`` is accepted only
    when no safetensors file is present.
    """
    root = path or embedding_model_dir()
    has_config = _nonempty_file(root / "config.json")
    has_tokenizer = _nonempty_file(root / "tokenizer.json") or _nonempty_file(root / "tokenizer_config.json")
    return has_config and has_tokenizer and _weights_ready(root)


def _temp_sibling(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.tmp-{os.getpid()}")


def cleanup_stale_embedding_temps(dest: Path) -> None:
    """Remove leftover ``<dest>.tmp-*`` folders from a crashed copy or download.

    Restores ``<dest>.bak-*`` when dest is missing so a crash during swap never
    leaves the user with no model. Never touch a user-picked override folder.
    """
    if is_protected_override_dir(dest):
        return
    parent = dest.parent
    if not parent.is_dir():
        return
    prefix = f"{dest.name}.tmp-"
    for child in parent.iterdir():
        if child.name.startswith(prefix) and child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
    _restore_or_drop_backups(dest)


def _restore_or_drop_backups(dest: Path) -> None:
    parent = dest.parent
    if not parent.is_dir():
        return
    prefix = f"{dest.name}.bak-"
    backups = [child for child in parent.iterdir() if child.name.startswith(prefix) and child.is_dir()]
    if not backups:
        return
    if dest.exists():
        for backup in backups:
            shutil.rmtree(backup, ignore_errors=True)
        return
    backups.sort(key=lambda item: item.stat().st_mtime_ns if item.exists() else 0)
    try:
        os.replace(str(backups[-1]), str(dest))
    except OSError:
        return
    for leftover in backups[:-1]:
        shutil.rmtree(leftover, ignore_errors=True)


def _remove_if_incomplete(path: Path) -> None:
    if is_protected_override_dir(path):
        return
    if refill_recorded_for_current_model(path):
        return
    if path.exists() and not embedding_ready(path):
        shutil.rmtree(path, ignore_errors=True)


def discard_managed_embedding_dir(path: Path) -> bool:
    """Delete an app-managed model folder so it can be refilled.

    Returns False (and does nothing) when *path* is a user override.
    Refill no longer uses this: dest stays until a validated replacement exists.
    """
    if is_protected_override_dir(path):
        return False
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    return True


def commit_model_dir(tmp: Path, dest: Path, *, replace_existing: bool = False) -> Path:
    """Atomically install a complete tmp folder over dest.

    *tmp* must already be complete. dest is never deleted until that check
    passes: it is moved aside, then tmp is renamed into place, and dest is
    restored if the swap fails. When dest is already ready and
    *replace_existing* is false, that is a win and tmp is discarded.
    Never writes into a user-picked CLIPPY_EMBEDDING_MODEL_DIR.
    """
    if is_protected_override_dir(dest):
        shutil.rmtree(tmp, ignore_errors=True)
        raise PermissionError(f"Refusing to refill user CLIPPY_EMBEDDING_MODEL_DIR: {dest}")
    if not embedding_ready(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
        raise FileNotFoundError(f"Model copy at {tmp} is incomplete")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if embedding_ready(dest) and not replace_existing:
        shutil.rmtree(tmp, ignore_errors=True)
        return dest
    return _swap_validated_model_dir(tmp, dest)


def _swap_validated_model_dir(tmp: Path, dest: Path) -> Path:
    """Move dest aside, put a ready tmp in its place, restore dest on failure."""
    backup = dest.with_name(f"{dest.name}.bak-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    moved_aside = False
    try:
        if dest.exists():
            os.replace(str(dest), str(backup))
            moved_aside = True
        os.replace(str(tmp), str(dest))
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        if moved_aside and backup.exists() and not dest.exists():
            try:
                os.replace(str(backup), str(dest))
            except OSError:
                pass
        if embedding_ready(dest):
            shutil.rmtree(backup, ignore_errors=True)
            return dest
        raise
    if embedding_ready(dest):
        shutil.rmtree(backup, ignore_errors=True)
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    if backup.exists():
        try:
            os.replace(str(backup), str(dest))
        except OSError:
            pass
    raise FileNotFoundError(f"Atomic install of MiniLM at {dest} did not produce a complete model")


def _legacy_embedding_sources(
    project_root: Path,
    dest: Path,
    *,
    scan_runtime_copies: bool = True,
) -> list[Path]:
    """Places a previous install may have written MiniLM weights."""
    seen: set[Path] = set()
    out: list[Path] = []

    def add(candidate: Path) -> None:
        try:
            key = candidate.resolve()
        except OSError:
            key = candidate
        if key in seen:
            return
        seen.add(key)
        out.append(candidate)

    add(project_root / EMBEDDING_MODEL_RELATIVE)
    try:
        dest_key = dest.resolve()
    except OSError:
        dest_key = dest
    add(dest_key)
    if scan_runtime_copies:
        try:
            user_data = dest.parents[2]
        except IndexError:
            user_data = None
        if user_data is not None and user_data.is_dir():
            try:
                children = sorted(user_data.iterdir(), key=lambda item: item.name)
            except OSError:
                children = []
            for child in children:
                if child.name.startswith("mcp-runtime"):
                    add(child / EMBEDDING_MODEL_RELATIVE)
    return [path for path in out if path != dest and path != dest_key]


def _should_move_legacy(src: Path) -> bool:
    # Leftover copies inside versioned MCP runtime folders can be moved once.
    return any(part.startswith("mcp-runtime") for part in src.parts)


def _transfer_model(src: Path, dest: Path, *, prefer_move: bool, replace_existing: bool = False) -> None:
    """Copy into a temp sibling, verify it, then rename into place."""
    if is_protected_override_dir(dest):
        raise PermissionError(f"Refusing to refill user CLIPPY_EMBEDDING_MODEL_DIR: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cleanup_stale_embedding_temps(dest)
    tmp = _temp_sibling(dest)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp)
        commit_model_dir(tmp, dest, replace_existing=replace_existing)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if prefer_move and src.exists() and embedding_ready(dest):
        shutil.rmtree(src, ignore_errors=True)


def _migrate_legacy_embedding_model_unlocked(
    dest: Path,
    *,
    project_root: Path,
    scan_runtime_copies: bool = True,
) -> Path:
    if is_protected_override_dir(dest):
        return dest
    if refill_recorded_for_current_model(dest):
        return dest
    cleanup_stale_embedding_temps(dest)
    if embedding_ready(dest):
        return dest
    for src in _legacy_embedding_sources(
        project_root, dest, scan_runtime_copies=scan_runtime_copies
    ):
        try:
            if src.resolve() == dest.resolve():
                continue
        except OSError:
            if src == dest:
                continue
        if not embedding_ready(src):
            continue
        try:
            _transfer_model(src, dest, prefer_move=_should_move_legacy(src))
        except OSError as exc:
            print(f"[models] Could not migrate embeddings from {src} -> {dest}: {exc}")
            continue
        if embedding_ready(dest):
            print(f"[models] Migrated embeddings from {src} -> {dest}")
            return dest
    return dest


def migrate_legacy_embedding_model(
    dest: Path | None = None,
    *,
    project_root: Path | None = None,
) -> Path:
    """Reuse a leftover MiniLM folder once instead of downloading again.

    Copies from the source-tree or AppImage path. Moves leftover copies that
    live under ``mcp-runtime-*`` so each update does not keep another 90 MB.
    """
    dest = dest or embedding_model_dir()
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    if is_protected_override_dir(dest):
        return dest
    if refill_recorded_for_current_model(dest):
        return dest
    scan_runtime = not is_user_embedding_override()
    if embedding_ready(dest):
        return dest
    with _embedding_lock:
        return _migrate_legacy_embedding_model_unlocked(
            dest, project_root=root, scan_runtime_copies=scan_runtime
        )


def _snapshot(repo_id: str, local_dir: Path, *, replace_existing: bool = False) -> Path:
    from huggingface_hub import snapshot_download

    if is_protected_override_dir(local_dir):
        raise PermissionError(f"Refusing to download into user CLIPPY_EMBEDDING_MODEL_DIR: {local_dir}")
    cleanup_stale_embedding_temps(local_dir)
    tmp = _temp_sibling(local_dir)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(tmp),
        )
        return commit_model_dir(tmp, local_dir, replace_existing=replace_existing)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def refill_managed_embedding_dir(dest: Path | None = None, *, project_root: Path | None = None) -> Path:
    """Copy or download a replacement without deleting dest first.

    dest is replaced only after the temp folder is complete and validated.
    A bad legacy copy or an offline download leaves dest untouched.
    """
    dest = dest or embedding_model_dir()
    if is_protected_override_dir(dest):
        raise PermissionError(f"Refusing to refill user CLIPPY_EMBEDDING_MODEL_DIR: {dest}")
    root = Path(project_root) if project_root is not None else _PROJECT_ROOT
    with _embedding_lock:
        cleanup_stale_embedding_temps(dest)
        for src in _legacy_embedding_sources(
            root, dest, scan_runtime_copies=not is_user_embedding_override()
        ):
            try:
                if src.resolve() == dest.resolve():
                    continue
            except OSError:
                if src == dest:
                    continue
            if not embedding_ready(src):
                continue
            try:
                _transfer_model(
                    src,
                    dest,
                    prefer_move=_should_move_legacy(src),
                    replace_existing=True,
                )
            except OSError as exc:
                print(f"[models] Could not refill embeddings from {src} -> {dest}: {exc}")
                continue
            if embedding_ready(dest):
                print(f"[models] Refilled embeddings from {src} -> {dest}")
                return dest
        print(f"[models] Downloading embeddings from {EMBEDDING_REPO} -> {dest}")
        _snapshot(EMBEDDING_REPO, dest, replace_existing=True)
        if not embedding_ready(dest):
            raise FileNotFoundError(
                f"Downloaded {EMBEDDING_REPO} but weights are missing under {dest}"
            )
        print(f"[models] Embeddings ready at {dest}")
        return dest


def ensure_embedding_model(*, force: bool = False) -> Path:
    """Ensure official MiniLM weights exist in the resolved model folder."""
    path = embedding_model_dir()
    if is_protected_override_dir(path):
        if embedding_ready(path):
            return path
        raise FileNotFoundError(
            f"CLIPPY_EMBEDDING_MODEL_DIR is missing or incomplete: {path}"
        )
    if not force and refill_recorded_for_current_model(path):
        if embedding_ready(path):
            return path
        identity = embedding_model_identity()
        raise FileNotFoundError(
            f"MiniLM model is missing or incomplete at {path} "
            f"(a refill was already attempted for {identity['model_id']}@"
            f"{identity['revision']})"
        )
    with _embedding_lock:
        cleanup_stale_embedding_temps(path)
        if not force:
            if embedding_ready(path):
                return path
            migrated = _migrate_legacy_embedding_model_unlocked(path, project_root=_PROJECT_ROOT)
            if embedding_ready(migrated):
                return migrated
            path = embedding_model_dir()
        print(f"[models] Downloading embeddings from {EMBEDDING_REPO} -> {path}")
        _snapshot(EMBEDDING_REPO, path)
        if not embedding_ready(path):
            raise FileNotFoundError(
                f"Downloaded {EMBEDDING_REPO} but weights are missing under {path}"
            )
        print(f"[models] Embeddings ready at {path}")
        return path


def ensure_all(*, force: bool = False) -> dict[str, str]:
    """Download local embedding weights. Safe to call from setup or API startup."""
    embedding = ensure_embedding_model(force=force)
    return {
        "embedding_repo": EMBEDDING_REPO,
        "embedding_dir": str(embedding),
    }


if __name__ == "__main__":
    info = ensure_all()
    for key, value in info.items():
        print(f"{key}={value}")
