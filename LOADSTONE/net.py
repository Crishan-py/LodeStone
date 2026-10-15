"""HTTP, filesystem and host-platform helpers."""
from pathlib import Path
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
import uuid



VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_LOADER_LIST_URL = "https://meta.fabricmc.net/v2/versions/loader"
FABRIC_GAME_LIST_URL = "https://meta.fabricmc.net/v2/versions/game"
FABRIC_PROFILE_URL_TMPL = "https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/profile/json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge/"
FORGE_METADATA_URL = FORGE_MAVEN + "maven-metadata.xml"
MODRINTH_API = "https://api.modrinth.com/v2"
RESOURCES_BASE_URL = "https://resources.download.minecraft.net"
LIBRARIES_BASE_URL = "https://libraries.minecraft.net/"
USER_AGENT = "LOADSTONE/1.0 (+https://example.invalid)"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
def http_get_json(url: str, timeout: int = 20, retries: int = 2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404):
                raise
            last = exc
        except Exception as exc:
            last = exc
        time.sleep(0.8 * (attempt + 1))
    raise last
def http_get_text(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")
def _json_or_text(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {"raw": raw.decode("utf-8", "replace")[:300]}
def http_download(url: str, dest: Path, expected_size=None, expected_sha1=None,
                  progress_cb=None, chunk_size: int = 65536, user_agent=None):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": user_agent or USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            f.write(chunk)
            if progress_cb:
                progress_cb(len(chunk))
    if expected_sha1:
        if file_sha1(tmp) != expected_sha1:
            tmp.unlink(missing_ok=True)
            raise IOError(f"Checksum mismatch for {dest.name}")
    os.replace(tmp, dest)
def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
def file_is_valid(path: Path, expected_size=None, expected_sha1=None) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        if expected_size is not None and path.stat().st_size != expected_size:
            return False
        if expected_sha1 is not None:
            return file_sha1(path) == expected_sha1
        return path.stat().st_size > 0
    except OSError:
        return False
def is_within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False
def sanitize_instance_name(name: str) -> str:
    name = (name or "").strip().rstrip(".")
    if not name:
        raise ValueError("Instance name cannot be empty.")
    if len(name) > 64:
        raise ValueError("Instance name is too long.")
    if ".." in name or "/" in name or "\\" in name:
        raise ValueError("Instance name contains invalid characters.")
    if os.path.isabs(name):
        raise ValueError("Instance name cannot be an absolute path.")
    if _INVALID_NAME_RE.search(name):
        raise ValueError('Instance name cannot contain: < > : " / \\ | ? *')
    return name
def offline_uuid(username: str) -> str:
    data = ("OfflinePlayer:" + username).encode("utf-8")
    digest = bytearray(hashlib.md5(data).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))
def current_os_name() -> str:
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "osx"
    return "linux"
def current_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return "64"
    if machine in ("x86", "i386", "i686"):
        return "32"
    if "arm64" in machine or "aarch64" in machine:
        return "arm64"
    return "64"
def classpath_sep() -> str:
    return ";" if current_os_name() == "windows" else ":"
def open_in_file_manager(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if current_os_name() == "windows":
        os.startfile(str(path))
    elif current_os_name() == "osx":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])
FABRIC_INSTALLER_LIST_URL = "https://meta.fabricmc.net/v2/versions/installer"
