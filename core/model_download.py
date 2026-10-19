"""Download Clippy Vision ML weights from Hugging Face on first use.

Embeddings use the public sentence-transformers MiniLM. The checkpoint lives
in one stable per-user folder (Electron userData/models/embeddings/…, or the
source-tree models/ folder during development). CLIPPY_EMBEDDING_MODEL_DIR
overrides that location.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
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


def embedding_ready(path: Path | None = None) -> bool:
    root = path or embedding_model_dir()
    return (root / "config.json").is_file() and (
        (root / "model.safetensors").is_file() or (root / "pytorch_model.bin").is_file()
    )


def _legacy_embedding_sources(project_root: Path, dest: Path) -> list[Path]:
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


def _transfer_model(src: Path, dest: Path, *, prefer_move: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if prefer_move and not dest.exists():
        try:
            shutil.move(str(src), str(dest))
            return
        except OSError:
            pass
    shutil.copytree(src, dest, dirs_exist_ok=True)
    if prefer_move:
        shutil.rmtree(src, ignore_errors=True)


def _migrate_legacy_embedding_model_unlocked(
    dest: Path,
    *,
    project_root: Path,
) -> Path:
    if embedding_ready(dest):
        return dest
    for src in _legacy_embedding_sources(project_root, dest):
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
    if embedding_ready(dest):
        return dest
    with _embedding_lock:
        return _migrate_legacy_embedding_model_unlocked(dest, project_root=root)


def _snapshot(repo_id: str, local_dir: Path) -> Path:
    from huggingface_hub import snapshot_download

    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(local_dir),
    )
    return local_dir


def ensure_embedding_model(*, force: bool = False) -> Path:
    """Ensure official MiniLM weights exist in the resolved model folder."""
    path = embedding_model_dir()
    with _embedding_lock:
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
