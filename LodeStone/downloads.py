"""Library/asset resolution, progress reporting and fetching."""
from pathlib import Path
import re
import shutil
import threading
import time
import zipfile
from .net import LIBRARIES_BASE_URL, current_arch, current_os_name, file_is_valid, http_download, is_within
from .paths import log_exception
from .rules import rules_allow



class ProgressTracker:
    def __init__(self, emit_fn, total_estimate: int = 1):
        self.emit_fn = emit_fn
        self.event = "installProgress"
        self.lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self.total = max(total_estimate, 1)
        self.downloaded = 0
        self.unit = "bytes"
        self.current_task = "Preparing"
        self._last_emit = 0.0

    def reset(self, total: int, unit: str = "bytes"):
        with self.lock:
            self.total = max(int(total), 1)
            self.downloaded = 0
            self.unit = unit
        self._emit(force=True)

    def set_total(self, total: int):
        with self.lock:
            self.total = max(total, 1)

    def set_task(self, task: str, force=False):
        with self.lock:
            self.current_task = task
        self._emit(force=True)

    def add(self, n: int):
        with self.lock:
            self.downloaded += n
        self._emit()

    def _emit(self, force=False):
        if force:
            self._emit_lock.acquire()
        elif not self._emit_lock.acquire(False):
            return
        try:
            now = time.time()
            if not force and now - self._last_emit < 0.08:
                return
            self._last_emit = now
            with self.lock:
                percent = min(100, int(self.downloaded * 100 / self.total))
                payload = {
                    "percent": percent,
                    "current": self.current_task,
                    "downloaded": self.downloaded,
                    "total": self.total,
                    "unit": self.unit,
                }
            self.emit_fn(self.event, payload)
        finally:
            self._emit_lock.release()
def maven_to_path(name: str) -> str:
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    ext = "jar"
    if "@" in version:
        version, ext = version.split("@", 1)
    if classifier and "@" in classifier:
        classifier, ext = classifier.split("@", 1)
    group_path = group.replace(".", "/")
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{ext}"
    return f"{group_path}/{artifact}/{version}/{filename}"
def lib_key(name: str) -> str:


    parts = name.split(":")
    if len(parts) < 3:
        return name
    key = f"{parts[0]}:{parts[1]}"
    if len(parts) > 3:
        key += ":" + parts[3].split("@", 1)[0]
    return key
def fix_maven_url(url: str) -> str:
    url = url.replace("http://files.minecraftforge.net/maven/", "https://maven.minecraftforge.net/")
    if url.startswith("http://") and not url.startswith(("http://127.0.0.1", "http://localhost")):
        url = "https://" + url[len("http://"):]
    if not url.endswith("/"):
        url += "/"
    return url
def collect_library_entries(libraries: list, features=None):
    classpath_entries = []
    native_entries = []
    features = features or {}
    seen_keys = set()
    seen_native_paths = set()

    for lib in libraries:
        if lib.get("clientreq") is False:
            continue
        if "rules" in lib and not rules_allow(lib["rules"], features):
            continue

        name = lib.get("name", "")
        downloads = lib.get("downloads") or {}
        artifact = downloads.get("artifact")

        if artifact and artifact.get("path"):
            key = lib_key(name) if name else artifact["path"]
            if key not in seen_keys:
                seen_keys.add(key)
                classpath_entries.append({
                    "path": artifact["path"],
                    "url": artifact.get("url", ""),
                    "size": artifact.get("size"),
                    "sha1": artifact.get("sha1"),
                })
        elif name and not downloads.get("classifiers"):
            key = lib_key(name)
            if key not in seen_keys:
                seen_keys.add(key)
                path = maven_to_path(name)
                base_url = fix_maven_url(lib.get("url") or LIBRARIES_BASE_URL)
                classpath_entries.append({
                    "path": path,
                    "url": base_url + path,
                    "size": lib.get("size"),
                    "sha1": lib.get("sha1"),
                })

        natives_map = lib.get("natives")
        if natives_map:
            classifier_key = natives_map.get(current_os_name())
            if classifier_key:
                classifier_key = classifier_key.replace("${arch}", current_arch())
                classifiers = downloads.get("classifiers", {})
                native_artifact = classifiers.get(classifier_key)
                if native_artifact and native_artifact.get("path"):
                    if native_artifact["path"] in seen_native_paths:
                        continue
                    seen_native_paths.add(native_artifact["path"])
                    exclude = lib.get("extract", {}).get("exclude", [])
                    native_entries.append({
                        "path": native_artifact["path"],
                        "url": native_artifact.get("url", ""),
                        "size": native_artifact.get("size"),
                        "sha1": native_artifact.get("sha1"),
                        "extract_exclude": exclude,
                    })

    return classpath_entries, native_entries
def entry_weight(entry: dict) -> int:
    return int(entry.get("size") or 300_000)
def ensure_file(entry: dict, dest_root: Path, tracker, retries: int = 3):
    dest = dest_root / entry["path"]
    quick_sha = None if entry.get("quick") else entry.get("sha1")
    if file_is_valid(dest, entry.get("size"), quick_sha):
        return dest
    if not entry.get("url"):
        raise RuntimeError(f"Failed to download library: {entry['path']} (no URL)")

    def cb(n):
        if tracker:
            tracker.add(n)

    last = None
    for attempt in range(retries):
        try:
            http_download(entry["url"], dest, entry.get("size"), entry.get("sha1"), cb)
            return dest
        except Exception as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"Failed to download {entry['path']} ({last})") from last
def download_all(items, tracker, workers: int = 10):



    todo, missing, seen = [], [], set()
    for entry, root in items:
        dest = root / entry["path"]
        key = str(dest)
        if key in seen:
            continue
        seen.add(key)
        quick_sha = None if entry.get("quick") else entry.get("sha1")
        if file_is_valid(dest, entry.get("size"), quick_sha):
            tracker.add(entry_weight(entry))
            continue
        if not entry.get("url"):
            missing.append(entry)
            continue
        todo.append((entry, root))

    if not todo:
        return missing
    if len(todo) == 1 or workers <= 1:
        for entry, root in todo:
            ensure_file(entry, root, tracker)
        return missing

    with ThreadPoolExecutor(max_workers=min(workers, len(todo))) as pool:
        futures = [pool.submit(ensure_file, e, r, tracker) for e, r in todo]
        try:
            for fut in as_completed(futures):
                fut.result()
        except Exception:
            for fut in futures:
                fut.cancel()
            raise
    return missing
def extract_natives(native_paths_and_excludes, natives_dir: Path):
    shutil.rmtree(natives_dir, ignore_errors=True)
    natives_dir.mkdir(parents=True, exist_ok=True)
    for jar_path, exclude in native_paths_and_excludes:
        try:
            with zipfile.ZipFile(jar_path, "r") as zf:
                for member in zf.namelist():
                    if member.startswith("META-INF/") or member.endswith("/"):
                        continue
                    if any(member.startswith(ex.rstrip("/") + "/") or member == ex
                           for ex in exclude):
                        continue
                    if not is_within(natives_dir, natives_dir / member):
                        continue
                    zf.extract(member, natives_dir)
        except zipfile.BadZipFile:
            log_exception(f"Bad native jar: {jar_path}")
def _jar_main_class(jar_path: Path) -> str | None:
    try:
        with zipfile.ZipFile(jar_path) as zf:
            manifest = zf.read("META-INF/MANIFEST.MF").decode("utf-8", "ignore")
            manifest = manifest.replace("\r\n ", "").replace("\n ", "")
            m = re.search(r"Main-Class:\s*(\S+)", manifest)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None
