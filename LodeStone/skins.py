"""Local skins and capes via CustomSkinLoader."""
from pathlib import Path
import base64
import json
import os
import re
import struct
import urllib.request
import urllib.parse
import urllib.error
from .instances import instance_game_dir, instance_mods_dir
from .net import USER_AGENT
from .paths import JsonStore, SKINS_DIR



PNG_SIG = b"\x89PNG\r\n\x1a\n"
def decode_png_uri(uri: str) -> bytes:

    m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=\s]+)$", uri or "")
    if not m:
        raise ValueError("That isn't a PNG image.")
    try:
        data = base64.b64decode(m.group(1))
    except Exception:
        raise ValueError("That image couldn't be read.")
    if not data.startswith(PNG_SIG) or len(data) < 33:
        raise ValueError("That isn't a PNG image.")
    if len(data) > 200_000:
        raise ValueError("That file is too big to be a skin.")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) not in ((64, 64), (64, 32)):
        raise ValueError("Skins must be 64x64 (or 64x32) pixels.")
    return data
def png_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
def _texture_host_ok(host: str) -> bool:
    return host == "minecraft.net" or host.endswith(".minecraft.net")
def fetch_texture_uri(url):
    """Pull a cape texture off Mojang's CDN and inline it as a data URI.

    A plain unauthenticated GET against textures.minecraft.net -- this is not
    account-gated, so it still works with no Microsoft session.
    """
    if not url:
        return None
    if url.startswith("http://textures.minecraft.net"):
        url = "https://" + url[len("http://"):]
    if not _texture_host_ok(urllib.parse.urlparse(url).hostname or ""):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(400_001)
        if len(data) > 400_000 or not data.startswith(PNG_SIG):
            return None
        return png_uri(data)
    except Exception:
        return None
skins_index = JsonStore(SKINS_DIR / "index.json", [])
def list_saved_skins() -> list:
    out = []
    for entry in skins_index.read():
        path = SKINS_DIR / f"{entry.get('id', '')}.png"
        if path.is_file():
            out.append({**entry, "image": png_uri(path.read_bytes())})
    return out
CSL_PROJECT = "customskinloader"
CAPE_TEXTURE_URL = "https://textures.minecraft.net/texture/"
MOJANG_CAPES = [
    {"id": "minecon2011", "name": "MINECON 2011", "hash": "953cac8b779fe41383e675ee2b86071a71658f2180f56fbce8aa315ea70e2ed6"},
    {"id": "minecon2012", "name": "MINECON 2012", "hash": "a2e8d97ec79100e90a75d369d1b3ba81273c4f82bc1b737e934eed4a854be1b6"},
    {"id": "minecon2013", "name": "MINECON 2013", "hash": "153b1a0dfcbae953cdeb6f2c2bf6bf79943239b1372780da44bcbb29273131da"},
    {"id": "minecon2015", "name": "MINECON 2015", "hash": "b0cc08840700447322d953a02b965f1d65a13a603bf64b17c803c21446fe1635"},
    {"id": "minecon2016", "name": "MINECON 2016", "hash": "e7dfea16dc83c97df01a12fabbd1216359c0cd0ea42f9999b6e97c584963e980"},
    {"id": "mojang", "name": "Mojang", "hash": "5786fe99be377dfb6858859f926c4dbc995751e91cee373468c5fbf4865e7151"},
    {"id": "mojang-classic", "name": "Mojang (Classic)", "hash": "8f120319222a9f4a104e2f5cb97b2cda93199a2ee9e1585cb8d09d6f687cb761"},
    {"id": "mojang-studios", "name": "Mojang Studios", "hash": "9e507afc56359978a3eb3e32367042b853cddd0995d17d0da995662913fb00f7"},
    {"id": "migrator", "name": "Migrator", "hash": "2340c0e03dd24a11b15a8b33c2a7e9e32abb2051b2481d0ba7defd635ca7a933"},
    {"id": "vanilla", "name": "Vanilla", "hash": "f9a76537647989f9a0b6d001e320dac591c359e9e61a31f4ce11c88f207f0ad4"},
    {"id": "cherry-blossom", "name": "Cherry Blossom", "hash": "afd553b39358a24edfe3b8a9a939fa5fa4faa4d9a9c3d6af8eafb377fa05c2bb"},
    {"id": "turtle", "name": "Turtle", "hash": "5048ea61566353397247d2b7d946034de926b997d5e66c86483dfb1e031aee95"},
    {"id": "common", "name": "Common", "hash": "5ec930cdd2629c8771655c60eebeb867b4b6559b0e6d3bc71c40c96347fa03f0"},
    {"id": "birthday", "name": "Birthday", "hash": "2056f2eebd759cce93460907186ef44e9192954ae12b227d817eb4b55627a7fc"},
    {"id": "copper", "name": "Copper", "hash": "5e6f3193e74cd16cdd6637d9bae5484e3a37ff2a14c2d157c659a07810b1bdca"},
    {"id": "hero", "name": "Hero", "hash": "bb384a1079b9a6f2811520c7991b6e8150d02e5a96457e44509e30822f72f38e"},
    {"id": "home", "name": "Home", "hash": "1de21419009db483900da6298a1e6cbf9f1bc1523a0dcdc16263fab150693edd"},
    {"id": "realms-mapmaker", "name": "Realms MapMaker", "hash": "17912790ff164b93196f08ba71d0e62129304776d0f347334f8a6eae509f8a56"},
    {"id": "mojira-moderator", "name": "Mojira Moderator", "hash": "ae677f7d98ac70a533713518416df4452fe5700365c09cf45d0d156ea9396551"},
    {"id": "translator", "name": "Translator", "hash": "1bf91499701404e21bd46b0191d63239a4ef76ebde88d27e4d430ac211df681e"},
    {"id": "translator-chinese", "name": "Chinese Translator", "hash": "2262fb1d24912209490586ecae98aca8500df3eff91f2a07da37ee524e7e3cb6"},
    {"id": "millionth-customer", "name": "Millionth Customer", "hash": "70efffaf86fe5bc089608d3cb297d3e276b9eb7a8f9f2fe6659c23a2d8b18edf"},
    {"id": "menace", "name": "Menace", "hash": "dbc21e222528e30dc88445314f7be6ff12d3aeebc3c192054fba7e3b3f8c77b1"},
    {"id": "prismarine", "name": "Prismarine", "hash": "d8f8d13a1adf9636a16c31d47f3ecc9bb8d8533108aa5ad2a01b13b1a0c55eac"},
    {"id": "pan", "name": "Pan", "hash": "28de4a81688ad18b49e735a273e086c18f1e3966956123ccb574034c06f5d336"},
    {"id": "cobalt", "name": "Cobalt", "hash": "ca35c56efe71ed290385f4ab5346a1826b546a54d519e6a3ff01efa01acce81"},
    {"id": "yearn", "name": "Yearn", "hash": "308b32a9e303155a0b4262f9e5483ad4a22e3412e84fe8385a0bdd73dc41fa89"},
    {"id": "15th-anniversary", "name": "15th Anniversary", "hash": "cd9d82ab17fd92022dbd4a86cde4c382a7540e117fae7b9a2853658505a80625"},
    {"id": "mcc-15th-year", "name": "MCC 15th Year", "hash": "56c35628fe1c4d59dd52561a3d03bfa4e1a76d397c8b9c476c2f77cb6aebb1df"},
    {"id": "founders", "name": "Founder's", "hash": "99aba02ef05ec6aa4d42db8ee43796d6cd50e4b2954ab29f0caeb85f96bf52a1"},
    {"id": "purple-heart", "name": "Purple Heart", "hash": "cb40a92e32b57fd732a00fc325e7afb00a7ca74936ad50d8e860152e482cfbde"},
    {"id": "zombie-horse", "name": "Zombie Horse", "hash": "a3f6e4f14801f3ea55e3d95b9b4ef3b5e8802d947f669de93d6ec4b9354a436b"},
    {"id": "scrolls-champion", "name": "Scrolls Champion", "hash": "3efadf6510961830f9fcc077f19b4daf286d502b5f5aafbd807c7bbffcaca245"},
]
_CAPE_BY_ID = {c["id"]: c for c in MOJANG_CAPES}
def _cape_url(cape: dict) -> str:
    return CAPE_TEXTURE_URL + cape["hash"]
def _csl_find_config(instance_name: str):




    game = instance_game_dir(instance_name)
    primary = game / "CustomSkinLoader" / "CustomSkinLoader.json"
    if primary.is_file():
        return primary
    skip = {"saves", "mods", "resourcepacks", "shaderpacks", "screenshots", "logs",
            "crash-reports", "schematics"}
    stack = [(game, 0)]
    try:
        while stack:
            cur, depth = stack.pop()
            with os.scandir(cur) as it:
                for e in it:
                    low = e.name.lower()
                    if e.is_file(follow_symlinks=False):
                        if low == "customskinloader.json" and cur.name.lower() == "customskinloader":
                            return Path(e.path)
                    elif e.is_dir(follow_symlinks=False) and depth < 2 and low not in skip:
                        stack.append((Path(e.path), depth + 1))
    except OSError:
        pass
    return None
def _csl_config_path(instance_name: str) -> Path:
    return _csl_find_config(instance_name) or (
        instance_game_dir(instance_name) / "CustomSkinLoader" / "CustomSkinLoader.json")
def _csl_local_dir(instance_name: str) -> Path:

    return _csl_config_path(instance_name).parent / "LocalSkin"
CSL_CONFIG_VERSION = "15.0.1"
CSL_CONFIG_BUILD = 40
CSL_RUN_ONCE_MSG = "You must run the game at least once to use this feature."
def _csl_write_config(instance_name: str):



    path = _csl_config_path(instance_name)
    version, build = CSL_CONFIG_VERSION, CSL_CONFIG_BUILD
    try:
        old = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(old, dict):
            version = old.get("version", version)
            build = old.get("buildNumber", build)
    except (OSError, ValueError):
        pass
    cfg = {
        "version": version,
        "buildNumber": build,
        "loadlist": [
            {"name": "GameProfile", "type": "GameProfile"},
            {"name": "LocalSkin", "type": "Legacy", "checkPNG": False,
             "skin": "LocalSkin/skins/{USERNAME}.png", "model": "auto",
             "cape": "LocalSkin/capes/{USERNAME}.png", "elytra": "LocalSkin/elytras/{USERNAME}.png"},
        ],
        "enableTransparentSkin": True,
        "forceLoadAllTextures": True,
        "enableCape": True,
        "threadPoolSize": 8,
        "enableLogStdOut": False,
        "cacheExpiry": 30,
        "forceUpdateSkull": False,
        "enableLocalProfileCache": False,
        "enableCacheAutoClean": False,
        "forceDisableCache": False,
    }
    _write_bytes_atomic(path, (json.dumps(cfg, indent=2) + "\n").encode("utf-8"))
def _norm_name(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())
def _mod_present(instance_name: str, needle: str) -> bool:
    mods = instance_mods_dir(instance_name)
    if not mods.is_dir():
        return False
    n = _norm_name(needle)
    return any(p.is_file() and p.suffix.lower() == ".jar" and n in _norm_name(p.name) for p in mods.iterdir())
def _csl_installed(instance_name: str) -> bool:
    return _mod_present(instance_name, "customskinloader")
def _write_bytes_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
