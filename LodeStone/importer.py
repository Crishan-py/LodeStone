"""Adopting instances from other launchers already on this PC."""
from pathlib import Path
import json
import os
import re
import shutil
from .instances import instance_dir
from .net import current_os_name
from .paths import log_exception



def _home_dir() -> Path:
    return Path.home()
def _windows_appdata() -> Path:
    env = os.environ.get("APPDATA")
    return Path(env) if env else _home_dir() / "AppData" / "Roaming"
def _launcher_roots(launcher: str) -> list:


    system = current_os_name()
    home = _home_dir()
    if launcher == "prismlauncher":
        if system == "windows":
            return [_windows_appdata() / "PrismLauncher"]
        if system == "osx":
            return [home / "Library" / "Application Support" / "PrismLauncher"]
        return [home / ".local" / "share" / "PrismLauncher",
                home / ".var" / "app" / "org.prismlauncher.PrismLauncher" / "data" / "PrismLauncher"]
    if launcher == "polymc":
        if system == "windows":
            return [_windows_appdata() / "PolyMC"]
        if system == "osx":
            return [home / "Library" / "Application Support" / "PolyMC"]
        return [home / ".local" / "share" / "PolyMC",
                home / ".var" / "app" / "org.polymc.PolyMC" / "data" / "polymc"]
    if launcher == "sklauncher":
        if system == "windows":
            return [_windows_appdata() / ".sklauncher"]
        if system == "osx":
            return [home / "Library" / "Application Support" / ".sklauncher"]
        return [home / ".sklauncher"]
    if launcher == "fastclient":
        if system == "windows":
            return [_windows_appdata() / ".fastclient"]
        if system == "osx":
            return [home / "Library" / "Application Support" / ".fastclient"]
        return [home / ".fastclient"]
    return []
LAUNCHER_LABELS = {
    "prismlauncher": "Prism Launcher",
    "polymc": "PolyMC",
    "sklauncher": "SKLauncher",
    "fastclient": "Fast Client",
}
def _read_json_safe(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
def _maven_version(name: str, prefix: str) -> str:

    return name[len(prefix):] if name.startswith(prefix) else ""
def _loader_from_version_json(vjson: dict, mc_version: str):



    for lib in vjson.get("libraries", []):
        lname = lib.get("name", "")
        if lname.startswith("net.fabricmc:fabric-loader:"):
            return "fabric", _maven_version(lname, "net.fabricmc:fabric-loader:")
        if lname.startswith("net.minecraftforge:forge:"):
            full = _maven_version(lname, "net.minecraftforge:forge:")
            prefix = mc_version + "-"
            return "forge", (full[len(prefix):] if full.startswith(prefix) else full)
    vid = vjson.get("id", "")
    m = re.match(r"^fabric-loader-([\w.]+)-", vid)
    if m:
        return "fabric", m.group(1)
    m = re.match(r"^(?:[\w.]+-)?forge-([\w.]+)$", vid)
    if m:
        return "forge", m.group(1)
    return "vanilla", None
def _loader_from_mmc_pack(components: list):

    mc_version = None
    loader, loader_version = "vanilla", None
    for c in components or []:
        uid = c.get("uid", "")
        ver = c.get("version") or c.get("cachedVersion")
        if uid == "net.minecraft":
            mc_version = ver
        elif uid == "net.fabricmc.fabric-loader":
            loader, loader_version = "fabric", ver
        elif uid == "net.minecraftforge":
            loader, loader_version = "forge", ver
        elif uid == "org.quiltmc.quilt-loader":
            loader, loader_version = "quilt", ver
        elif uid == "com.neoforged":
            loader, loader_version = "neoforge", ver
    return mc_version, loader, loader_version
def _count_images(path: Path) -> int:
    if not path.is_dir():
        return 0
    try:
        return sum(1 for p in path.iterdir()
                   if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    except OSError:
        return 0
def _count_dir(path: Path, suffix=None) -> int:
    if not path.is_dir():
        return 0
    try:
        if suffix:
            return sum(1 for p in path.iterdir() if p.is_file() and p.suffix.lower() == suffix)
        return sum(1 for p in path.iterdir())
    except OSError:
        return 0
def _prism_game_dir(instance_dir: Path) -> Path:



    plain = instance_dir / "minecraft"
    dotted = instance_dir / ".minecraft"
    if plain.is_dir():
        return plain
    if dotted.is_dir():
        return dotted
    return plain
def _scan_prismlauncher(root: Path) -> list:
    instances_dir = root / "instances"
    if not instances_dir.is_dir():
        return []
    found = []
    for sub in sorted(instances_dir.iterdir(), key=lambda p: p.name.lower()):
        pack = _read_json_safe(sub / "mmc-pack.json") if sub.is_dir() else None
        if not pack:
            continue
        mc_version, loader, loader_version = _loader_from_mmc_pack(pack.get("components"))
        game_dir = _prism_game_dir(sub)
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "has_options": (game_dir / "options.txt").is_file(),
            "mods_count": _count_dir(game_dir / "mods", ".jar") if loader in ("fabric", "forge") else 0,
            "resourcepacks_count": _count_dir(game_dir / "resourcepacks"),
            "saves_count": _count_dir(game_dir / "saves"),
            "shaderpacks_count": _count_dir(game_dir / "shaderpacks"),
            "schematics_count": _count_dir(game_dir / "schematics"),
            "screenshots_count": _count_images(game_dir / "screenshots"),
            "config_count": _count_dir(game_dir / "config") if loader in ("fabric", "forge") else 0,
        })
    return found
_FASTCLIENT_FABRIC_LOG_RE = re.compile(r"Loading Minecraft (\S+) with Fabric Loader (\S+)")
_FASTCLIENT_MODLAUNCHER_RE = re.compile(r"ModLauncher running: args \[(.*?)\]")
_FASTCLIENT_VERSION_ARG_RE = re.compile(r"--version,\s*([^,\]]+)")
_FASTCLIENT_TARGET_ARG_RE = re.compile(r"--launchTarget,\s*([^,\]]+)")
_FASTCLIENT_MODDED_VERSION_RE = re.compile(r"^(.+?)-(forge|neoforge)-(.+)$")
FASTCLIENT_ROOT_SOURCE_ID = "__root__"
def _read_first_log_line(logs_dir: Path):

    log_path = logs_dir / "latest.log"
    if not log_path.is_file():
        return None
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except OSError:
        return None
    return None
def _parse_fastclient_log_line(line: str):




    if not line:
        return None
    m = _FASTCLIENT_FABRIC_LOG_RE.search(line)
    if m:
        return "fabric", m.group(1), m.group(2)
    m = _FASTCLIENT_MODLAUNCHER_RE.search(line)
    if m:
        args_str = m.group(1)
        vm = _FASTCLIENT_VERSION_ARG_RE.search(args_str)
        version_str = vm.group(1).strip() if vm else None
        tm = _FASTCLIENT_TARGET_ARG_RE.search(args_str)
        target = tm.group(1).strip().lower() if tm else ""
        if version_str:
            vm2 = _FASTCLIENT_MODDED_VERSION_RE.match(version_str)
            if vm2:
                mc_version, loader, loader_version = vm2.group(1), vm2.group(2), vm2.group(3)
                return loader, mc_version, loader_version
            loader = "neoforge" if "neoforge" in target else "forge"
            return loader, version_str, None
        loader = "neoforge" if "neoforge" in target else "forge"
        return loader, None, None
    return None
def _fastclient_versions(versions_dir: Path) -> dict:


    versions = {}
    if not versions_dir.is_dir():
        return versions
    for vsub in versions_dir.iterdir():
        if not vsub.is_dir():
            continue
        vjson = _read_json_safe(vsub / f"{vsub.name}.json")
        if not vjson:
            for f in vsub.iterdir():
                if f.suffix == ".json":
                    vjson = _read_json_safe(f)
                    if vjson:
                        break
        if vjson:
            versions[vsub.name] = vjson
    return versions
def _fastclient_profile_version_id(profile_dir: Path):




    for f in profile_dir.iterdir():
        if f.is_file() and f.suffix == ".json":
            data = _read_json_safe(f)
            if isinstance(data, dict):
                vid = (data.get("lastVersionId") or data.get("versionId")
                       or data.get("version") or data.get("mcVersion"))
                if vid:
                    return vid
    return None
def _fastclient_profile_index(root: Path):






    index = {}
    try:
        entries = list(root.iterdir())
    except OSError:
        return index
    for f in entries:
        if not (f.is_file() and f.suffix == ".json"):
            continue
        data = _read_json_safe(f)
        profiles_obj = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles_obj, dict):
            continue
        for key, entry in profiles_obj.items():
            if not isinstance(entry, dict):
                continue
            vid = (entry.get("lastVersionId") or entry.get("versionId")
                   or entry.get("version") or entry.get("mcVersion"))
            if not vid:
                continue
            names = {key, entry.get("name")}
            game_dir = entry.get("gameDir")
            if game_dir:
                names.add(Path(game_dir).name)
            for n in names:
                if n:
                    index[n] = vid
    return index
def _scan_fastclient(root: Path) -> list:
    versions = _fastclient_versions(root / "versions")
    found = []

    if (root / "logs" / "latest.log").is_file():
        mc_version = next(iter(versions)) if len(versions) == 1 else None
        found.append({
            "source_id": FASTCLIENT_ROOT_SOURCE_ID,
            "name": "Vanilla",
            "version": mc_version,
            "loader": "vanilla",
            "loader_version": None,
            "has_options": (root / "options.txt").is_file(),
            "mods_count": 0,
            "resourcepacks_count": _count_dir(root / "resourcepacks"),
            "saves_count": _count_dir(root / "saves"),
            "shaderpacks_count": _count_dir(root / "shaderpacks"),
            "schematics_count": _count_dir(root / "schematics"),
            "screenshots_count": _count_images(root / "screenshots"),
            "config_count": 0,
        })

    profiles_dir = root / "profiles"
    if not profiles_dir.is_dir():
        return found
    profile_index = _fastclient_profile_index(root)
    for sub in sorted(profiles_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue

        log_info = _parse_fastclient_log_line(_read_first_log_line(sub / "logs"))
        if log_info:
            loader, mc_version, loader_version = log_info
        else:
            version_id = (profile_index.get(sub.name)
                          or _fastclient_profile_version_id(sub))
            if not version_id and sub.name in versions:
                version_id = sub.name
            if not version_id and len(versions) == 1:
                version_id = next(iter(versions))

            vjson = versions.get(version_id)
            if vjson:
                mc_version = vjson.get("inheritsFrom") or vjson.get("id")
                loader, loader_version = _loader_from_version_json(vjson, mc_version)
            else:
                mc_version, loader, loader_version = version_id, "unknown", None

        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "has_options": (sub / "options.txt").is_file(),
            "mods_count": _count_dir(sub / "mods", ".jar"),
            "resourcepacks_count": _count_dir(sub / "resourcepacks"),
            "saves_count": _count_dir(sub / "saves"),
            "shaderpacks_count": _count_dir(sub / "shaderpacks"),
            "schematics_count": _count_dir(sub / "schematics"),
            "screenshots_count": _count_images(sub / "screenshots"),
            "config_count": _count_dir(sub / "config"),
        })
    return found
_SK_LOG_VERSION_RES = (
    re.compile(r"Minecraft Version:\s*(\d[\w.\-]*)"),
    re.compile(r"Loading Minecraft (\d[\w.\-]*)"),
    re.compile(r"--version,?\s*(\d[\w.\-]*)"),
    re.compile(r"Client\s+(\d+\.\d+(?:\.\d+)?)\b"),
)
def _sk_vanilla_version_from_log(logs_dir: Path):


    log_path = logs_dir / "latest.log"
    try:
        with log_path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 80:
                    break
                for rx in _SK_LOG_VERSION_RES:
                    m = rx.search(line)
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return None
def _sk_game_dir(sub: Path):


    for cand in (sub, sub / ".minecraft", sub / "minecraft"):
        if (cand / "logs" / "latest.log").is_file():
            return cand
    return None
def _scan_sklauncher(root: Path) -> list:
    instances_dir = root / "instances"
    if not instances_dir.is_dir():
        return []
    found = []
    for sub in sorted(instances_dir.iterdir(), key=lambda p: p.name.lower()):
        if not sub.is_dir():
            continue
        game_dir = _sk_game_dir(sub)
        if not game_dir:
            continue
        logs_dir = game_dir / "logs"
        log_info = _parse_fastclient_log_line(_read_first_log_line(logs_dir))
        if log_info:
            loader, mc_version, loader_version = log_info
            if not mc_version:
                mc_version = _sk_vanilla_version_from_log(logs_dir)
        else:
            loader, loader_version = "vanilla", None
            mc_version = _sk_vanilla_version_from_log(logs_dir)
        found.append({
            "source_id": sub.name,
            "name": sub.name,
            "version": mc_version,
            "loader": loader,
            "loader_version": loader_version,
            "has_options": (game_dir / "options.txt").is_file(),
            "mods_count": _count_dir(game_dir / "mods", ".jar") if loader in ("fabric", "forge") else 0,
            "resourcepacks_count": _count_dir(game_dir / "resourcepacks"),
            "saves_count": _count_dir(game_dir / "saves"),
            "shaderpacks_count": _count_dir(game_dir / "shaderpacks"),
            "schematics_count": _count_dir(game_dir / "schematics"),
            "screenshots_count": _count_images(game_dir / "screenshots"),
            "config_count": _count_dir(game_dir / "config") if loader in ("fabric", "forge") else 0,
        })
    return found
_LAUNCHER_SCANNERS = {
    "prismlauncher": _scan_prismlauncher,
    "polymc": _scan_prismlauncher,
    "sklauncher": _scan_sklauncher,
    "fastclient": _scan_fastclient,
}
def scan_all_launchers() -> list:
    sources = []
    for launcher, scanner in _LAUNCHER_SCANNERS.items():
        for root in _launcher_roots(launcher):
            if not root.is_dir():
                continue
            try:
                instances = scanner(root)
            except Exception:
                log_exception(f"scanning {launcher} at {root}")
                continue
            if instances:
                sources.append({
                    "launcher": launcher, "label": LAUNCHER_LABELS[launcher],
                    "root": str(root), "instances": instances,
                })
            break
    return sources
def _find_scanned_instance(launcher: str, source_id: str):
    for root in _launcher_roots(launcher):
        if not root.is_dir():
            continue
        scanner = _LAUNCHER_SCANNERS.get(launcher)
        if not scanner:
            return None, None
        for inst in scanner(root):
            if inst["source_id"] == source_id:
                return inst, root
        return None, None
    return None, None
def _source_paths(launcher: str, source_id: str, root: Path):


    if launcher in ("prismlauncher", "polymc"):
        game_dir = _prism_game_dir(root / "instances" / source_id)
        return (game_dir / "options.txt", game_dir / "mods",
                game_dir / "resourcepacks", game_dir / "saves", game_dir / "config",
                game_dir / "shaderpacks", game_dir / "schematics", game_dir / "screenshots")
    if launcher == "sklauncher":
        game_dir = _sk_game_dir(root / "instances" / source_id) or (root / "instances" / source_id)
        return (game_dir / "options.txt", game_dir / "mods",
                game_dir / "resourcepacks", game_dir / "saves", game_dir / "config",
                game_dir / "shaderpacks", game_dir / "schematics", game_dir / "screenshots")
    if launcher == "fastclient":
        profile_dir = root if source_id == FASTCLIENT_ROOT_SOURCE_ID else root / "profiles" / source_id
        return (profile_dir / "options.txt", profile_dir / "mods",
                profile_dir / "resourcepacks", profile_dir / "saves", profile_dir / "config",
                profile_dir / "shaderpacks", profile_dir / "schematics", profile_dir / "screenshots")
    return None, None, None, None, None, None, None, None
def _copy_tree_contents(src: Path, dst: Path):


    if not src.is_dir():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in src.iterdir():
        target = dst / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
            count += 1
        except OSError:
            log_exception(f"copying {item} -> {target}")
    return count
