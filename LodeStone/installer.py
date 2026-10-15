"""Forge and Fabric installation."""
from pathlib import Path
import json
import logging
import re
import shutil
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
import zipfile
from .downloads import ProgressTracker, _jar_main_class, collect_library_entries, download_all, entry_weight, extract_natives, maven_to_path
from .instances import INSTALL_SCHEMA, ensure_game_layout, instance_dir, read_instance_meta, write_instance_meta
from .java import DEFAULT_JAVA_INFO, console_java, resolve_java
from .meta import meta_cache
from .net import CREATE_NO_WINDOW, FORGE_MAVEN, RESOURCES_BASE_URL, classpath_sep, file_sha1, http_download, http_get_json, is_within, sanitize_instance_name
from .paths import ASSET_INDEXES_DIR, ASSET_OBJECTS_DIR, DATA_DIR, LIBRARIES_DIR, TMP_DIR, VERSIONS_DIR, log_exception, settings_store
from .rules import merge_arguments



class ForgeInstallContext:



    def __init__(self, profile: dict, installer_jar: Path, tmp_dir: Path,
                 mc_version: str, client_path: Path):
        self.profile = profile
        self.installer_jar = installer_jar
        self.tmp_dir = tmp_dir
        self.tokens = {
            "SIDE": "client",
            "MINECRAFT_JAR": str(client_path),
            "MINECRAFT_VERSION": mc_version,
            "ROOT": str(DATA_DIR),
            "INSTALLER": str(installer_jar),
            "LIBRARY_DIR": str(LIBRARIES_DIR),
        }
        for key, val in (profile.get("data") or {}).items():
            client_val = val.get("client") if isinstance(val, dict) else val
            if client_val is not None:
                self.tokens[key] = self._data_value(str(client_val))

    def _data_value(self, value: str) -> str:
        if value.startswith("[") and value.endswith("]"):
            return str(LIBRARIES_DIR / maven_to_path(value[1:-1]))
        if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            return value[1:-1]
        if value.startswith("/"):
            member = value.lstrip("/")
            dest = self.tmp_dir / member
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(self.installer_jar) as zf:
                    dest.write_bytes(zf.read(member))
            return str(dest)
        return value

    def resolve(self, arg: str) -> str:
        if arg.startswith("[") and arg.endswith("]"):
            return str(LIBRARIES_DIR / maven_to_path(arg[1:-1]))
        out = re.sub(r"\{([A-Za-z0-9_]+)\}",
                     lambda m: self.tokens.get(m.group(1), m.group(0)), arg)
        if len(out) >= 2 and out.startswith("'") and out.endswith("'"):
            out = out[1:-1]
        return out

    def outputs_valid(self, proc: dict) -> bool:
        outputs = proc.get("outputs") or {}
        if not outputs:
            return False
        for key, expected in outputs.items():
            path = Path(self.resolve(key))
            want = self.resolve(expected)
            if not path.is_file():
                return False
            try:
                if file_sha1(path) != want:
                    return False
            except OSError:
                return False
        return True
class Installer:
    def __init__(self, api: "API", meta: dict):
        self.api = api
        self.name = sanitize_instance_name(meta["name"])
        self.version_id = meta["version"]
        self.loader = meta.get("loader") or "vanilla"
        self.loader_version = meta.get("loader_version")

    def run(self) -> bool:
        emit = self.api.emit
        forge_tmp = None
        try:
            emit("installStarted", {"instance": self.name,
                                    "title": f"Installing {self.name}..."})

            target_dir = instance_dir(self.name)
            target_dir.mkdir(parents=True, exist_ok=True)

            tracker = ProgressTracker(emit)
            tracker.set_task("Fetching version metadata", force=True)

            manifest = meta_cache.get_manifest()
            version_entry = next(
                (v for v in manifest["versions"] if v["id"] == self.version_id), None)
            if not version_entry:
                raise RuntimeError(f"Minecraft {self.version_id} was not found in Mojang's version list.")
            vanilla_json = meta_cache.get_version_json(self.version_id, version_entry["url"])

            java_info = vanilla_json.get("javaVersion") or dict(DEFAULT_JAVA_INFO)
            java_path = resolve_java(java_info, allow_download=True, tracker=tracker)
            if not java_path:
                raise RuntimeError("Could not find or download a suitable Java runtime.")

            asset_index_info = vanilla_json.get("assetIndex", {})
            asset_index_id = asset_index_info.get("id", self.version_id)
            asset_index_path = ASSET_INDEXES_DIR / f"{asset_index_id}.json"
            tracker.set_task("Fetching asset index", force=True)
            if asset_index_path.exists():
                asset_index_data = json.loads(asset_index_path.read_text(encoding="utf-8"))
            elif asset_index_info.get("url"):
                asset_index_data = http_get_json(asset_index_info["url"])
                asset_index_path.write_text(json.dumps(asset_index_data), encoding="utf-8")
            else:
                asset_index_data = {"objects": {}}
            asset_objects = asset_index_data.get("objects", {})

            if self.loader == "fabric":
                plan = self._plan_fabric(vanilla_json, tracker)
            elif self.loader == "forge":
                plan, forge_tmp = self._plan_forge(vanilla_json, tracker)
            else:
                plan = self._plan_vanilla(vanilla_json)

            cp_entries, native_entries = collect_library_entries(plan["libraries"])

            client_dl = vanilla_json.get("downloads", {}).get("client", {})
            client_entry = {
                "path": f"{self.version_id}/{self.version_id}.jar",
                "url": client_dl.get("url", ""),
                "size": client_dl.get("size"),
                "sha1": client_dl.get("sha1"),
            }

            asset_items, seen_hashes = [], set()
            for _name, obj in asset_objects.items():
                obj_hash = obj.get("hash")
                if not obj_hash or obj_hash in seen_hashes:
                    continue
                seen_hashes.add(obj_hash)
                prefix = obj_hash[:2]
                asset_items.append(({
                    "path": f"{prefix}/{obj_hash}",
                    "url": f"{RESOURCES_BASE_URL}/{prefix}/{obj_hash}",
                    "size": obj.get("size"),
                    "sha1": obj_hash,
                    "quick": True,
                }, ASSET_OBJECTS_DIR))

            lib_items = [(e, LIBRARIES_DIR)
                         for e in cp_entries + native_entries + plan.get("installer_entries", [])]

            total = entry_weight(client_entry)
            total += sum(entry_weight(e) for e, _ in lib_items)
            total += sum(int(e.get("size") or 0) for e, _ in asset_items)
            tracker.reset(total)

            tracker.set_task("Downloading client", force=True)
            download_all([(client_entry, VERSIONS_DIR)], tracker)
            client_path = VERSIONS_DIR / client_entry["path"]

            tracker.set_task("Downloading libraries", force=True)
            download_all(lib_items, tracker)

            tracker.set_task("Downloading assets", force=True)
            download_all(asset_items, tracker, workers=16)

            post = plan.get("post")
            if post:
                post(client_path, java_path, tracker)

            missing = [e["path"] for e in cp_entries
                       if not (LIBRARIES_DIR / e["path"]).is_file()]
            missing += [e["path"] for e in native_entries
                        if not (LIBRARIES_DIR / e["path"]).is_file()]
            if missing:
                raise RuntimeError(
                    "Some files are still missing after install: " + ", ".join(missing[:3]))

            tracker.set_task("Extracting natives", force=True)
            natives_dir = target_dir / "natives"
            extract_natives(
                [(LIBRARIES_DIR / e["path"], e.get("extract_exclude", [])) for e in native_entries],
                natives_dir)

            tracker.set_task("Finalizing instance", force=True)
            ensure_game_layout(self.name, self.loader)

            classpath = ["libraries/" + e["path"] for e in cp_entries]
            classpath.append(f"versions/{client_entry['path']}")

            meta = read_instance_meta(self.name) or {}
            meta.update({
                "name": self.name,
                "version": self.version_id,
                "loader": self.loader,
                "loader_version": self.loader_version,
                "main_class": plan["main_class"],
                "asset_index": asset_index_id,
                "arguments": plan.get("arguments"),
                "minecraft_arguments": plan.get("minecraft_arguments"),
                "classpath": classpath,
                "java_component": java_info.get("component", DEFAULT_JAVA_INFO["component"]),
                "java_major": int(java_info.get("majorVersion", DEFAULT_JAVA_INFO["majorVersion"])),
                "installed": True,
                "schema": INSTALL_SCHEMA,
                "created": meta.get("created", time.time()),
            })
            write_instance_meta(self.name, meta)

            def _set_active(s):
                if not s.get("active_instance"):
                    s["active_instance"] = self.name
            settings_store.update(_set_active)

            emit("installFinished", {"instance": self.name})
            self.api.push_state()
            return True

        except Exception as exc:
            log_exception(f"Install failed for {self.name}")
            emit("installError", {"message": str(exc) or "Installation failed."})
            return False
        finally:
            if forge_tmp:
                shutil.rmtree(forge_tmp, ignore_errors=True)


    def _plan_vanilla(self, vanilla_json):
        return {
            "main_class": vanilla_json["mainClass"],
            "libraries": list(vanilla_json.get("libraries", [])),
            "arguments": merge_arguments(vanilla_json.get("arguments"), None),
            "minecraft_arguments": vanilla_json.get("minecraftArguments"),
        }


    def _plan_fabric(self, vanilla_json, tracker):
        if not self.loader_version:
            raise RuntimeError("No Fabric loader version selected.")
        tracker.set_task("Fetching Fabric metadata", force=True)
        try:
            profile = meta_cache.get_fabric_profile(self.version_id, self.loader_version)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Fabric loader {self.loader_version} isn't available for Minecraft "
                f"{self.version_id} (HTTP {exc.code}).") from exc

        main_class = profile.get("mainClass") or vanilla_json["mainClass"]
        if isinstance(main_class, dict):
            main_class = main_class.get("client") or vanilla_json["mainClass"]

        return {
            "main_class": main_class,
            "libraries": list(profile.get("libraries", [])) + list(vanilla_json.get("libraries", [])),
            "arguments": merge_arguments(vanilla_json.get("arguments"), profile.get("arguments")),
            "minecraft_arguments": profile.get("minecraftArguments") or vanilla_json.get("minecraftArguments"),
        }


    def _plan_forge(self, vanilla_json, tracker):
        if not self.loader_version:
            raise RuntimeError("No Forge version selected.")
        mc_version = self.version_id
        full_version = f"{mc_version}-{self.loader_version}"

        installer_jar = VERSIONS_DIR / f"forge-installer-{full_version}.jar"
        installer_url = f"{FORGE_MAVEN}{full_version}/forge-{full_version}-installer.jar"

        tracker.set_task("Downloading Forge installer", force=True)
        tracker.reset(8_000_000)
        if installer_jar.exists() and not zipfile.is_zipfile(installer_jar):
            installer_jar.unlink(missing_ok=True)
        if not installer_jar.exists():
            try:
                http_download(installer_url, installer_jar, progress_cb=tracker.add)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    f"Forge {self.loader_version} for Minecraft {mc_version} could not be "
                    f"downloaded (HTTP {exc.code}).") from exc

        tmp_dir = TMP_DIR / f"forge-{full_version}"
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(installer_jar) as zf:
            profile = json.loads(zf.read("install_profile.json").decode("utf-8"))
            legacy = "versionInfo" in profile
            if legacy:
                version_json = profile["versionInfo"]
            else:
                json_member = (profile.get("json") or "/version.json").lstrip("/")
                version_json = json.loads(zf.read(json_member).decode("utf-8"))

            for member in zf.namelist():
                if member.endswith("/") or not member.startswith("maven/"):
                    continue
                dest = LIBRARIES_DIR / member[len("maven/"):]
                if not is_within(LIBRARIES_DIR, dest):
                    continue
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(member))

            if legacy:
                install = profile.get("install", {})
                file_path, art = install.get("filePath"), install.get("path")
                if file_path and art:
                    dest = LIBRARIES_DIR / maven_to_path(art)
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(zf.read(file_path))

        installer_entries = []
        if not legacy:
            installer_entries, _ = collect_library_entries(list(profile.get("libraries", [])))

        plan = {
            "main_class": version_json.get("mainClass", vanilla_json["mainClass"]),
            "libraries": list(version_json.get("libraries", [])) + list(vanilla_json.get("libraries", [])),
            "arguments": merge_arguments(vanilla_json.get("arguments"), version_json.get("arguments")),
            "minecraft_arguments": version_json.get("minecraftArguments") or vanilla_json.get("minecraftArguments"),
            "installer_entries": installer_entries,
        }

        if not legacy and profile.get("processors"):
            def post(client_path, java_path, trk):
                self._run_forge_processors(profile, installer_jar, tmp_dir, mc_version,
                                           client_path, java_path, trk)
            plan["post"] = post

        return plan, tmp_dir

    def _run_forge_processors(self, profile, installer_jar, tmp_dir, mc_version,
                              client_path, java_path, tracker):
        ctx = ForgeInstallContext(profile, installer_jar, tmp_dir, mc_version, client_path)
        procs = [p for p in profile.get("processors", [])
                 if not p.get("sides") or "client" in p["sides"]]
        if not procs:
            return

        java_exe = console_java(java_path)
        tracker.reset(len(procs), unit="steps")

        for index, proc in enumerate(procs, 1):
            tracker.set_task(f"Setting up Forge ({index}/{len(procs)})", force=True)

            if ctx.outputs_valid(proc):
                tracker.add(1)
                continue

            jar_rel = maven_to_path(proc["jar"])
            jar_path = LIBRARIES_DIR / jar_rel
            if not jar_path.is_file():
                raise RuntimeError(f"Missing Forge tool: {proc['jar']}")

            cp_parts = [str(jar_path)]
            for coord in proc.get("classpath", []):
                cp = LIBRARIES_DIR / maven_to_path(coord)
                if not cp.is_file():
                    raise RuntimeError(f"Missing Forge tool library: {coord}")
                cp_parts.append(str(cp))

            main_class = _jar_main_class(jar_path)
            if not main_class:
                raise RuntimeError(f"Forge tool has no main class: {proc['jar']}")

            args = [ctx.resolve(a) for a in proc.get("args", [])]
            cmd = [java_exe, "-cp", classpath_sep().join(cp_parts), main_class] + args
            logging.info("Forge processor %s/%s: %s", index, len(procs), " ".join(cmd)[:1500])

            result = subprocess.run(
                cmd, cwd=str(tmp_dir), capture_output=True, text=True, errors="replace",
                timeout=1200, creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                tail = ((result.stderr or "") + (result.stdout or ""))[-800:].strip()
                logging.error("Forge processor failed (%s): %s", main_class, tail)
                raise RuntimeError(f"Forge setup step failed ({main_class.split('.')[-1]}): {tail}")
            tracker.add(1)
