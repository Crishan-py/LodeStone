"""Managing local Minecraft servers (Paper, properties, plugins)."""
from pathlib import Path
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
import zipfile
from .catalog import _authors_text, _clean_text, _zip_read, read_mod_jar
from .downloads import ProgressTracker
from .instances import instance_game_dir, instance_mods_dir, read_instance_meta
from .java import DEFAULT_JAVA_INFO, console_java, java_info_from_meta, resolve_java
from .meta import meta_cache, pick_best_version
from .net import CREATE_NO_WINDOW, FABRIC_INSTALLER_LIST_URL, MODRINTH_API, http_download, http_get_json, is_within, open_in_file_manager, sanitize_instance_name
from .paths import DATA_DIR, log_exception



SERVERS_DIR = DATA_DIR / "servers"
SERVERS_DIR.mkdir(parents=True, exist_ok=True)
SERVER_META_FILE = "loadstone_server.json"
SERVER_SOFTWARE = ("vanilla", "paper", "fabric")
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
PAPER_USER_AGENT = "LOADSTONE/1.3 (https://github.com/snigglaberry/loadstone)"
EULA_URL = "https://aka.ms/MinecraftEULA"
PLUGIN_LOADERS = ["paper", "spigot", "bukkit"]
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{1,16}$")
GAMERULES = [
    ("keepInventory", "Keep inventory", "bool", False, ["keep_inventory"]),
    ("mobGriefing", "Mob griefing", "bool", True, ["mob_griefing"]),
    ("doDaylightCycle", "Daylight cycle", "bool", True, ["advance_time"]),
    ("doWeatherCycle", "Weather cycle", "bool", True, ["advance_weather"]),
    ("doMobSpawning", "Mob spawning", "bool", True, ["spawn_mobs"]),
    ("doFireTick", "Fire spreads", "bool", True, []),
    ("naturalRegeneration", "Natural regeneration", "bool", True, ["natural_health_regeneration"]),
    ("showDeathMessages", "Death messages", "bool", True, ["show_death_messages"]),
    ("announceAdvancements", "Announce advancements", "bool", True, ["show_advancement_messages"]),
    ("commandBlockOutput", "Command block output", "bool", True, ["command_block_output"]),
    ("doImmediateRespawn", "Immediate respawn", "bool", False, ["immediate_respawn"]),
    ("fallDamage", "Fall damage", "bool", True, ["fall_damage"]),
    ("fireDamage", "Fire damage", "bool", True, ["fire_damage"]),
    ("drowningDamage", "Drowning damage", "bool", True, ["drowning_damage"]),
    ("doInsomnia", "Phantoms spawn", "bool", True, ["spawn_phantoms"]),
    ("doPatrolSpawning", "Pillager patrols", "bool", True, ["spawn_patrols"]),
    ("doTraderSpawning", "Wandering traders", "bool", True, ["spawn_wandering_traders"]),
    ("disableRaids", "Disable raids", "bool", False, ["raids"]),
    ("playersSleepingPercentage", "Sleeping percentage", "int", 100, ["players_sleeping_percentage"]),
    ("randomTickSpeed", "Random tick speed", "int", 3, ["random_tick_speed"]),
    ("spawnRadius", "Spawn radius", "int", 10, ["respawn_radius"]),
]
_GAMERULE_IDS = {g[0]: g for g in GAMERULES}
WORLD_TYPES = ["minecraft:normal", "minecraft:flat", "minecraft:large_biomes", "minecraft:amplified"]
LEGACY_WORLD_TYPES = {"minecraft:normal": "default", "minecraft:flat": "flat",
                      "minecraft:large_biomes": "largeBiomes", "minecraft:amplified": "amplified"}
def server_dir(name: str) -> Path:
    return SERVERS_DIR / name
def read_server_meta(name: str):
    try:
        return json.loads((server_dir(name) / SERVER_META_FILE).read_text(encoding="utf-8"))
    except Exception:
        return None
def write_server_meta(name: str, meta: dict):
    d = server_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (SERVER_META_FILE + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, d / SERVER_META_FILE)
def mc_tuple(version: str):
    nums = [int(n) for n in re.findall(r"\d+", (version or "").split("-")[0])[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)
def _prop_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).replace("\r", " ").replace("\n", " ")
    return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in s)
def read_properties(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v
    except OSError:
        pass
    return out
def write_properties(path: Path, updates: dict):
    lines, seen = [], set()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else None
            if key in updates:
                lines.append(f"{key}={_prop_value(updates[key])}")
                seen.add(key)
            else:
                lines.append(line)
    else:
        lines.append("#Minecraft server properties (written by LOADSTONE)")
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={_prop_value(v)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
def _clamp_int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default
def build_server_properties(cfg: dict, version: str) -> dict:
    wt = cfg.get("level_type") if cfg.get("level_type") in WORLD_TYPES else "minecraft:normal"
    if mc_tuple(version) < (1, 16, 0):
        wt = LEGACY_WORLD_TYPES[wt]
    gm = cfg.get("gamemode") if cfg.get("gamemode") in ("survival", "creative", "adventure", "spectator") else "survival"
    diff = cfg.get("difficulty") if cfg.get("difficulty") in ("peaceful", "easy", "normal", "hard") else "easy"
    hardcore = bool(cfg.get("hardcore"))
    props = {
        "motd": (cfg.get("motd") or "A LOADSTONE server")[:120],
        "server-port": _clamp_int(cfg.get("port"), 1024, 65535, 25565),
        "max-players": _clamp_int(cfg.get("max_players"), 1, 1000, 20),
        "gamemode": gm,
        "difficulty": "hard" if hardcore else diff,
        "hardcore": hardcore,
        "pvp": bool(cfg.get("pvp", True)),
        "online-mode": bool(cfg.get("online_mode", False)),
        "allow-flight": bool(cfg.get("allow_flight", False)),
        "allow-nether": bool(cfg.get("allow_nether", True)),
        "spawn-monsters": bool(cfg.get("spawn_monsters", True)),
        "spawn-animals": bool(cfg.get("spawn_animals", True)),
        "spawn-npcs": bool(cfg.get("spawn_npcs", True)),
        "enable-command-block": bool(cfg.get("enable_command_block", False)),
        "generate-structures": bool(cfg.get("generate_structures", True)),
        "white-list": bool(cfg.get("white_list", False)),
        "view-distance": _clamp_int(cfg.get("view_distance"), 2, 32, 10),
        "simulation-distance": _clamp_int(cfg.get("simulation_distance"), 2, 32, 10),
        "spawn-protection": _clamp_int(cfg.get("spawn_protection"), 0, 1000, 16),
        "level-name": re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", (cfg.get("level_name") or "world")).strip() or "world",
        "level-seed": (cfg.get("level_seed") or "").strip()[:64],
        "level-type": wt,
    }
    return props
def gamerule_candidates(camel: str, version: str) -> list:
    g = _GAMERULE_IDS.get(camel)
    if not g:
        return []
    names = [camel] + list(g[4])
    if mc_tuple(version) >= (1, 21, 11):
        names = list(g[4]) + [camel]
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out
def _has_java_error(line: str) -> bool:
    return "Incorrect argument" in line or "Unknown or incomplete" in line or "<--[HERE]" in line
def fetch_paper_versions() -> list:
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(PAPER_API, headers={"User-Agent": PAPER_USER_AGENT,
                                                             "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            last = exc
            time.sleep(0.8 * (attempt + 1))
    else:
        raise last
    raw = data.get("versions", {})
    flat = []
    if isinstance(raw, dict):
        for group in raw.values():
            flat.extend(group)
    else:
        flat = list(raw)
    flat = [str(v) for v in flat if re.fullmatch(r"\d+(\.\d+)+", str(v))]
    if not flat:
        raise RuntimeError("Paper returned no versions.")
    return sorted(set(flat), key=mc_tuple, reverse=True)
def paper_latest_build(version: str) -> dict:
    req = urllib.request.Request(f"{PAPER_API}/versions/{urllib.parse.quote(version)}/builds",
                                 headers={"User-Agent": PAPER_USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        builds = json.loads(resp.read().decode("utf-8"))
    if isinstance(builds, dict):
        builds = builds.get("builds", [])
    if not builds:
        raise RuntimeError(f"Paper has no builds for {version}.")
    pick = next((b for b in builds if str(b.get("channel", "")).upper() == "STABLE"), builds[0])
    dl = (pick.get("downloads") or {}).get("server:default")
    if not dl or not dl.get("url"):
        raise RuntimeError("Paper build has no download.")
    return dl
def read_plugin_jar(path: Path) -> dict:
    info = {"name": None, "version": None, "description": None, "authors": None,
            "loader": "paper", "_icon": None}
    try:
        with zipfile.ZipFile(path) as zf:
            raw = _zip_read(zf, "paper-plugin.yml") or _zip_read(zf, "plugin.yml")
            if raw:
                text = raw.decode("utf-8-sig", "replace")

                def grab(key):
                    m = re.search(rf"^{key}\s*:\s*['\"]?(.+?)['\"]?\s*$", text, re.M)
                    return m.group(1) if m else None
                info.update(name=grab("name"), version=grab("version"),
                            description=grab("description"), authors=grab("author"))
    except Exception:
        logging.info("Couldn't read plugin metadata from %s", path.name)
    info["name"] = _clean_text(info["name"], 80)
    info["description"] = _clean_text(info["description"])
    info["authors"] = _authors_text(info["authors"])
    return info
class ProcSampler:


    def __init__(self, proc):
        self.proc = proc
        self.pid = proc.pid
        self._ps = None
        self._last = None
        try:
            import psutil
            self._ps = psutil.Process(self.pid)
            self._ps.cpu_percent(None)
        except Exception:
            self._ps = None

    def sample(self):
        ncpu = os.cpu_count() or 1
        try:
            if self._ps is not None:
                return self._ps.cpu_percent(None) / ncpu, self._ps.memory_info().rss
            if os.name == "nt":
                return self._sample_windows(ncpu)
            if os.path.exists(f"/proc/{self.pid}/stat"):
                return self._sample_linux(ncpu)
        except Exception:
            pass
        return None, None

    def _sample_windows(self, ncpu):
        import ctypes
        from ctypes import wintypes
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        handle = wintypes.HANDLE(int(self.proc._handle))

        class FT(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        c, e, kt, ut = FT(), FT(), FT(), FT()
        k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
        cpu_100ns = ((kt.hi << 32) | kt.lo) + ((ut.hi << 32) | ut.lo)
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb)
        now = time.time()
        cpu = None
        if self._last:
            dt = now - self._last[0]
            if dt > 0:
                cpu = max(0.0, (cpu_100ns - self._last[1]) / 1e7 / dt / ncpu * 100)
        self._last = (now, cpu_100ns)
        return cpu, int(pmc.WorkingSetSize)

    def _sample_linux(self, ncpu):
        parts = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])
        hz = os.sysconf("SC_CLK_TCK")
        rss = int(Path(f"/proc/{self.pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        now = time.time()
        cpu = None
        if self._last:
            dt = now - self._last[0]
            if dt > 0:
                cpu = max(0.0, (ticks - self._last[1]) / hz / dt / ncpu * 100)
        self._last = (now, ticks)
        return cpu, rss
class ServerRuntime:
    def __init__(self, name):
        self.name = name
        self.proc = None
        self.status = "starting"
        self.started = time.time()
        self.log = []
        self.seq = 0
        self.pending = []
        self.players = {}
        self.exited = threading.Event()
        self.lock = threading.Lock()
        self.waiters = []
        self.swallow_until = 0.0
        self.eula_needed = False
        self.tick_ok = True
        self.tps = None
        self.mspt = None
        self.cpu = None
        self.ram = None
        self.stopping = False
        self.restart_after = False
TICK_NOISE_RE = re.compile(r"(The game is running|Target tick rate|Average time per tick|Percentiles|"
                           r"Unknown or incomplete command|<--\[HERE\]|Incorrect argument)")
LOG_JOIN_RE = re.compile(r"\]: ([A-Za-z0-9_.]{1,16}) joined the game\s*$")
LOG_LEAVE_RE = re.compile(r"\]: ([A-Za-z0-9_.]{1,16}) left the game\s*$")
class ServerMixin:
    def _srv_init(self):
        self._srv_rt: dict = {}
        self._srv_installing: set = set()
        self._srv_lock = threading.RLock()

    def _srv_status(self, name):
        with self._srv_lock:
            if name in self._srv_installing:
                return "installing"
            rt = self._srv_rt.get(name)
        return rt.status if rt and rt.status != "offline" else "offline"

    def _srv_summary(self, meta) -> dict:
        name = meta["name"]
        with self._srv_lock:
            rt = self._srv_rt.get(name)
        live = rt is not None and rt.status != "offline"
        props = read_properties(server_dir(name) / "server.properties")
        return {
            "name": name, "software": meta.get("software"), "version": meta.get("version"),
            "loader_version": meta.get("loader_version"), "installed": bool(meta.get("installed")),
            "port": _clamp_int(props.get("server-port"), 1, 65535, meta.get("port", 25565)),
            "max_players": _clamp_int(props.get("max-players"), 1, 1000, 20),
            "memory_mb": meta.get("memory_mb", 2048), "status": self._srv_status(name),
            "players": len(rt.players) if live else 0,
            "uptime": int(time.time() - rt.started) if live and rt.status == "online" else 0,
            "created": meta.get("created"),
        }

    def _srv_payload(self) -> list:
        items = []
        if SERVERS_DIR.is_dir():
            for d in SERVERS_DIR.iterdir():
                meta = read_server_meta(d.name) if d.is_dir() else None
                if meta:
                    meta["name"] = d.name
                    items.append(self._srv_summary(meta))
        items.sort(key=lambda s: (s.get("created") or 0))
        return items

    def _srv_push(self):
        self.emit("serversChanged", {"servers": self._srv_payload()})

    def get_servers(self):
        try:
            return {"success": True, "servers": self._srv_payload(),
                    "gamerules": [{"id": g[0], "label": g[1], "type": g[2], "default": g[3]} for g in GAMERULES]}
        except Exception:
            log_exception("get_servers")
            return {"success": False, "error": "Couldn't read your servers."}

    def get_server_versions(self, software):
        try:
            releases = [v["id"] for v in meta_cache.get_manifest()["versions"] if v["type"] == "release"]
            if software == "paper":
                versions = fetch_paper_versions()
            elif software == "fabric":
                allowed = set(meta_cache.get_fabric_game_versions())
                versions = [v for v in releases if v in allowed]
            else:
                versions = releases
            return {"success": True, "versions": versions}
        except Exception:
            log_exception("get_server_versions")
            return {"success": False, "error": "Couldn't load versions for that server type."}

    def create_server(self, config):
        try:
            cfg = dict(config or {})
            try:
                name = sanitize_instance_name(cfg.get("name"))
            except ValueError as exc:
                return {"success": False, "error": str(exc).replace("Instance", "Server")}
            software = cfg.get("software")
            if software not in SERVER_SOFTWARE:
                return {"success": False, "error": "Pick Vanilla, Paper or Fabric."}
            version = cfg.get("version")
            if not version:
                return {"success": False, "error": "Select a Minecraft version."}
            if software == "fabric" and not cfg.get("loader_version"):
                return {"success": False, "error": "Select a Fabric loader version."}
            if server_dir(name).exists():
                return {"success": False, "error": "A server with that name already exists."}

            gamerules = {}
            for k, v in (cfg.get("gamerules") or {}).items():
                g = _GAMERULE_IDS.get(k)
                if not g:
                    continue
                gamerules[k] = bool(v) if g[2] == "bool" else _clamp_int(v, 0, 100000, g[3])
            meta = {
                "name": name, "software": software, "version": version,
                "loader_version": cfg.get("loader_version") if software == "fabric" else None,
                "installed": False, "memory_mb": _clamp_int(cfg.get("memory_mb"), 512, 65536, 2048),
                "port": _clamp_int(cfg.get("port"), 1024, 65535, 25565),
                "gamerules": gamerules, "gamerules_applied": False,
                "java_component": None, "java_major": None, "created": time.time(),
                "properties": build_server_properties(cfg, version),
            }
            write_server_meta(name, meta)
            with self._srv_lock:
                self._srv_installing.add(name)
            self._srv_push()
            threading.Thread(target=self._srv_install_worker, args=(name,), daemon=True).start()
            return {"success": True}
        except Exception as exc:
            log_exception("create_server")
            return {"success": False, "error": f"Failed to create server: {exc}"}

    def _srv_install_worker(self, name):
        meta = read_server_meta(name)
        try:
            emit = lambda ev, data: self.emit(ev, dict(data, server=name))
            tracker = ProgressTracker(emit)
            tracker.event = "serverInstallProgress"
            tracker.set_task("Fetching version metadata", force=True)
            manifest = meta_cache.get_manifest()
            entry = next((v for v in manifest["versions"] if v["id"] == meta["version"]), None)
            if not entry:
                raise RuntimeError(f"Minecraft {meta['version']} was not found.")
            vjson = meta_cache.get_version_json(meta["version"], entry["url"])
            java_info = vjson.get("javaVersion") or dict(DEFAULT_JAVA_INFO)
            if not resolve_java(java_info, allow_download=True, tracker=tracker):
                raise RuntimeError("Could not find or download a suitable Java runtime.")
            meta["java_component"] = java_info.get("component")
            meta["java_major"] = java_info.get("majorVersion")

            jar = server_dir(name) / "server.jar"
            tracker.set_task(f"Downloading {meta['software'].capitalize()} {meta['version']}", force=True)
            if meta["software"] == "vanilla":
                dl = vjson.get("downloads", {}).get("server")
                if not dl:
                    raise RuntimeError("Mojang has no server download for this version.")
                tracker.reset(dl.get("size") or 1)
                http_download(dl["url"], jar, dl.get("size"), dl.get("sha1"), tracker.add)
            elif meta["software"] == "paper":
                dl = paper_latest_build(meta["version"])
                tracker.reset(dl.get("size") or 1)
                http_download(dl["url"], jar, dl.get("size"), None, tracker.add, user_agent=PAPER_USER_AGENT)
                sha = (dl.get("checksums") or {}).get("sha256")
                if sha:
                    h = hashlib.sha256(jar.read_bytes()).hexdigest()
                    if h != sha:
                        jar.unlink(missing_ok=True)
                        raise RuntimeError("Paper download failed its checksum.")
            else:
                installers = http_get_json(FABRIC_INSTALLER_LIST_URL)
                inst = next((i for i in installers if i.get("stable")), installers[0])["version"]
                url = (f"https://meta.fabricmc.net/v2/versions/loader/{urllib.parse.quote(meta['version'])}/"
                       f"{urllib.parse.quote(meta['loader_version'])}/{urllib.parse.quote(inst)}/server/jar")
                tracker.reset(1)
                http_download(url, jar, None, None, tracker.add)
            (server_dir(name) / "mods").mkdir(exist_ok=True)
            if meta["software"] == "paper":
                (server_dir(name) / "plugins").mkdir(exist_ok=True)
            write_properties(server_dir(name) / "server.properties", meta.pop("properties", {}))
            meta["installed"] = True
            write_server_meta(name, meta)
            self.emit("serverInstallFinished", {"server": name})
        except Exception as exc:
            log_exception("server install")
            self.emit("serverInstallError", {"server": name, "message": f"Couldn't install {name}: {exc}"})
            shutil.rmtree(server_dir(name), ignore_errors=True)
        finally:
            with self._srv_lock:
                self._srv_installing.discard(name)
            self._srv_push()

    def delete_server(self, name):
        try:
            if self._srv_status(name) != "offline":
                return {"success": False, "error": "Stop the server before deleting it."}
            target = server_dir(name)
            if not target.exists() or not is_within(SERVERS_DIR, target):
                return {"success": False, "error": "Server not found."}
            shutil.rmtree(target, ignore_errors=True)
            with self._srv_lock:
                self._srv_rt.pop(name, None)
            self._srv_push()
            return {"success": True}
        except Exception:
            log_exception("delete_server")
            return {"success": False, "error": "Failed to delete the server."}

    def open_server_folder(self, name):
        try:
            d = server_dir(name)
            if not d.is_dir() or not is_within(SERVERS_DIR, d):
                return {"success": False, "error": "Server not found."}
            open_in_file_manager(d)
            return {"success": True}
        except Exception:
            log_exception("open_server_folder")
            return {"success": False, "error": "Couldn't open the folder."}

    def _srv_eula_accepted(self, name) -> bool:
        try:
            text = (server_dir(name) / "eula.txt").read_text(encoding="utf-8", errors="replace")
            return bool(re.search(r"^eula\s*=\s*true\s*$", text, re.M | re.I))
        except OSError:
            return False

    def accept_eula(self, name):

        try:
            meta = read_server_meta(name)
            if not meta:
                return {"success": False, "error": "Server not found."}
            stamp = time.strftime("%a %b %d %H:%M:%S %Z %Y")
            (server_dir(name) / "eula.txt").write_text(
                "#By changing the setting below to TRUE you are indicating your agreement to our EULA "
                f"({EULA_URL}).\n#{stamp}\neula=true\n", encoding="utf-8")
            return self.start_server(name)
        except Exception:
            log_exception("accept_eula")
            return {"success": False, "error": "Couldn't update eula.txt."}

    def start_server(self, name):
        try:
            meta = read_server_meta(name)
            if not meta or not meta.get("installed"):
                return {"success": False, "error": "This server isn't installed yet."}
            with self._srv_lock:
                rt = self._srv_rt.get(name)
                if (rt and rt.status != "offline") or name in self._srv_installing:
                    return {"success": False, "error": "Server is already running."}
            java = resolve_java(java_info_from_meta(meta), allow_download=False)
            if not java:
                return {"success": False, "error": f"Java {meta.get('java_major') or ''} was not found. Recreate the server."}
            mem = _clamp_int(meta.get("memory_mb"), 512, 65536, 2048)
            cmd = [console_java(java), f"-Xmx{mem}M", f"-Xms{min(mem, 1024)}M", "-jar", "server.jar", "nogui"]
            rt = ServerRuntime(name)
            rt.proc = subprocess.Popen(cmd, cwd=str(server_dir(name)), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       creationflags=CREATE_NO_WINDOW, shell=False)
            with self._srv_lock:
                self._srv_rt[name] = rt
            self._srv_line(rt, f"[LOADSTONE] Starting {meta['software']} {meta['version']} with {mem} MB...")
            threading.Thread(target=self._srv_reader, args=(rt, meta), daemon=True).start()
            threading.Thread(target=self._srv_flusher, args=(rt,), daemon=True).start()
            threading.Thread(target=self._srv_monitor, args=(rt,), daemon=True).start()
            self._srv_push()
            return {"success": True}
        except Exception as exc:
            log_exception("start_server")
            return {"success": False, "error": f"Couldn't start the server: {exc}"}

    def _srv_line(self, rt, text):
        with rt.lock:
            rt.seq += 1
            rt.log.append((rt.seq, text))
            if len(rt.log) > 3000:
                del rt.log[:500]
            rt.pending.append((rt.seq, text))

    def _srv_flusher(self, rt):
        while True:
            time.sleep(0.15)
            with rt.lock:
                batch, rt.pending = rt.pending, []
            if batch:
                self.emit("serverLog", {"server": rt.name, "lines": batch})
            if rt.exited.is_set() and not batch:
                return

    def _srv_reader(self, rt, meta):
        name = rt.name
        try:
            for raw in iter(rt.proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                now = time.time()
                with rt.lock:
                    waiters = list(rt.waiters)
                consumed = False
                for w in waiters:
                    m = w["rx"].search(line)
                    if m:
                        w["match"] = m
                        w["event"].set()
                        consumed = True
                        break
                if not consumed and now < rt.swallow_until and TICK_NOISE_RE.search(line):
                    if "Unknown or incomplete" in line:
                        rt.tick_ok = False
                    consumed = True
                if consumed:
                    continue
                self._srv_line(rt, line)
                if "You need to agree to the EULA" in line:
                    rt.eula_needed = True
                if rt.status == "starting" and "Done (" in line and "For help" in line:
                    rt.status = "online"
                    rt.started = time.time()
                    self._srv_push()
                    threading.Thread(target=self._srv_apply_gamerules, args=(rt, meta), daemon=True).start()
                m = LOG_JOIN_RE.search(line)
                if m:
                    rt.players[m.group(1)] = time.time()
                    self.emit("serverPlayers", {"server": name, "players": list(rt.players)})
                    self._srv_push()
                m = LOG_LEAVE_RE.search(line)
                if m:
                    rt.players.pop(m.group(1), None)
                    self.emit("serverPlayers", {"server": name, "players": list(rt.players)})
                    self._srv_push()
        except Exception:
            log_exception("server reader")
        finally:
            code = rt.proc.wait()
            rt.status = "offline"
            rt.players.clear()
            rt.exited.set()
            self._srv_line(rt, f"[LOADSTONE] Server stopped (exit code {code}).")
            self.emit("serverPlayers", {"server": name, "players": []})
            self._srv_push()
            if rt.eula_needed and not self._srv_eula_accepted(name):
                self.emit("serverEula", {"server": name})
            elif code != 0 and not rt.stopping:
                self.emit("serverCrashed", {"server": name,
                                            "message": f"{name} stopped unexpectedly (exit code {code}). Check the console."})
            if rt.restart_after:
                self.start_server(name)

    def _srv_send(self, rt, command: str) -> bool:
        command = (command or "").replace("\r", " ").replace("\n", " ").strip()
        if not command or rt.proc is None or rt.proc.poll() is not None:
            return False
        try:
            rt.proc.stdin.write((command + "\n").encode("utf-8"))
            rt.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _srv_ask(self, rt, command, pattern, timeout=3.0):

        w = {"rx": re.compile(pattern), "event": threading.Event(), "match": None}
        with rt.lock:
            rt.waiters.append(w)
        try:
            if not self._srv_send(rt, command):
                return None
            w["event"].wait(timeout)
            return w["match"]
        finally:
            with rt.lock:
                if w in rt.waiters:
                    rt.waiters.remove(w)

    def _srv_apply_gamerules(self, rt, meta):
        try:
            if meta.get("gamerules_applied") or not meta.get("gamerules"):
                return
            time.sleep(1.0)
            version = meta.get("version", "")
            for camel, value in meta["gamerules"].items():
                for cand in gamerule_candidates(camel, version):
                    v = (not value) if (camel == "disableRaids" and cand == "raids") else value
                    val = ("true" if v else "false") if isinstance(v, bool) else str(v)
                    w = {"rx": re.compile(r"(Incorrect argument|Unknown or incomplete|<--\[HERE\]|Game ?rule .* (is now|has been updated))",
                                          re.I), "event": threading.Event(), "match": None}
                    with rt.lock:
                        rt.waiters.append(w)
                    self._srv_send(rt, f"gamerule {cand} {val}")
                    w["event"].wait(1.5)
                    with rt.lock:
                        if w in rt.waiters:
                            rt.waiters.remove(w)
                    bad = w["match"] is not None and _has_java_error(w["match"].group(0))
                    if not bad:
                        break
            meta = read_server_meta(rt.name) or meta
            meta["gamerules_applied"] = True
            write_server_meta(rt.name, meta)
            self._srv_line(rt, "[LOADSTONE] Game rules applied.")
        except Exception:
            log_exception("apply gamerules")

    def _srv_monitor(self, rt):
        sampler = ProcSampler(rt.proc)
        last_tick = 0.0
        while not rt.exited.is_set():
            time.sleep(2.0)
            if rt.exited.is_set():
                break
            rt.cpu, rt.ram = sampler.sample()
            if rt.status == "online" and rt.tick_ok and time.time() - last_tick > 8:
                last_tick = time.time()
                rt.swallow_until = time.time() + 2.5
                m = self._srv_ask(rt, "tick query", r"Average time per tick:\s*([\d.]+)\s*ms", 2.0)
                if m:
                    rt.mspt = float(m.group(1))
                    rt.tps = min(20.0, 1000.0 / max(rt.mspt, 0.001))
            self.emit("serverStats", {
                "server": rt.name, "status": rt.status, "cpu": rt.cpu, "ram": rt.ram,
                "uptime": int(time.time() - rt.started) if rt.status == "online" else 0,
                "players": len(rt.players), "tps": rt.tps, "mspt": rt.mspt,
            })

    def stop_server(self, name, force=False):
        try:
            with self._srv_lock:
                rt = self._srv_rt.get(name)
            if not rt or rt.status == "offline":
                return {"success": False, "error": "Server is not running."}
            rt.stopping = True
            rt.status = "stopping"
            self._srv_push()
            if force:
                rt.proc.kill()
            else:
                self._srv_send(rt, "stop")
                threading.Thread(target=self._srv_kill_later, args=(rt,), daemon=True).start()
            return {"success": True}
        except Exception:
            log_exception("stop_server")
            return {"success": False, "error": "Couldn't stop the server."}

    def _srv_kill_later(self, rt):
        if not rt.exited.wait(45):
            try:
                rt.proc.kill()
            except OSError:
                pass

    def restart_server(self, name):
        with self._srv_lock:
            rt = self._srv_rt.get(name)
        if not rt or rt.status == "offline":
            return self.start_server(name)
        rt.restart_after = True
        return self.stop_server(name)

    def stop_all_servers(self):
        with self._srv_lock:
            runtimes = list(self._srv_rt.values())
        for rt in runtimes:
            if rt.status != "offline":
                rt.stopping = True
                self._srv_send(rt, "stop")
        for rt in runtimes:
            rt.exited.wait(20)

    def get_server_log(self, name):
        with self._srv_lock:
            rt = self._srv_rt.get(name)
        if not rt:
            return {"success": True, "lines": [], "status": "offline", "players": []}
        with rt.lock:
            lines = list(rt.log)
        return {"success": True, "lines": lines, "status": rt.status, "players": list(rt.players)}

    def send_server_command(self, name, command):
        with self._srv_lock:
            rt = self._srv_rt.get(name)
        if not rt or rt.status in ("offline",):
            return {"success": False, "error": "Server is offline."}
        command = (command or "").strip().lstrip("/")
        if not command:
            return {"success": False, "error": "Type a command first."}
        self._srv_line(rt, "> " + command)
        if not self._srv_send(rt, command):
            return {"success": False, "error": "Couldn't reach the server."}
        return {"success": True}

    def _srv_online_rt(self, name):
        with self._srv_lock:
            rt = self._srv_rt.get(name)
        return rt if rt and rt.status == "online" else None

    def get_server_players(self, name):
        try:
            rt = self._srv_online_rt(name)
            ops = set()
            try:
                ops = {o.get("name") for o in json.loads((server_dir(name) / "ops.json").read_text(encoding="utf-8"))}
            except Exception:
                pass
            return {"success": True, "online": rt is not None,
                    "players": [{"name": p, "op": p in ops} for p in (rt.players if rt else [])]}
        except Exception:
            log_exception("get_server_players")
            return {"success": False, "error": "Couldn't read players."}

    _SRV_BOOL_SETTINGS = {
        "offline_mode": None, "white_list": ("white-list", False), "pvp": ("pvp", True),
        "allow_flight": ("allow-flight", False), "allow_nether": ("allow-nether", True),
        "enable_command_block": ("enable-command-block", False),
        "spawn_monsters": ("spawn-monsters", True), "spawn_animals": ("spawn-animals", True),
        "spawn_npcs": ("spawn-npcs", True),
    }
    _SRV_INT_SETTINGS = {
        "port": ("server-port", 1024, 65535, 25565), "max_players": ("max-players", 1, 1000, 20),
        "view_distance": ("view-distance", 2, 32, 10), "simulation_distance": ("simulation-distance", 2, 32, 10),
        "spawn_protection": ("spawn-protection", 0, 1000, 16),
    }

    def get_server_settings(self, name):
        try:
            meta = read_server_meta(name)
            if not meta:
                return {"success": False, "error": "Server not found."}
            props = read_properties(server_dir(name) / "server.properties")
            s = {"motd": props.get("motd", "A LOADSTONE server"),
                 "memory_mb": _clamp_int(meta.get("memory_mb"), 512, 65536, 2048),
                 "gamemode": props.get("gamemode", "survival"), "difficulty": props.get("difficulty", "easy"),
                 "offline_mode": props.get("online-mode", "false").strip().lower() != "true"}
            for key, spec in self._SRV_BOOL_SETTINGS.items():
                if spec:
                    s[key] = props.get(spec[0], "true" if spec[1] else "false").strip().lower() == "true"
            for key, (prop, lo, hi, default) in self._SRV_INT_SETTINGS.items():
                s[key] = _clamp_int(props.get(prop), lo, hi, default)
            return {"success": True, "settings": s, "status": self._srv_status(name)}
        except Exception:
            log_exception("get_server_settings")
            return {"success": False, "error": "Couldn't read the server settings."}

    def update_server_settings(self, name, settings):
        try:
            meta = read_server_meta(name)
            if not meta or not meta.get("installed"):
                return {"success": False, "error": "This server isn't installed yet."}
            st = dict(settings or {})
            props = {}
            if "motd" in st:
                props["motd"] = str(st["motd"] or "A LOADSTONE server")[:120]
            if st.get("gamemode") in ("survival", "creative", "adventure", "spectator"):
                props["gamemode"] = st["gamemode"]
            if st.get("difficulty") in ("peaceful", "easy", "normal", "hard"):
                props["difficulty"] = st["difficulty"]
            if "offline_mode" in st:
                props["online-mode"] = not bool(st["offline_mode"])
            for key, spec in self._SRV_BOOL_SETTINGS.items():
                if spec and key in st:
                    props[spec[0]] = bool(st[key])
            for key, (prop, lo, hi, default) in self._SRV_INT_SETTINGS.items():
                if key in st:
                    props[prop] = _clamp_int(st[key], lo, hi, default)
            write_properties(server_dir(name) / "server.properties", props)
            if "memory_mb" in st:
                meta["memory_mb"] = _clamp_int(st["memory_mb"], 512, 65536, 2048)
            if "server-port" in props:
                meta["port"] = props["server-port"]
            write_server_meta(name, meta)
            self._srv_push()
            return {"success": True}
        except Exception as exc:
            log_exception("update_server_settings")
            return {"success": False, "error": f"Couldn't save the settings: {exc}"}

    def get_player_info(self, name, player):
        rt = self._srv_online_rt(name)
        if not rt or not PLAYER_NAME_RE.match(player or ""):
            return {"success": False, "error": "Player isn't online."}
        pat = rf"{re.escape(player)} has the following entity data: (.+?)\s*$"
        hp = self._srv_ask(rt, f"data get entity {player} Health", pat)
        gm = self._srv_ask(rt, f"data get entity {player} playerGameType", pat)
        out = {"success": True, "health": None, "gamemode": None}
        try:
            if hp:
                out["health"] = float(re.sub(r"[^\d.\-]", "", hp.group(1)))
            if gm:
                out["gamemode"] = ["survival", "creative", "adventure", "spectator"][int(re.sub(r"\D", "", gm.group(1)))]
        except (ValueError, IndexError):
            pass
        return out

    def player_action(self, name, player, action, value=None):
        try:
            rt = self._srv_online_rt(name)
            if not rt:
                return {"success": False, "error": "Server isn't running."}
            if not PLAYER_NAME_RE.match(player or ""):
                return {"success": False, "error": "Invalid player name."}
            meta = read_server_meta(name) or {}
            version = meta.get("version", "")
            cmds = []
            if action == "gamemode":
                if value not in ("survival", "creative", "adventure", "spectator"):
                    return {"success": False, "error": "Unknown game mode."}
                cmds = [f"gamemode {value} {player}"]
            elif action in ("op", "deop", "kill"):
                cmds = [f"{action} {player}"]
            elif action == "heal":
                cmds = [f"effect give {player} minecraft:instant_health 1 30 true",
                        f"effect give {player} minecraft:saturation 1 30 true"]
            elif action == "feed":
                cmds = [f"effect give {player} minecraft:saturation 1 30 true"]
            elif action == "kick":
                cmds = [f"kick {player} Kicked from LOADSTONE"]
            elif action == "ban":
                cmds = [f"ban {player} Banned from LOADSTONE"]
            elif action == "set_health":
                hp = _clamp_int(value, 1, 1024, 20)
                cmds = [f"effect give {player} minecraft:instant_health 1 30 true"]
                if hp < 20:
                    cmds.append(("DELAY", 0.7))
                    cmds.append(f"damage {player} {20 - hp} minecraft:generic_kill")
            elif action == "max_health":
                mh = _clamp_int(value, 1, 1024, 20)
                attr = "minecraft:max_health" if mc_tuple(version) >= (1, 21, 2) else "minecraft:generic.max_health"
                cmds = [f"attribute {player} {attr} base set {mh}"]
            else:
                return {"success": False, "error": "Unknown action."}
            for c in cmds:
                if isinstance(c, tuple):
                    time.sleep(c[1])
                    continue
                self._srv_line(rt, "> " + c)
                if not self._srv_send(rt, c):
                    return {"success": False, "error": "Couldn't reach the server."}
            return {"success": True}
        except Exception:
            log_exception("player_action")
            return {"success": False, "error": "That action failed."}

    def _srv_content_dir(self, meta):
        if meta.get("software") == "fabric":
            return server_dir(meta["name"]) / "mods"
        if meta.get("software") == "paper":
            return server_dir(meta["name"]) / "plugins"
        return None

    def get_server_content(self, name):
        try:
            meta = read_server_meta(name)
            if not meta:
                return {"success": False, "error": "Server not found."}
            meta["name"] = name
            folder = self._srv_content_dir(meta)
            if folder is None:
                return {"success": True, "items": [], "kind": None, "software": "vanilla"}
            folder.mkdir(exist_ok=True)
            reader = read_mod_jar if meta["software"] == "fabric" else read_plugin_jar
            items = []
            for p in folder.iterdir():
                low = p.name.lower()
                if low.endswith(".jar.disabled"):
                    enabled, stem = False, p.name[:-13]
                elif low.endswith(".jar") and p.is_file():
                    enabled, stem = True, p.name[:-4]
                else:
                    continue
                info = reader(p)
                items.append({"file": p.name, "name": info["name"] or stem, "version": info["version"],
                              "description": info["description"], "authors": info["authors"], "enabled": enabled})
            items.sort(key=lambda i: i["name"].lower())
            return {"success": True, "items": items, "software": meta["software"],
                    "kind": "mods" if meta["software"] == "fabric" else "plugins"}
        except Exception:
            log_exception("get_server_content")
            return {"success": False, "error": "Couldn't read the mods folder."}

    def _srv_content_file(self, name, filename):
        meta = read_server_meta(name)
        if not meta:
            raise RuntimeError("Server not found.")
        meta["name"] = name
        folder = self._srv_content_dir(meta)
        path = folder / Path(filename).name
        if not path.exists() or not is_within(folder, path):
            raise RuntimeError("File not found.")
        return path

    def toggle_server_content(self, name, filename, enabled):
        try:
            if self._srv_status(name) not in ("offline",):
                return {"success": False, "error": "Stop the server before enabling or disabling mods."}
            p = self._srv_content_file(name, filename)
            if enabled and p.name.lower().endswith(".jar.disabled"):
                p.rename(p.with_name(p.name[:-9]))
            elif not enabled and p.name.lower().endswith(".jar"):
                p.rename(p.with_name(p.name + ".disabled"))
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc) or "Couldn't change that file."}

    def delete_server_content(self, name, filename):
        try:
            if self._srv_status(name) not in ("offline",):
                return {"success": False, "error": "Stop the server before removing mods."}
            self._srv_content_file(name, filename).unlink()
            return {"success": True}
        except Exception as exc:
            return {"success": False, "error": str(exc) or "Couldn't delete that file."}

    def search_server_mods(self, name, query="", offset=0):

        try:
            meta = read_server_meta(name)
            if not meta:
                return {"success": False, "error": "Server not found."}
            sw = meta["software"]
            if sw == "vanilla":
                return {"success": True, "hits": [], "total": 0, "unsupported": True}
            facets = [["project_type:mod"], [f"versions:{meta['version']}"]]
            if sw == "fabric":
                facets += [["categories:fabric"], ["server_side:required", "server_side:optional"]]
            else:
                facets += [[f"categories:{c}" for c in PLUGIN_LOADERS]]
            params = {"query": query or "", "limit": "24", "offset": str(max(0, int(offset or 0))),
                      "facets": json.dumps(facets), "index": "relevance" if query else "downloads"}
            data = http_get_json(f"{MODRINTH_API}/search?" + urllib.parse.urlencode(params))
            return {"success": True, "hits": data.get("hits", []), "total": data.get("total_hits", 0)}
        except Exception:
            log_exception("search_server_mods")
            return {"success": False, "error": "Failed to search Modrinth."}

    def _srv_find_version(self, project_id, loaders, mc):
        params = {"loaders": json.dumps(loaders), "game_versions": json.dumps([mc])}
        url = f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}/version?" + urllib.parse.urlencode(params)
        return pick_best_version(http_get_json(url))

    def _srv_install_deps(self, version, dest, loaders, mc, visited, files, depth=0):
        files.append(self._download_version_file(version, dest))
        if depth >= 3:
            return
        for dep in version.get("dependencies", []):
            pid = dep.get("project_id")
            if dep.get("dependency_type") != "required" or not pid or pid in visited:
                continue
            visited.add(pid)
            try:
                dv = (http_get_json(f"{MODRINTH_API}/version/{urllib.parse.quote(dep['version_id'])}")
                      if dep.get("version_id") else self._srv_find_version(pid, loaders, mc))
                if dv:
                    self._srv_install_deps(dv, dest, loaders, mc, visited, files, depth + 1)
            except Exception:
                log_exception(f"server dependency {pid} failed")

    def install_server_mod(self, name, project_id):
        try:
            meta = read_server_meta(name)
            if not meta:
                return {"success": False, "error": "Server not found."}
            meta["name"] = name
            folder = self._srv_content_dir(meta)
            if folder is None:
                return {"success": False, "error": "Vanilla servers can't run mods. Make a Fabric or Paper server."}
            loaders = ["fabric"] if meta["software"] == "fabric" else PLUGIN_LOADERS
            version = self._srv_find_version(project_id, loaders, meta["version"])
            if not version:
                return {"success": False,
                        "error": f"No version of this supports {meta['software'].capitalize()} {meta['version']}."}
            folder.mkdir(exist_ok=True)
            files: list = []
            self._srv_install_deps(version, folder, loaders, meta["version"], {project_id}, files)
            return {"success": True, "files": files}
        except Exception as exc:
            log_exception("install_server_mod")
            return {"success": False, "error": f"Failed to install: {exc}"}

    def _fabric_has_iris(self, name):
        mods = instance_mods_dir(name)
        if not mods.is_dir():
            return False
        for p in mods.iterdir():
            low = p.name.lower()
            if not low.endswith(".jar"):
                continue
            if re.match(r"^iris[-_+ ]", low) and "flywheel" not in low:
                return True
            n = (read_mod_jar(p).get("name") or "").lower()
            if n in ("iris", "iris shaders"):
                return True
        return False

    def install_iris(self, instance_name):
        try:
            meta = read_instance_meta(instance_name)
            if not meta or meta.get("loader") != "fabric":
                return {"success": False, "error": "Iris needs a Fabric instance."}
            version = self._find_version("iris", "fabric", meta["version"])
            if not version:
                return {"success": False, "error": f"Iris doesn't support Minecraft {meta['version']} yet."}
            mods_dir = instance_mods_dir(instance_name)
            mods_dir.mkdir(parents=True, exist_ok=True)
            files: list = []
            self._install_mod_with_deps(version, mods_dir, "fabric", meta["version"], {"YL57xq9U"}, files)
            return {"success": True, "files": files}
        except Exception as exc:
            log_exception("install_iris")
            return {"success": False, "error": f"Failed to install Iris: {exc}"}

    def _install_shader(self, project_id, instance_name, meta):
        mc = meta["version"]
        note = None
        base = f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}/version"
        versions = http_get_json(base + "?" + urllib.parse.urlencode({"game_versions": json.dumps([mc])}))
        if not versions:
            versions = http_get_json(base)
            note = f"No exact match for {mc}, installed the newest version instead."
        iris_first = [v for v in versions if "iris" in (v.get("loaders") or [])] or versions
        version = pick_best_version(iris_first)
        if not version:
            raise RuntimeError("That shader pack has no downloadable version.")
        dest = instance_game_dir(instance_name) / "shaderpacks"
        dest.mkdir(parents=True, exist_ok=True)
        return {"success": True, "files": [self._download_version_file(version, dest)], "note": note}
