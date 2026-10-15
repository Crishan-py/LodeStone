"""Locating, probing and installing Java runtimes."""
from pathlib import Path
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from .downloads import ProgressTracker, download_all
from .net import CREATE_NO_WINDOW, current_arch, current_os_name, http_get_json, is_within
from .paths import RUNTIME_DIR, settings_store



JAVA_RUNTIME_INDEX_URL = ("https://launchermeta.mojang.com/v1/products/java-runtime/"
                          "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json")
DEFAULT_JAVA_INFO = {"component": "jre-legacy", "majorVersion": 8}
_JAVA_PROBE_CACHE: dict = {}
_JAVA_PROBE_LOCK = threading.Lock()
_JAVA_UI_CACHE = {"at": 0.0, "value": (None, None)}
def console_java(path: str) -> str:

    p = Path(path)
    if p.name.lower() == "javaw.exe":
        sibling = p.with_name("java.exe")
        if sibling.exists():
            return str(sibling)
    return str(path)
def _parse_java_major(text: str):
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if m:
        major = int(m.group(1))
        if major == 1 and m.group(2):
            major = int(m.group(2))
        return major
    return None
def probe_java(path: str):

    exe = console_java(path)
    with _JAVA_PROBE_LOCK:
        if exe in _JAVA_PROBE_CACHE:
            return _JAVA_PROBE_CACHE[exe]
    result = (None, None)
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, errors="replace",
            timeout=15, creationflags=CREATE_NO_WINDOW,
        )
        text = (proc.stderr or proc.stdout or "")
        lines = text.splitlines()
        result = (_parse_java_major(text), lines[0] if lines else None)
    except Exception:
        result = (None, None)
    with _JAVA_PROBE_LOCK:
        _JAVA_PROBE_CACHE[exe] = result
    return result
def java_compatible(major, required: int) -> bool:
    if major is None:
        return True
    if required <= 8:
        return major == 8
    return major >= required
def _java_exe_names():
    return ("java.exe",) if current_os_name() == "windows" else ("java",)
def find_system_javas() -> list:

    found = []
    seen = set()

    def add(p):
        try:
            p = Path(p)
            if not p.is_file():
                return
            key = str(p.resolve())
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        found.append(str(p))

    for exe in ("java", "java.exe"):
        w = shutil.which(exe)
        if w:
            add(w)

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        for n in _java_exe_names():
            add(Path(java_home) / "bin" / n)

    home = Path.home()
    system = current_os_name()
    globs = []
    if system == "windows":
        vendors = ["Java", "Eclipse Adoptium", "Eclipse Foundation", "Microsoft", "Zulu",
                   "BellSoft", "Amazon Corretto", "Semeru", "AdoptOpenJDK", "OpenJDK"]
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                for v in vendors:
                    globs.append((Path(base) / v, "*/bin"))
        appdata = os.environ.get("APPDATA")
        if appdata:
            globs.append((Path(appdata) / ".minecraft" / "runtime", "*/*/*/bin"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            globs.append((Path(local) / "Packages", "Microsoft.4297127D64EC6_*/LocalCache/Local/runtime/*/*/*/bin"))
    elif system == "osx":
        globs.append((Path("/Library/Java/JavaVirtualMachines"), "*/Contents/Home/bin"))
        globs.append((home / "Library/Application Support/minecraft/runtime", "*/*/*/jre.bundle/Contents/Home/bin"))
    else:
        globs.append((Path("/usr/lib/jvm"), "*/bin"))
        globs.append((home / ".sdkman/candidates/java", "*/bin"))
        globs.append((home / ".jdks", "*/bin"))
        globs.append((home / ".minecraft/runtime", "*/*/*/bin"))

    for base, pattern in globs:
        try:
            if not base.exists():
                continue
            for bin_dir in base.glob(pattern):
                for n in _java_exe_names():
                    add(bin_dir / n)
        except OSError:
            pass
    return found
def mojang_runtime_platform() -> str:
    system = current_os_name()
    arch = current_arch()
    if system == "windows":
        return {"64": "windows-x64", "32": "windows-x86", "arm64": "windows-arm64"}.get(arch, "windows-x64")
    if system == "osx":
        return "mac-os-arm64" if arch == "arm64" else "mac-os"
    return "linux-i386" if arch == "32" else "linux"
def managed_java_path(component: str):
    root = RUNTIME_DIR / component
    if not (root / ".loadstone_installed").exists():
        return None
    for rel in ("bin/java.exe", "bin/java", "jre.bundle/Contents/Home/bin/java"):
        p = root / rel
        if p.exists():
            return str(p)
    return None
def install_managed_java(component: str, tracker):

    tracker = tracker or ProgressTracker(lambda *_: None)
    tracker.set_task("Looking up Java runtime", force=True)
    plat = mojang_runtime_platform()
    index = http_get_json(JAVA_RUNTIME_INDEX_URL, timeout=30)
    entries = (index.get(plat) or {}).get(component) or []
    if not entries:
        raise RuntimeError(
            f"Mojang has no '{component}' Java runtime for {plat}. Install a matching Java "
            "yourself and set its path in Settings.")
    manifest = http_get_json(entries[0]["manifest"]["url"], timeout=30)
    files = manifest.get("files", {})

    root = RUNTIME_DIR / component
    root.mkdir(parents=True, exist_ok=True)

    items, executables, links = [], [], []
    total = 0
    for rel, info in files.items():
        target = root / rel
        if not is_within(root, target):
            continue
        kind = info.get("type")
        if kind == "directory":
            target.mkdir(parents=True, exist_ok=True)
        elif kind == "file":
            raw = (info.get("downloads") or {}).get("raw")
            if not raw:
                continue
            items.append(({"path": rel, "url": raw["url"], "size": raw.get("size"),
                           "sha1": raw.get("sha1")}, root))
            total += raw.get("size") or 0
            if info.get("executable"):
                executables.append(target)
        elif kind == "link":
            links.append((target, info.get("target")))

    tracker.reset(max(total, 1))
    tracker.set_task(f"Downloading Java runtime ({component})", force=True)
    download_all(items, tracker, workers=12)

    if current_os_name() != "windows":
        for target in executables:
            try:
                os.chmod(target, 0o755)
            except OSError:
                pass
        for target, link_to in links:
            try:
                if link_to and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.symlink(link_to, target)
            except OSError:
                pass

    (root / ".loadstone_installed").write_text(str(time.time()), encoding="utf-8")
    path = managed_java_path(component)
    if not path:
        raise RuntimeError("Java runtime was downloaded but no java executable was found in it.")
    return path
def java_info_from_meta(meta: dict) -> dict:
    return {
        "component": meta.get("java_component") or DEFAULT_JAVA_INFO["component"],
        "majorVersion": int(meta.get("java_major") or DEFAULT_JAVA_INFO["majorVersion"]),
    }
def resolve_java(java_info, allow_download=False, tracker=None):



    info = java_info or DEFAULT_JAVA_INFO
    required = int(info.get("majorVersion") or 8)
    component = info.get("component") or "jre-legacy"

    configured = (settings_store.read().get("java_path") or "").strip()
    if configured and Path(configured).exists():
        major, _ = probe_java(configured)
        if java_compatible(major, required):
            return configured
        logging.info("Configured Java (%s) is not compatible with Java %s", major, required)

    managed = managed_java_path(component)
    if managed:
        return managed

    best = None
    for cand in find_system_javas():
        major, _ = probe_java(cand)
        if major is None or not java_compatible(major, required):
            continue
        if best is None or major < best[0]:
            best = (major, cand)
    if best:
        return best[1]

    if allow_download:
        return install_managed_java(component, tracker)
    return None
def detect_java_for_ui():

    now = time.time()
    if now - _JAVA_UI_CACHE["at"] < 20 and _JAVA_UI_CACHE["at"] > 0:
        return _JAVA_UI_CACHE["value"]
    value = (None, None)
    configured = (settings_store.read().get("java_path") or "").strip()
    if configured and Path(configured).exists():
        value = (configured, probe_java(configured)[1])
    else:
        best = None
        for cand in find_system_javas():
            major, line = probe_java(cand)
            if major is not None and (best is None or major > best[0]):
                best = (major, cand, line)
        if best:
            value = (best[1], best[2])
    _JAVA_UI_CACHE["at"] = now
    _JAVA_UI_CACHE["value"] = value
    return value
