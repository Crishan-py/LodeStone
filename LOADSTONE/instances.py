"""Instance layout, metadata and account lookups."""
from pathlib import Path
import json
import os
from .meta import _meta_lock
from .net import _INVALID_NAME_RE, sanitize_instance_name
from .paths import DATA_DIR, INSTANCES_DIR



INSTALL_SCHEMA = 2
INSTANCE_ICONS = {"box", "shirt", "flame", "hammer", "square", "circle", "triangle", "star",
                  "hexagon", "diamond", "heart", "cat", "ghost", "skull", "zap", "gem"}
def instance_dir(name: str) -> Path:
    safe = sanitize_instance_name(name)
    return INSTANCES_DIR / safe
def read_instance_meta(name: str) -> dict | None:
    meta_path = instance_dir(name) / "instance.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
def write_instance_meta(name: str, meta: dict):
    path = instance_dir(name) / "instance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _meta_lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, path)
def update_instance_meta(name: str, mutator):
    with _meta_lock:
        meta = read_instance_meta(name)
        if meta is None:
            return None
        mutator(meta)
        write_instance_meta(name, meta)
        return meta
def is_installed(meta: dict) -> bool:
    return bool(meta.get("installed")) and meta.get("schema") == INSTALL_SCHEMA
def list_instances() -> list:
    result = []
    if not INSTANCES_DIR.exists():
        return result
    for child in sorted(INSTANCES_DIR.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            try:
                meta = read_instance_meta(child.name)
            except ValueError:
                continue
            if meta:
                result.append(meta)
    result.sort(key=lambda m: m["name"].lower())
    return result
def instance_game_dir(name: str) -> Path:
    return instance_dir(name) / "game"
def instance_mods_dir(name: str) -> Path:
    return instance_game_dir(name) / "mods"
def instance_config_dir(name: str) -> Path:
    return instance_game_dir(name) / "config"
def ensure_game_layout(name: str, loader: str):

    game = instance_game_dir(name)
    folders = ["resourcepacks", "shaderpacks", "saves"]
    if loader in ("fabric", "forge"):
        folders += ["mods", "config"]
    for f in folders:
        (game / f).mkdir(parents=True, exist_ok=True)
def unique_instance_name(base: str) -> str:
    cleaned = _INVALID_NAME_RE.sub("-", base or "").replace("..", "-").strip().rstrip(".")
    cleaned = (cleaned or "Modpack")[:56]
    candidate = cleaned
    n = 2
    while instance_dir(candidate).exists():
        candidate = f"{cleaned} ({n})"
        n += 1
    return candidate
CHANGELOG_REPO = "snigglaberry/loadstone"
APP_VERSION = "1.4"
CHANGELOG_CACHE = DATA_DIR / "changelog_cache.json"
_changelog_mem = {"at": 0.0, "value": None}
def public_account(a: dict) -> dict:

    return {"name": a.get("name"), "uuid": a.get("uuid"), "type": a.get("type", "offline")}
def find_account(accounts: list, key):
    if not key:
        return None
    return (next((a for a in accounts if a.get("uuid") == key), None)
            or next((a for a in accounts if a.get("name") == key), None))
