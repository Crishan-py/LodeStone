"""Version metadata cache and version-list helpers."""
from pathlib import Path
import json
import os
import re
import threading
import urllib.request
import urllib.parse
import urllib.error
from .net import FABRIC_GAME_LIST_URL, FABRIC_LOADER_LIST_URL, FABRIC_PROFILE_URL_TMPL, FORGE_METADATA_URL, VERSION_MANIFEST_URL, http_get_json, http_get_text
from .paths import VERSIONS_DIR, log_exception



class MetaCache:
    def __init__(self):
        self._lock = threading.Lock()
        self.manifest = None
        self.fabric_loaders = None
        self.fabric_loaders_by_mc = {}
        self.fabric_games = None
        self.forge_all = None

    def get_manifest(self, force=False):
        with self._lock:
            if self.manifest is not None and not force:
                return self.manifest
        manifest = None
        try:
            manifest = http_get_json(VERSION_MANIFEST_URL)
        except Exception:
            log_exception("Failed to download version manifest")
            cache_file = VERSIONS_DIR / "version_manifest_v2.json"
            if cache_file.exists():
                try:
                    manifest = json.loads(cache_file.read_text(encoding="utf-8"))
                except Exception:
                    log_exception("Failed to read cached manifest")
        if manifest is None:
            raise RuntimeError("Failed to load versions.")
        try:
            (VERSIONS_DIR / "version_manifest_v2.json").write_text(
                json.dumps(manifest), encoding="utf-8")
        except OSError:
            pass
        with self._lock:
            self.manifest = manifest
        return manifest


    def get_fabric_loaders(self, mc_version=None, force=False):
        with self._lock:
            if mc_version:
                if mc_version in self.fabric_loaders_by_mc and not force:
                    return self.fabric_loaders_by_mc[mc_version]
            elif self.fabric_loaders is not None and not force:
                return self.fabric_loaders
        try:
            if mc_version:
                try:
                    loaders = http_get_json(
                        f"{FABRIC_LOADER_LIST_URL}/{urllib.parse.quote(mc_version)}")
                except urllib.error.HTTPError:
                    loaders = []
            else:
                loaders = http_get_json(FABRIC_LOADER_LIST_URL)
        except Exception:
            log_exception("Failed to download fabric loader list")
            raise RuntimeError("Fabric loader could not be retrieved.")
        with self._lock:
            if mc_version:
                self.fabric_loaders_by_mc[mc_version] = loaders
            else:
                self.fabric_loaders = loaders
        return loaders

    def get_fabric_game_versions(self, force=False):
        with self._lock:
            if self.fabric_games is not None and not force:
                return self.fabric_games
        try:
            games = http_get_json(FABRIC_GAME_LIST_URL)
        except Exception:
            log_exception("Failed to download fabric game list")
            raise RuntimeError("Fabric versions could not be retrieved.")
        versions = [g.get("version") for g in games if g.get("version")]
        with self._lock:
            self.fabric_games = versions
        return versions

    def get_fabric_profile(self, mc_version: str, loader_version: str) -> dict:
        cache_path = VERSIONS_DIR / f"fabric-{loader_version}-{mc_version}.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        url = FABRIC_PROFILE_URL_TMPL.format(
            mc=urllib.parse.quote(mc_version), loader=urllib.parse.quote(loader_version))
        data = http_get_json(url)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data


    def _forge_all_versions(self, force=False):
        with self._lock:
            if self.forge_all is not None and not force:
                return self.forge_all
        xml_text = http_get_text(FORGE_METADATA_URL)
        all_versions = re.findall(r"<version>([^<]+)</version>", xml_text)
        with self._lock:
            self.forge_all = all_versions
        return all_versions

    def get_forge_versions(self, mc_version: str, force=False):
        all_versions = self._forge_all_versions(force)
        prefix = mc_version + "-"
        matches = [v[len(prefix):] for v in all_versions if v.startswith(prefix)]
        matches.reverse()
        return matches

    def get_forge_mc_versions(self, force=False):
        seen, out = set(), []
        for v in self._forge_all_versions(force):
            mc = v.split("-", 1)[0]
            if mc and mc not in seen:
                seen.add(mc)
                out.append(mc)
        return out


    def get_version_json(self, version_id: str, url: str) -> dict:
        cache_path = VERSIONS_DIR / version_id / f"{version_id}.json"
        if cache_path.exists():
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        data = http_get_json(url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data
meta_cache = MetaCache()
_meta_lock = threading.RLock()
def _dir_size(path: Path) -> int:

    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
def pick_best_version(versions: list):

    if not versions:
        return None
    for wanted in ("release", "beta", "alpha"):
        for v in versions:
            if v.get("version_type") == wanted:
                return v
    return versions[0]
def primary_file(version: dict):
    files = version.get("files", [])
    return next((f for f in files if f.get("primary")), files[0] if files else None)
def _version_tuple(tag) -> tuple:

    nums = [int(n) for n in re.findall(r"\d+", str(tag or ""))]
    while len(nums) > 1 and nums[-1] == 0:
        nums.pop()
    return tuple(nums) or (0,)
