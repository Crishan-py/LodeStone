"""Data locations, logging and the JSON-backed stores."""
from pathlib import Path
import json
import logging
import os
import sys
import threading
import traceback



def _resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent
def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
RESOURCE_DIR = _resource_dir()
APP_DIR = _app_dir()
DATA_DIR = Path(os.environ.get("LOADSTONE_DATA", str(APP_DIR / "loadstone_data")))
INSTANCES_DIR = DATA_DIR / "instances"
VERSIONS_DIR = DATA_DIR / "versions"
LIBRARIES_DIR = DATA_DIR / "libraries"
ASSETS_DIR = DATA_DIR / "assets"
ASSET_OBJECTS_DIR = ASSETS_DIR / "objects"
ASSET_INDEXES_DIR = ASSETS_DIR / "indexes"
RUNTIME_DIR = DATA_DIR / "runtime"
TMP_DIR = DATA_DIR / "tmp"
SKINS_DIR = DATA_DIR / "skins"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "launcher.log"
for _d in (DATA_DIR, INSTANCES_DIR, VERSIONS_DIR, LIBRARIES_DIR, ASSETS_DIR,
           ASSET_OBJECTS_DIR, ASSET_INDEXES_DIR, RUNTIME_DIR, TMP_DIR, SKINS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
def log_exception(context: str):
    logging.error("%s\n%s", context, traceback.format_exc())
class JsonStore:
    def __init__(self, path: Path, default):
        self.path = path
        self._lock = threading.RLock()
        self._default = default
        if not self.path.exists():
            self._write(default)

    def _write(self, data):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            return json.loads(json.dumps(self._default))

    def read(self):
        with self._lock:
            return self._read()

    def write(self, data):
        with self._lock:
            self._write(data)

    def update(self, mutator):


        with self._lock:
            data = self._read()
            result = mutator(data)
            if result is not None:
                data = result
            self._write(data)
            return data
accounts_store = JsonStore(ACCOUNTS_FILE, [])
THEMES = ("green", "blue", "purple", "red", "orange", "pink")
UI_STYLES = ("default", "minecraft")
settings_store = JsonStore(SETTINGS_FILE, {
    "active_account": None,
    "active_instance": None,
    "memory_mb": 2048,
    "java_path": None,
    "ms_warning_seen": False,
    "sidebar_collapsed": False,
    "theme": "green",
    "ui_style": "default",
    "server_beta_seen": False,
    "local_skin_warning_seen": [],
    "playtime_seconds": 0,
})
