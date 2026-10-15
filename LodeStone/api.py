"""The RPC surface the UI talks to."""
from pathlib import Path
import base64
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
import uuid
import zipfile
from .catalog import ADDON_TYPES, _catalog_cached, _catalog_error, _catalog_tree, _catalog_url_ok, _catalog_walk, list_instance_content, read_content_icon
from .downloads import ProgressTracker, download_all, entry_weight
from .importer import _copy_tree_contents, _find_scanned_instance, _source_paths, scan_all_launchers
from .installer import Installer
from .instances import APP_VERSION, CHANGELOG_CACHE, CHANGELOG_REPO, INSTALL_SCHEMA, INSTANCE_ICONS, _changelog_mem, ensure_game_layout, find_account, instance_config_dir, instance_dir, instance_game_dir, instance_mods_dir, is_installed, list_instances, public_account, read_instance_meta, unique_instance_name, update_instance_meta, write_instance_meta
from .java import _JAVA_UI_CACHE, console_java, detect_java_for_ui, java_info_from_meta, resolve_java
from .launch import _explain_crash, _log_tail, build_launch_command
from .meta import _dir_size, _version_tuple, meta_cache, pick_best_version, primary_file
from .net import CREATE_NO_WINDOW, MODRINTH_API, USER_AGENT, http_download, http_get_json, http_get_text, is_within, offline_uuid, open_in_file_manager, sanitize_instance_name
from .paths import DATA_DIR, INSTANCES_DIR, SKINS_DIR, THEMES, TMP_DIR, UI_STYLES, accounts_store, log_exception, settings_store
from .servers import SERVERS_DIR, ServerMixin
from .skins import CSL_PROJECT, CSL_RUN_ONCE_MSG, MOJANG_CAPES, PNG_SIG, _CAPE_BY_ID, _cape_url, _csl_find_config, _csl_installed, _csl_local_dir, _csl_write_config, _mod_present, _write_bytes_atomic, decode_png_uri, fetch_texture_uri, list_saved_skins, png_uri, skins_index



class API(ServerMixin):
    def __init__(self):
        self._bus = None
        self._quit = None
        self._processes: dict = {}
        self._sessions: dict = {}
        self._stopping: set = set()
        self._installing: set = set()
        self._addon_busy = False
        self._importing: set = set()
        self._proc_lock = threading.Lock()
        self._launcher_started = time.time()
        self._srv_init()

    def _attach(self, bus, quit_callback):
        self._bus = bus
        self._quit = quit_callback

    def emit(self, name: str, data=None):
        if self._bus is None:
            return
        try:
            self._bus.publish(name, data if data is not None else {})
        except Exception:
            log_exception(f"emit({name}) failed")

    def push_state(self):
        self.emit("stateChanged", self._state_dict())

    def _busy(self):
        """True while anything the launcher owns still needs supervising."""
        with self._proc_lock:
            if self._processes or self._installing or self._importing:
                return True
        return bool(self._sessions)


    def _runtime_dict(self) -> dict:
        with self._proc_lock:
            running = list(self._processes.keys())
            sessions = dict(self._sessions)
            installing = list(self._installing)
        return {
            "running": running,
            "sessions": sessions,
            "installing": installing,
            "playtime_total": int(settings_store.read().get("playtime_seconds", 0)),
        }

    def _state_dict(self) -> dict:
        settings = settings_store.read()
        accounts = accounts_store.read()
        active_account = find_account(accounts, settings.get("active_account"))
        if not active_account and accounts:
            active_account = accounts[0]

        instances = []
        for meta in list_instances():
            instances.append({
                "name": meta.get("name"),
                "version": meta.get("version"),
                "loader": meta.get("loader", "vanilla"),
                "loader_version": meta.get("loader_version"),
                "installed": is_installed(meta),
                "playtime": int(meta.get("playtime", 0)),
                "icon": meta.get("icon") if meta.get("icon") in INSTANCE_ICONS else None,
                "created": meta.get("created"),
            })
        active_instance_name = settings.get("active_instance")
        active_instance = next(
            (i for i in instances if i["name"] == active_instance_name), None)
        if not active_instance and instances:
            active_instance = instances[0]

        java_path, java_line = detect_java_for_ui()
        runtime = self._runtime_dict()

        return {
            "version": APP_VERSION,
            "accounts": [public_account(a) for a in accounts],
            "active_account": public_account(active_account) if active_account else None,
            "instances": instances,
            "active_instance": active_instance,
            "running": runtime["running"],
            "sessions": runtime["sessions"],
            "installing": runtime["installing"],
            "playtime_total": runtime["playtime_total"],
            "settings": {
                "memory_mb": int(settings.get("memory_mb", 2048)),
                "java_path": settings.get("java_path"),
                "ms_warning_seen": bool(settings.get("ms_warning_seen", False)),
                "sidebar_collapsed": bool(settings.get("sidebar_collapsed", False)),
                "theme": settings.get("theme", "green") if settings.get("theme") in THEMES else "green",
                "ui_style": settings.get("ui_style", "default") if settings.get("ui_style") in UI_STYLES else "default",
                "server_beta_seen": bool(settings.get("server_beta_seen", False)),
                "local_skin_warning_seen": [x for x in (settings.get("local_skin_warning_seen") or [])
                                            if isinstance(x, str)],
                },
            "java_ok": java_path is not None,
            "java_version": java_line,
        }

    def get_state(self):
        try:
            return {"success": True, "state": self._state_dict()}
        except Exception as exc:
            log_exception("get_state")
            return {"success": False, "error": str(exc)}

    def get_runtime(self):
        try:
            data = self._runtime_dict()
            data["success"] = True
            return data
        except Exception as exc:
            log_exception("get_runtime")
            return {"success": False, "error": str(exc)}


    def get_versions(self):
        try:
            manifest = meta_cache.get_manifest()
            releases = [v for v in manifest["versions"] if v["type"] == "release"]
            return {"success": True, "versions": releases,
                    "latest_release": manifest.get("latest", {}).get("release")}
        except Exception:
            log_exception("get_versions")
            return {"success": False, "error": "Failed to load versions"}

    def get_fabric_loaders(self, minecraft_version=None):
        try:
            loaders = meta_cache.get_fabric_loaders(minecraft_version)
            return {"success": True, "loaders": loaders}
        except Exception as exc:
            log_exception("get_fabric_loaders")
            return {"success": False, "error": str(exc) or "Fabric loader could not be retrieved."}

    def get_fabric_game_versions(self):
        try:
            return {"success": True, "versions": meta_cache.get_fabric_game_versions()}
        except Exception as exc:
            log_exception("get_fabric_game_versions")
            return {"success": False, "error": str(exc) or "Fabric versions could not be retrieved."}

    def get_forge_versions(self, minecraft_version):
        try:
            versions = meta_cache.get_forge_versions(minecraft_version)
            return {"success": True, "versions": versions}
        except Exception as exc:
            log_exception("get_forge_versions")
            return {"success": False, "error": str(exc) or "Forge versions could not be retrieved."}

    def get_forge_mc_versions(self):
        try:
            return {"success": True, "versions": meta_cache.get_forge_mc_versions()}
        except Exception as exc:
            log_exception("get_forge_mc_versions")
            return {"success": False, "error": str(exc) or "Forge versions could not be retrieved."}


    def add_account(self, username):

        try:
            username = (username or "").strip()
            if not username or len(username) > 16 or not re.match(r"^[A-Za-z0-9_]+$", username):
                return {"success": False, "error": "Enter a valid username (letters, numbers, underscore)."}
            accounts = accounts_store.read()
            if any((a.get("name") or "").lower() == username.lower() for a in accounts):
                return {"success": False, "error": "An account with that name already exists."}
            account = {"name": username, "uuid": offline_uuid(username), "type": "offline"}
            accounts.append(account)
            accounts_store.write(accounts)

            def _set_active(s):
                if not s.get("active_account"):
                    s["active_account"] = account["uuid"]
            settings_store.update(_set_active)

            self.push_state()
            return {"success": True, "account": public_account(account)}
        except Exception as exc:
            log_exception("add_account")
            return {"success": False, "error": f"Failed to add account: {exc}"}

    def remove_account(self, key):
        try:
            accounts = accounts_store.read()
            target = find_account(accounts, key)
            if not target:
                return {"success": False, "error": "Account not found."}
            accounts = [a for a in accounts if a is not target and a.get("uuid") != target.get("uuid")]
            accounts_store.write(accounts)

            def _fix_active(s):
                cur = find_account(accounts_store.read(), s.get("active_account"))
                if not cur:
                    s["active_account"] = accounts[0]["uuid"] if accounts else None
            settings_store.update(_fix_active)

            self.push_state()
            return {"success": True}
        except Exception as exc:
            log_exception("remove_account")
            return {"success": False, "error": f"Failed to remove account: {exc}"}

    def switch_account(self, key):
        try:
            target = find_account(accounts_store.read(), key)
            if not target:
                return {"success": False, "error": "Account not found."}
            settings_store.update(lambda s: s.__setitem__("active_account", target["uuid"]))
            self.push_state()
            return {"success": True}
        except Exception:
            log_exception("switch_account")
            return {"success": False, "error": "Failed to switch account."}


    def list_saved_skins(self):
        try:
            return {"success": True, "skins": list_saved_skins()}
        except Exception:
            log_exception("list_saved_skins")
            return {"success": False, "error": "Couldn't read your saved skins."}


    def save_skin(self, name, image, variant="classic", source=None):
        try:
            png = decode_png_uri(image)
            name = re.sub(r"\s+", " ", (name or "").strip())[:32] or "My skin"
            skin_id = uuid.uuid4().hex[:10]
            (SKINS_DIR / f"{skin_id}.png").write_bytes(png)
            entry = {"id": skin_id, "name": name,
                     "variant": "slim" if variant == "slim" else "classic", "created": time.time()}
            if isinstance(source, str) and _catalog_url_ok(source):
                entry["source"] = source
            skins_index.update(lambda idx: idx.append(entry))
            return {"success": True, "skins": list_saved_skins()}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception:
            log_exception("save_skin")
            return {"success": False, "error": "Couldn't save the skin."}


    def delete_saved_skin(self, skin_id):
        try:
            skin_id = re.sub(r"[^A-Za-z0-9]", "", str(skin_id))
            (SKINS_DIR / f"{skin_id}.png").unlink(missing_ok=True)
            skins_index.update(lambda idx: idx.__setitem__(slice(None), [e for e in idx if e.get("id") != skin_id]))
            return {"success": True, "skins": list_saved_skins()}
        except Exception:
            log_exception("delete_saved_skin")
            return {"success": False, "error": "Couldn't delete the skin."}


    def catalog_fetch_skin(self, url):

        try:
            if not _catalog_url_ok(url):
                return {"success": False, "error": "That isn't a catalog skin."}
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read(200_001)
            if not data.startswith(PNG_SIG):
                return {"success": False, "error": "That isn't a PNG image."}
            uri = png_uri(data)
            decode_png_uri(uri)
            return {"success": True, "image": uri}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception:
            log_exception("catalog_fetch_skin")
            return {"success": False, "error": "Couldn't download that skin. Check your connection."}


    def _local_context(self, instance_name):
        accounts = accounts_store.read()
        acc = find_account(accounts, settings_store.read().get("active_account")) or (accounts[0] if accounts else None)
        if not acc or acc.get("type") != "offline":
            raise ValueError("Local skins are for offline accounts.")
        username = acc.get("name") or ""
        if not re.match(r"^[A-Za-z0-9_]{1,16}$", username):
            raise ValueError("This account's username can't be used for a local skin.")
        meta = read_instance_meta(instance_name)
        if not meta:
            raise ValueError("Instance not found.")
        if meta.get("loader", "vanilla") not in ("fabric", "forge"):
            raise ValueError("Vanilla instances can't load CustomSkinLoader. Pick a Fabric or Forge instance.")
        return username, meta

    def _csl_ensure(self, instance_name, meta) -> bool:


        if _csl_installed(instance_name):
            return False
        loader, mc = meta.get("loader"), meta.get("version")
        try:
            version = self._find_version(CSL_PROJECT, loader, mc)
        except Exception:
            log_exception("csl lookup")
            raise ValueError("Couldn't reach Modrinth to download CustomSkinLoader. Check your connection.")
        if not version:
            raise ValueError(f"CustomSkinLoader doesn't support {str(loader).capitalize()} {mc}, "
                             "so this version is unsupported for local skins.")
        mods_dir = instance_mods_dir(instance_name)
        mods_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._download_version_file(version, mods_dir)
            for dep in version.get("dependencies", []):
                if dep.get("dependency_type") != "required" or not dep.get("project_id"):
                    continue
                try:
                    proj = http_get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(dep['project_id'])}")
                    if _mod_present(instance_name, proj.get("slug") or proj.get("title") or ""):
                        continue
                    dv = self._find_version(dep["project_id"], loader, mc)
                    if dv:
                        self._download_version_file(dv, mods_dir)
                except Exception:
                    log_exception("csl dependency")
        except Exception:
            log_exception("csl download")
            raise ValueError("Couldn't download CustomSkinLoader. Check your connection and try again.")
        if not _csl_installed(instance_name):
            raise ValueError("CustomSkinLoader didn't download correctly. Try again.")
        return True

    def local_skin_targets(self):

        try:
            out = []
            for inst in list_instances():
                loader = inst.get("loader", "vanilla")
                if loader not in ("fabric", "forge"):
                    continue
                out.append({"name": inst["name"], "version": inst["version"], "loader": loader,
                            "csl": _csl_installed(inst["name"])})
            return {"success": True, "instances": out}
        except Exception:
            log_exception("local_skin_targets")
            return {"success": False, "error": "Failed to list instances."}

    def _offline_username(self):
        accounts = accounts_store.read()
        acc = find_account(accounts, settings_store.read().get("active_account")) or (accounts[0] if accounts else None)
        if not acc or acc.get("type") != "offline":
            raise ValueError("Local skins are for offline accounts.")
        username = acc.get("name") or ""
        if not re.match(r"^[A-Za-z0-9_]{1,16}$", username):
            raise ValueError("This account's username can't be used for a local skin.")
        return username

    def local_skin_plan(self):


        try:
            installed, installable, total = [], [], 0
            support = {}
            for inst in list_instances():
                loader = inst.get("loader", "vanilla")
                if loader not in ("fabric", "forge"):
                    continue
                total += 1
                name = inst["name"]
                if _csl_installed(name):
                    installed.append(name)
                    continue
                key = (loader, inst.get("version"))
                if key not in support:
                    try:
                        support[key] = bool(self._find_version(CSL_PROJECT, loader, inst.get("version")))
                    except Exception:
                        support[key] = True
                if support[key]:
                    installable.append({"name": name, "version": inst.get("version"), "loader": loader})
            return {"success": True, "total": total, "installed": installed, "installable": installable}
        except Exception:
            log_exception("local_skin_plan")
            return {"success": False, "error": "Failed to list instances."}

    def local_skin_state(self):

        try:
            username = self._offline_username()
            skin, cape = None, None
            for inst in list_instances():
                name = inst["name"]
                if inst.get("loader", "vanilla") not in ("fabric", "forge") or not _csl_installed(name):
                    continue
                base = _csl_local_dir(name)
                skin_path = base / "skins" / f"{username}.png"
                cape_path = base / "capes" / f"{username}.png"
                if skin is None and skin_path.is_file():
                    skin = png_uri(skin_path.read_bytes())
                if cape is None and cape_path.is_file():
                    try:
                        cape = _CAPE_BY_ID.get((base / ".lodestone_cape").read_text(encoding="utf-8").strip())
                    except OSError:
                        cape = None
                if skin and cape:
                    break
            return {"success": True, "name": username, "variant": "classic", "skin": skin,
                    "cape_id": cape["id"] if cape else None,
                    "capes": [{"alias": cape["name"], "active": True}] if cape else []}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log_exception("local_skin_state")
            return {"success": False, "error": f"Couldn't read the local skin: {exc}"}

    def local_install_csl(self, instance_name):
        try:
            _, meta = self._local_context(instance_name)
            return {"success": True, "downloaded": self._csl_ensure(instance_name, meta)}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log_exception("local_install_csl")
            return {"success": False, "error": f"Couldn't install CustomSkinLoader: {exc}"}

    def _local_bulk(self, install, write, write_config=True):




        wanted = {n for n in (install or []) if isinstance(n, str)}
        results = []
        for inst in list_instances():
            name = inst["name"]
            if inst.get("loader", "vanilla") not in ("fabric", "forge"):
                continue
            has_mod = _csl_installed(name)
            if not has_mod and name not in wanted:
                continue
            downloaded = False
            try:
                username, meta = self._local_context(name)
                if not has_mod:
                    downloaded = self._csl_ensure(name, meta)
                if write_config:
                    if _csl_find_config(name) is None:
                        logging.info("CustomSkinLoader.json not found for %s (looked under %s)",
                                     name, instance_game_dir(name))
                        prefix = "CustomSkinLoader was installed. " if downloaded else ""
                        results.append({"name": name, "ok": False, "downloaded": downloaded,
                                        "error": prefix + CSL_RUN_ONCE_MSG})
                        continue
                    _csl_write_config(name)
                write(_csl_local_dir(name), username)
                results.append({"name": name, "ok": True, "downloaded": downloaded})
            except ValueError as exc:
                results.append({"name": name, "ok": False, "downloaded": downloaded, "error": str(exc)})
            except Exception as exc:
                log_exception("local skin on " + name)
                results.append({"name": name, "ok": False, "downloaded": downloaded, "error": f"Failed: {exc}"})
        if not results:
            return {"success": False, "error": "No instance has CustomSkinLoader yet."}
        return {"success": any(r["ok"] for r in results), "results": results}

    def local_set_skin(self, image, install=None):


        try:
            png = decode_png_uri(image)

            def write(base, username):
                _write_bytes_atomic(base / "skins" / f"{username}.png", png)
            return self._local_bulk(install, write)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log_exception("local_set_skin")
            return {"success": False, "error": f"Couldn't set the local skin: {exc}"}

    def local_capes(self):
        return {"success": True, "capes": [{"id": c["id"], "name": c["name"], "url": _cape_url(c)} for c in MOJANG_CAPES]}

    def local_cape_image(self, cape_id):
        try:
            cape = _CAPE_BY_ID.get(cape_id)
            if not cape:
                return {"success": False, "error": "Unknown cape."}
            uri = fetch_texture_uri(_cape_url(cape))
            return {"success": bool(uri), "image": uri}
        except Exception:
            log_exception("local_cape_image")
            return {"success": False, "error": "Couldn't load that cape."}

    def local_set_cape(self, cape_id=None, install=None):

        try:
            cape, data = None, None
            if cape_id:
                cape = _CAPE_BY_ID.get(cape_id)
                if not cape:
                    return {"success": False, "error": "Unknown cape."}
                req = urllib.request.Request(_cape_url(cape), headers={"User-Agent": USER_AGENT})
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        data = resp.read(300_001)
                except Exception:
                    raise ValueError("Couldn't download that cape. Check your connection.")
                if not data.startswith(PNG_SIG) or len(data) > 300_000:
                    raise ValueError("That cape texture isn't valid.")

            def write(base, username):
                cape_path = base / "capes" / f"{username}.png"
                if cape is None:
                    cape_path.unlink(missing_ok=True)
                    (base / ".lodestone_cape").unlink(missing_ok=True)
                else:
                    _write_bytes_atomic(cape_path, data)
                    _write_bytes_atomic(base / ".lodestone_cape", cape["id"].encode("utf-8"))
            return self._local_bulk(install if cape else None, write, write_config=bool(cape))
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            log_exception("local_set_cape")
            return {"success": False, "error": f"Couldn't set the cape: {exc}"}

    def local_reset(self):

        try:
            def write(base, username):
                (base / "skins" / f"{username}.png").unlink(missing_ok=True)
                (base / "capes" / f"{username}.png").unlink(missing_ok=True)
                (base / ".lodestone_cape").unlink(missing_ok=True)
            return self._local_bulk(None, write, write_config=False)
        except Exception as exc:
            log_exception("local_reset")
            return {"success": False, "error": f"Couldn't reset the local skin: {exc}"}

    def catalog_all(self, force=False):

        try:
            def load():
                skins = _catalog_tree()
                if skins is None:
                    skins = _catalog_walk()
                skins.sort(key=lambda s: s["name"].lower())
                return skins
            return {"success": True, "skins": _catalog_cached("all", load, bool(force))}
        except Exception as exc:
            log_exception("catalog_all")
            return {"success": False, "error": _catalog_error(exc)}

    def get_instance_content(self, instance_name, kind="mods"):
        try:
            meta = read_instance_meta(instance_name)
            if not meta:
                return {"success": False, "error": "Instance not found."}
            if kind not in ("mods", "resourcepacks", "shaderpacks"):
                return {"success": False, "error": "Unknown content type."}
            if kind == "mods" and meta.get("loader", "vanilla") == "vanilla":
                return {"success": True, "items": [], "loader": "vanilla"}
            return {"success": True, "items": list_instance_content(instance_name, kind),
                    "loader": meta.get("loader", "vanilla")}
        except Exception:
            log_exception("get_instance_content")
            return {"success": False, "error": "Couldn't read the folder."}

    def get_content_icon(self, instance_name, kind, filename):
        try:
            return {"success": True, "icon": read_content_icon(instance_name, kind, filename)}
        except Exception:
            return {"success": True, "icon": None}


    def _make_instance(self, name, version, loader, loader_version):
        instance_dir(name).mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "version": version,
            "loader": loader,
            "loader_version": loader_version if loader in ("fabric", "forge") else None,
            "installed": False,
            "schema": INSTALL_SCHEMA,
            "main_class": None,
            "asset_index": None,
            "arguments": None,
            "minecraft_arguments": None,
            "classpath": [],
            "playtime": 0,
            "icon": None,
            "created": time.time(),
        }
        write_instance_meta(name, meta)
        ensure_game_layout(name, loader)
        return meta

    def create_instance(self, version, loader, loader_version, instance_name):


        try:
            name = sanitize_instance_name(instance_name)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        if instance_dir(name).exists():
            return {"success": False, "error": "Instance already exists."}
        if not version:
            return {"success": False, "error": "Select a Minecraft version."}
        if loader in ("fabric", "forge") and not loader_version:
            return {"success": False, "error": f"Select a {loader.capitalize()} version."}

        try:
            self._make_instance(name, version, loader, loader_version)
            self.push_state()
            return {"success": True}
        except Exception as exc:
            log_exception("create_instance")
            return {"success": False, "error": f"Failed to create instance: {exc}"}


    def scan_import_sources(self):
        try:
            return {"success": True, "sources": scan_all_launchers()}
        except Exception:
            log_exception("scan_import_sources")
            return {"success": False, "error": "Couldn't scan for other launchers."}

    def get_import_targets(self):

        try:
            return {"success": True, "instances": [
                {"name": i["name"], "version": i["version"], "loader": i.get("loader", "vanilla"),
                 "loader_version": i.get("loader_version")} for i in list_instances()]}
        except Exception:
            log_exception("get_import_targets")
            return {"success": False, "error": "Couldn't list instances."}

    def import_launcher_instance(self, launcher, source_id, dest_name, existing, copy_options, copy_mods, copy_resourcepacks, copy_saves=False, copy_config=False,
                                copy_shaderpacks=False, copy_schematics=False, copy_screenshots=False):



        try:
            inst, root = _find_scanned_instance(launcher, source_id)
            if not inst:
                return {"success": False, "error": "That instance couldn't be found anymore. Try rescanning."}
            if existing:
                if not read_instance_meta(existing):
                    return {"success": False, "error": "Destination instance not found."}
                dest_name = existing
            else:
                try:
                    dest_name = sanitize_instance_name(dest_name or inst["name"])
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                if instance_dir(dest_name).exists():
                    return {"success": False, "error": "An instance with that name already exists."}
                if not inst.get("version"):
                    return {"success": False, "error": "Couldn't work out which Minecraft version this uses."}
        except Exception:
            log_exception("import_launcher_instance (validate)")
            return {"success": False, "error": "Import failed."}

        with self._proc_lock:
            if dest_name in self._importing:
                return {"success": False, "error": "Already importing into that instance."}
            self._importing.add(dest_name)
        threading.Thread(
            target=self._import_worker,
            args=(launcher, inst, root, dest_name, bool(existing),
                  bool(copy_options), bool(copy_mods), bool(copy_resourcepacks), bool(copy_saves), bool(copy_config),
                  bool(copy_shaderpacks), bool(copy_schematics), bool(copy_screenshots)),
            daemon=True,
        ).start()
        return {"success": True, "instance": dest_name}

    def _import_worker(self, launcher, inst, root, dest_name, into_existing,
                       copy_options, copy_mods, copy_resourcepacks, copy_saves=False, copy_config=False,
                       copy_shaderpacks=False, copy_schematics=False, copy_screenshots=False):
        try:
            self.emit("importStarted", {"instance": dest_name})
            supported = inst["loader"] in ("vanilla", "fabric", "forge")
            dest_loader = inst["loader"] if supported else "vanilla"
            if not into_existing:
                self._make_instance(dest_name, inst["version"], dest_loader,
                                    inst.get("loader_version") if supported else None)

            options_path, mods_dir, packs_dir, saves_dir, config_dir, shaders_dir, schem_dir, shots_dir = _source_paths(launcher, inst["source_id"], root)
            game_dir = instance_game_dir(dest_name)
            ensure_game_layout(dest_name, dest_loader)

            copied = {"options": False, "servers": False, "hotbar": False,
                      "mods": 0, "resourcepacks": 0, "saves": 0, "config": 0,
                      "shaderpacks": 0, "schematics": 0, "screenshots": 0}
            if copy_options and options_path:
                if options_path.is_file():
                    try:
                        shutil.copy2(options_path, game_dir / "options.txt")
                        copied["options"] = True
                    except OSError:
                        log_exception("copying options.txt")
                for fname, key in (("servers.dat", "servers"), ("hotbar.nbt", "hotbar")):
                    src_file = options_path.parent / fname
                    if src_file.is_file():
                        try:
                            shutil.copy2(src_file, game_dir / fname)
                            copied[key] = True
                        except OSError:
                            log_exception(f"copying {fname}")
            if copy_mods and inst["loader"] in ("fabric", "forge") and mods_dir:
                copied["mods"] = _copy_tree_contents(mods_dir, instance_mods_dir(dest_name))
            if copy_resourcepacks and packs_dir:
                copied["resourcepacks"] = _copy_tree_contents(packs_dir, game_dir / "resourcepacks")
            if copy_saves and saves_dir:
                copied["saves"] = _copy_tree_contents(saves_dir, game_dir / "saves")
            if copy_config and inst["loader"] in ("fabric", "forge") and config_dir:
                copied["config"] = _copy_tree_contents(config_dir, instance_config_dir(dest_name))
            if copy_shaderpacks and shaders_dir:
                copied["shaderpacks"] = _copy_tree_contents(shaders_dir, game_dir / "shaderpacks")
            if copy_schematics and schem_dir:
                copied["schematics"] = _copy_tree_contents(schem_dir, game_dir / "schematics")
            if copy_screenshots and shots_dir:
                copied["screenshots"] = _copy_tree_contents(shots_dir, game_dir / "screenshots")

            self.push_state()
            self.emit("importFinished", {"instance": dest_name, "copied": copied})
        except Exception as exc:
            log_exception("_import_worker")
            self.emit("importError", {"instance": dest_name, "message": str(exc) or "Import failed."})
        finally:
            with self._proc_lock:
                self._importing.discard(dest_name)

    def delete_instance(self, instance_name):
        try:
            with self._proc_lock:
                if instance_name in self._processes:
                    return {"success": False, "error": "Stop the instance before deleting it."}
                if instance_name in self._installing:
                    return {"success": False, "error": "Instance is currently installing."}
            target = instance_dir(instance_name)
            if not target.exists():
                return {"success": False, "error": "Instance not found."}
            shutil.rmtree(target, ignore_errors=True)

            def _clear_active(s):
                if s.get("active_instance") == instance_name:
                    s["active_instance"] = None
            settings_store.update(_clear_active)

            self.push_state()
            return {"success": True}
        except Exception:
            log_exception("delete_instance")
            return {"success": False, "error": "Failed to delete instance."}

    def set_active_instance(self, instance_name):
        try:
            if not read_instance_meta(instance_name):
                return {"success": False, "error": "Instance not found."}
            settings_store.update(lambda s: s.__setitem__("active_instance", instance_name))
            self.push_state()
            return {"success": True}
        except Exception:
            log_exception("set_active_instance")
            return {"success": False, "error": "Failed to set active instance."}

    def set_instance_icon(self, instance_name, icon_key):
        try:
            if not read_instance_meta(instance_name):
                return {"success": False, "error": "Instance not found."}
            icon_key = icon_key if icon_key in INSTANCE_ICONS else None
            update_instance_meta(instance_name, lambda m: m.__setitem__("icon", icon_key))
            self.push_state()
            return {"success": True}
        except Exception:
            log_exception("set_instance_icon")
            return {"success": False, "error": "Couldn't change the icon."}

    def rename_instance(self, old_name, new_name):
        try:
            old_meta = read_instance_meta(old_name)
            if not old_meta:
                return {"success": False, "error": "Instance not found."}
            try:
                new_name = sanitize_instance_name(new_name)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            if new_name == old_name:
                return {"success": True}
            if instance_dir(new_name).exists():
                return {"success": False, "error": "An instance with that name already exists."}
            with self._proc_lock:
                if old_name in self._processes or old_name in self._installing:
                    return {"success": False, "error": "Stop or wait for the instance first."}
            shutil.move(str(instance_dir(old_name)), str(instance_dir(new_name)))
            old_meta["name"] = new_name
            write_instance_meta(new_name, old_meta)

            def _fix(s):
                if s.get("active_instance") == old_name:
                    s["active_instance"] = new_name
            settings_store.update(_fix)

            self.push_state()
            return {"success": True}
        except Exception:
            log_exception("rename_instance")
            return {"success": False, "error": "Couldn't rename the instance."}

    def get_disk_usage(self, kind):

        try:
            root = INSTANCES_DIR if kind == "instances" else SERVERS_DIR
            out = {}
            if root.is_dir():
                for d in root.iterdir():
                    if d.is_dir():
                        out[d.name] = _dir_size(d)
            return {"success": True, "sizes": out}
        except Exception:
            log_exception("get_disk_usage")
            return {"success": False, "sizes": {}}

    def get_instance_screenshots(self, instance_name):
        try:
            meta = read_instance_meta(instance_name)
            if not meta:
                return {"success": False, "error": "Instance not found."}
            folder = instance_game_dir(instance_name) / "screenshots"
            if not folder.is_dir():
                return {"success": True, "screenshots": []}
            shots = []
            for p in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                    shots.append({"file": p.name, "taken": p.stat().st_mtime})
            return {"success": True, "screenshots": shots}
        except Exception:
            log_exception("get_instance_screenshots")
            return {"success": False, "error": "Couldn't read the screenshots folder."}

    def get_screenshot_image(self, instance_name, filename):


        try:
            folder = instance_game_dir(instance_name) / "screenshots"
            path = folder / Path(filename).name
            if not path.is_file() or not is_within(folder, path):
                return {"success": False, "error": "Screenshot not found."}
            if path.stat().st_size > 40_000_000:
                return {"success": False, "error": "That screenshot is too large to preview."}
            ext = path.suffix.lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return {"success": True, "image": f"data:{mime};base64,{data}"}
        except Exception:
            log_exception("get_screenshot_image")
            return {"success": False, "error": "Couldn't read that screenshot."}

    def get_changelog(self, force=False):

        try:
            now = time.time()
            if not force and _changelog_mem["value"] and now - _changelog_mem["at"] < 600:
                return {"success": True, **_changelog_mem["value"]}
            rel = http_get_json(f"https://api.github.com/repos/{CHANGELOG_REPO}/releases/latest",
                                timeout=15, retries=1)
            asset = next((a for a in rel.get("assets", [])
                          if (a.get("name") or "").lower() == "changelog.txt"), None)
            text = None
            if asset and asset.get("browser_download_url"):
                try:
                    text = http_get_text(asset["browser_download_url"]).strip()
                except Exception:
                    log_exception("changelog.txt download")
            if not text:
                text = (rel.get("body") or "").strip()
            if not text:
                text = "No changelog was published with this release."
            assets = rel.get("assets", [])
            setup = next((a for a in assets if (a.get("name") or "").lower() == "setup.exe"), None) or next(
                (a for a in assets if (a.get("name") or "").lower().endswith(".exe")
                 and "setup" in (a.get("name") or "").lower()), None)
            value = {"tag": rel.get("tag_name"), "name": rel.get("name"),
                     "date": rel.get("published_at"), "text": text[:20000],
                     "setup_url": setup.get("browser_download_url") if setup else None,
                     "setup_size": int(setup.get("size") or 0) if setup else 0}
            _changelog_mem["at"], _changelog_mem["value"] = now, value
            try:
                CHANGELOG_CACHE.write_text(json.dumps(value), encoding="utf-8")
            except OSError:
                pass
            return {"success": True, **value}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"success": False, "error": "No releases have been published yet."}
            log_exception("get_changelog")
        except Exception:
            log_exception("get_changelog")
        try:
            cached = json.loads(CHANGELOG_CACHE.read_text(encoding="utf-8"))
            return {"success": True, "cached": True, **cached}
        except Exception:
            return {"success": False, "error": "Couldn't load the changelog. Check your connection."}

    def check_for_updates(self, force=False):

        try:
            res = self.get_changelog(bool(force))
            if not res.get("success"):
                return {"success": False, "error": res.get("error") or "Couldn't check for updates."}
            tag = res.get("tag") or ""
            muted = settings_store.read().get("update_muted_at") == APP_VERSION
            return {"success": True, "current": APP_VERSION, "latest": tag,
                    "available": _version_tuple(tag) > _version_tuple(APP_VERSION),
                    "installer": bool(res.get("setup_url")), "muted": muted}
        except Exception:
            log_exception("check_for_updates")
            return {"success": False, "error": "Couldn't check for updates."}

    def mute_updates(self):

        try:
            settings_store.update(lambda st: st.__setitem__("update_muted_at", APP_VERSION))
            return {"success": True}
        except Exception:
            log_exception("mute_updates")
            return {"success": False, "error": "Couldn't save that."}

    def install_update(self):
        if os.name != "nt":
            return {"success": False, "error": "Automatic updating is only available on Windows."}
        threading.Thread(target=self._update_worker, daemon=True).start()
        return {"success": True}

    def _update_worker(self):
        try:
            res = self.get_changelog(True)
            url = res.get("setup_url") if res.get("success") else None
            if not url:
                self.emit("updateError", {"message": "The latest release has no setup.exe to install."})
                return
            size = int(res.get("setup_size") or 0)
            dest = DATA_DIR / "update" / "setup.exe"
            done = [0]

            def progress(n):
                done[0] += n
                if size:
                    self.emit("updateProgress", {"percent": min(100, int(done[0] * 100 / size))})

            self.emit("updateProgress", {"percent": 0})
            http_download(url, dest, progress_cb=progress)
            self.emit("updateInstalling", {})
            os.startfile(str(dest))
            time.sleep(1.5)
            if self._quit is not None:
                self._quit()
        except Exception:
            log_exception("update")
            self.emit("updateError", {"message": "Couldn't download the update. Check your connection and try again."})

    def open_url(self, url):

        try:
            import webbrowser
            if not isinstance(url, str) or not url.startswith("https://"):
                return {"success": False}
            webbrowser.open(url)
            return {"success": True}
        except Exception:
            log_exception("open_url")
            return {"success": False}

    def open_instance_folder(self, instance_name):

        try:
            if not read_instance_meta(instance_name):
                return {"success": False, "error": "Instance not found."}
            meta = read_instance_meta(instance_name) or {}
            ensure_game_layout(instance_name, meta.get("loader", "vanilla"))
            open_in_file_manager(instance_game_dir(instance_name))
            return {"success": True}
        except Exception:
            log_exception("open_instance_folder")
            return {"success": False, "error": "Couldn't open the folder."}


    def launch(self, instance_name):
        try:
            with self._proc_lock:
                if instance_name in self._processes:
                    return {"success": False, "error": "Instance is already running."}
                if instance_name in self._installing:
                    return {"success": False, "error": "Instance is currently installing."}

            accounts = accounts_store.read()
            settings = settings_store.read()
            active_account = find_account(accounts, settings.get("active_account"))
            if not active_account and accounts:
                active_account = accounts[0]
            if not active_account:
                return {"success": False, "error": "Please add an account first."}

            meta = read_instance_meta(instance_name)
            if not meta:
                return {"success": False, "error": "Instance not found."}

            needs_install = not is_installed(meta)
            if not needs_install:
                java_path = resolve_java(java_info_from_meta(meta), allow_download=False)
                if java_path:
                    return self._do_launch(instance_name, meta, active_account, java_path)

            with self._proc_lock:
                self._installing.add(instance_name)
            self.push_state()
            threading.Thread(
                target=self._launch_worker,
                args=(instance_name, meta, active_account, needs_install),
                daemon=True,
            ).start()
            return {"success": True, "installing": True}
        except Exception:
            log_exception("launch")
            return {"success": False, "error": "Failed to launch Minecraft."}

    def _launch_worker(self, instance_name, meta, account, needs_install):
        try:
            if needs_install:
                if not Installer(self, meta).run():
                    return
                meta = read_instance_meta(instance_name) or meta

            info = java_info_from_meta(meta)
            java_path = resolve_java(info, allow_download=False)
            if not java_path:
                self.emit("installStarted", {
                    "instance": instance_name,
                    "title": f"Downloading Java {info['majorVersion']}..."})
                tracker = ProgressTracker(self.emit)
                java_path = resolve_java(info, allow_download=True, tracker=tracker)
                self.emit("installFinished", {"instance": instance_name})

            result = self._do_launch(instance_name, meta, account, java_path)
            if not result.get("success"):
                self.emit("launchError", {"instance": instance_name,
                                          "message": result.get("error", "Failed to launch.")})
        except Exception as exc:
            log_exception("_launch_worker")
            self.emit("installError", {"message": str(exc) or "Failed to launch Minecraft."})
        finally:
            with self._proc_lock:
                self._installing.discard(instance_name)
            self.push_state()

    def _do_launch(self, instance_name, meta, account, java_path):
        try:
            if not java_path:
                major = java_info_from_meta(meta)["majorVersion"]
                return {"success": False, "error": f"Java {major} was not found."}

            settings = settings_store.read()
            memory_mb = int(settings.get("memory_mb", 2048))
            command = build_launch_command(meta, account, memory_mb, console_java(java_path))
            game_dir = instance_game_dir(instance_name)
            ensure_game_layout(instance_name, meta.get("loader", "vanilla"))

            log_path = instance_dir(instance_name) / "launch_output.log"
            log_file = open(log_path, "wb")
            logging.info("Launching %s: %s", instance_name, " ".join(command)[:2500])

            try:
                proc = subprocess.Popen(
                    command,
                    cwd=str(game_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    shell=False,
                )
            except Exception:
                log_file.close()
                raise

            started = time.time()
            with self._proc_lock:
                self._processes[instance_name] = proc
                self._sessions[instance_name] = started

            update_instance_meta(instance_name, lambda m: m.__setitem__("last_played", started))
            threading.Thread(
                target=self._watch_process,
                args=(instance_name, proc, log_file, log_path, started),
                daemon=True,
            ).start()

            self.push_state()
            return {"success": True}
        except Exception as exc:
            log_exception("_do_launch")
            return {"success": False, "error": f"Failed to launch Minecraft: {exc}"}

    def _watch_process(self, instance_name, proc, log_file, log_path, started):
        code = proc.wait()
        try:
            log_file.close()
        except OSError:
            pass
        elapsed = int(time.time() - started)

        with self._proc_lock:
            self._processes.pop(instance_name, None)
            self._sessions.pop(instance_name, None)
            user_stopped = instance_name in self._stopping
            self._stopping.discard(instance_name)

        if elapsed >= 1:
            try:
                settings_store.update(
                    lambda s: s.__setitem__("playtime_seconds",
                                            int(s.get("playtime_seconds", 0)) + elapsed))
                update_instance_meta(
                    instance_name,
                    lambda m: m.__setitem__("playtime", int(m.get("playtime", 0)) + elapsed))
            except Exception:
                log_exception("playtime update")

        if code != 0 and not user_stopped:
            tail = _log_tail(log_path)
            logging.error("%s exited with code %s\n%s", instance_name, code, tail[-2000:])
            self.emit("launchError", {
                "instance": instance_name,
                "message": f"{instance_name} closed unexpectedly (exit code {code}). {_explain_crash(tail)}",
            })
        self.push_state()

    def stop(self, instance_name):
        try:
            with self._proc_lock:
                proc = self._processes.get(instance_name)
                if proc:
                    self._stopping.add(instance_name)
            if not proc:
                return {"success": False, "error": "Instance is not running."}
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return {"success": True}
        except Exception:
            log_exception("stop")
            return {"success": False, "error": "Failed to stop instance."}


    def save_settings(self, patch: dict):
        try:
            def _apply(settings):
                if "memory_mb" in patch:
                    try:
                        mb = int(patch["memory_mb"])
                        settings["memory_mb"] = max(512, min(mb, 32768))
                    except (TypeError, ValueError):
                        pass
                if "java_path" in patch:
                    settings["java_path"] = (patch["java_path"] or "").strip() or None
                if "ms_warning_seen" in patch:
                    settings["ms_warning_seen"] = bool(patch["ms_warning_seen"])
                if "sidebar_collapsed" in patch:
                    settings["sidebar_collapsed"] = bool(patch["sidebar_collapsed"])
                if patch.get("theme") in THEMES:
                    settings["theme"] = patch["theme"]
                if patch.get("ui_style") in UI_STYLES:
                    settings["ui_style"] = patch["ui_style"]
                if "server_beta_seen" in patch:
                    settings["server_beta_seen"] = bool(patch["server_beta_seen"])
                if isinstance(patch.get("local_skin_warning_seen"), list):
                    settings["local_skin_warning_seen"] = [str(x) for x in patch["local_skin_warning_seen"]][:200]
            settings = settings_store.update(_apply)
            _JAVA_UI_CACHE["at"] = 0.0
            self.push_state()
            return {"success": True, "settings": settings}
        except Exception:
            log_exception("save_settings")
            return {"success": False, "error": "Failed to save settings."}


    def search_addons(self, query="", project_type="mod"):
        try:
            if project_type not in ADDON_TYPES:
                project_type = "mod"
            facets = [[f"project_type:{project_type}"]]
            params = {
                "query": query or "",
                "limit": "30",
                "facets": json.dumps(facets),
                "index": "relevance" if query else "downloads",
            }
            url = f"{MODRINTH_API}/search?" + urllib.parse.urlencode(params)
            data = http_get_json(url)
            return {"success": True, "hits": data.get("hits", [])}
        except Exception:
            log_exception("search_addons")
            return {"success": False, "error": "Failed to search Modrinth."}

    def get_addon(self, project_id):
        try:
            data = http_get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}")
            return {"success": True, "project": data}
        except Exception:
            log_exception("get_addon")
            return {"success": False, "error": "Failed to load addon info."}

    def get_addon_targets(self, project_type="mod"):

        try:
            result = []
            for inst in list_instances():
                loader = inst.get("loader", "vanilla")
                if project_type == "mod" and loader not in ("fabric", "forge"):
                    continue
                if project_type == "shader" and loader != "fabric":
                    continue
                item = {
                    "name": inst["name"], "version": inst["version"],
                    "loader": loader, "loader_version": inst.get("loader_version"),
                    "installed": is_installed(inst),
                }
                if project_type == "shader":
                    item["iris"] = self._fabric_has_iris(inst["name"])
                result.append(item)
            return {"success": True, "instances": result}
        except Exception:
            log_exception("get_addon_targets")
            return {"success": False, "error": "Failed to list instances."}

    def _find_version(self, project_id, loader=None, mc_version=None):
        params = {}
        if loader:
            params["loaders"] = json.dumps([loader])
        if mc_version:
            params["game_versions"] = json.dumps([mc_version])
        url = f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}/version"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return pick_best_version(http_get_json(url))

    def _download_version_file(self, version, dest_dir: Path):
        file_info = primary_file(version)
        if not file_info:
            raise RuntimeError("That version has no downloadable file.")
        filename = Path(file_info["filename"]).name
        dest = dest_dir / filename
        if not is_within(dest_dir, dest):
            raise RuntimeError("Unsafe file name in addon.")
        http_download(file_info["url"], dest, file_info.get("size"),
                      (file_info.get("hashes") or {}).get("sha1"))
        return filename

    def _install_mod_with_deps(self, version, mods_dir, loader, mc_version, visited, files, depth=0):
        files.append(self._download_version_file(version, mods_dir))
        if depth >= 3:
            return
        for dep in version.get("dependencies", []):
            if dep.get("dependency_type") != "required":
                continue
            pid = dep.get("project_id")
            if not pid or pid in visited:
                continue
            visited.add(pid)
            try:
                if dep.get("version_id"):
                    dep_version = http_get_json(
                        f"{MODRINTH_API}/version/{urllib.parse.quote(dep['version_id'])}")
                else:
                    dep_version = self._find_version(pid, loader, mc_version)
                if dep_version:
                    self._install_mod_with_deps(dep_version, mods_dir, loader, mc_version,
                                                visited, files, depth + 1)
            except Exception:
                log_exception(f"dependency {pid} failed")

    def get_addon_dependencies(self, project_id, instance_name=None):


        try:
            meta = read_instance_meta(instance_name) if instance_name else None
            mc_version = meta["version"] if meta else None
            loader = meta.get("loader") if meta else None
            version = self._find_version(project_id, loader, mc_version)
            if not version:
                return {"success": True, "dependencies": []}
            deps = []
            for dep in version.get("dependencies", []):
                if dep.get("dependency_type") != "required":
                    continue
                pid = dep.get("project_id")
                if not pid:
                    continue
                try:
                    proj = http_get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(pid)}")
                    deps.append({"project_id": pid, "title": proj.get("title", pid),
                                "icon_url": proj.get("icon_url")})
                except Exception:
                    deps.append({"project_id": pid, "title": pid, "icon_url": None})
            return {"success": True, "dependencies": deps}
        except Exception:
            log_exception("get_addon_dependencies")
            return {"success": False, "error": "Couldn't load dependencies."}

    def install_addon(self, project_id, project_type, instance_name, include_deps=True):
        try:
            meta = read_instance_meta(instance_name)
            if not meta:
                return {"success": False, "error": "Instance not found."}
            mc_version = meta["version"]
            loader = meta.get("loader", "vanilla")

            if project_type == "mod":
                if loader not in ("fabric", "forge"):
                    return {"success": False, "error": "Vanilla instances can't run mods."}
                version = self._find_version(project_id, loader, mc_version)
                if not version:
                    return {"success": False,
                            "error": f"No version of this mod supports {loader.capitalize()} {mc_version}."}
                mods_dir = instance_mods_dir(instance_name)
                mods_dir.mkdir(parents=True, exist_ok=True)
                files: list = []
                if include_deps:
                    self._install_mod_with_deps(version, mods_dir, loader, mc_version,
                                                {project_id}, files)
                else:
                    files.append(self._download_version_file(version, mods_dir))
                return {"success": True, "files": files}

            if project_type == "resourcepack":
                note = None
                version = self._find_version(project_id, None, mc_version)
                if not version:
                    version = self._find_version(project_id)
                    note = f"No exact match for {mc_version}, installed the newest version instead."
                if not version:
                    return {"success": False, "error": "That resource pack has no downloadable version."}
                dest_dir = instance_game_dir(instance_name) / "resourcepacks"
                dest_dir.mkdir(parents=True, exist_ok=True)
                name = self._download_version_file(version, dest_dir)
                return {"success": True, "files": [name], "note": note}

            if project_type == "shader":
                if loader != "fabric":
                    return {"success": False, "error": "Shaders install into Fabric instances (with Iris)."}
                return self._install_shader(project_id, instance_name, meta)

            return {"success": False, "error": "Unsupported addon type."}
        except Exception as exc:
            log_exception("install_addon")
            return {"success": False, "error": f"Failed to install: {exc}"}

    def install_modpack(self, project_id):
        with self._proc_lock:
            if self._addon_busy:
                return {"success": False, "error": "Another addon install is already running."}
            self._addon_busy = True
        threading.Thread(target=self._modpack_worker, args=(project_id,), daemon=True).start()
        return {"success": True}

    def _modpack_worker(self, project_id):
        pack_path = None
        try:
            tracker = ProgressTracker(self.emit)
            tracker.event = "addonProgress"
            tracker.set_task("Fetching modpack", force=True)

            project = http_get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}")
            versions = http_get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(project_id)}/version")
            version = pick_best_version(versions)
            if not version:
                raise RuntimeError("This modpack has no downloadable versions.")
            files = version.get("files", [])
            pack_file = (next((f for f in files if f.get("filename", "").endswith(".mrpack")), None)
                         or primary_file(version))
            if not pack_file:
                raise RuntimeError("This modpack has no downloadable file.")

            pack_path = TMP_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', project_id)}.mrpack"
            tracker.reset(pack_file.get("size") or 1_000_000)
            tracker.set_task("Downloading modpack", force=True)
            http_download(pack_file["url"], pack_path, pack_file.get("size"),
                          (pack_file.get("hashes") or {}).get("sha1"), tracker.add)

            with zipfile.ZipFile(pack_path) as zf:
                index = json.loads(zf.read("modrinth.index.json").decode("utf-8"))
                deps = index.get("dependencies", {})
                mc_version = deps.get("minecraft")
                if not mc_version:
                    raise RuntimeError("This modpack doesn't say which Minecraft version it needs.")

                loader, loader_version = "vanilla", None
                if "fabric-loader" in deps:
                    loader, loader_version = "fabric", deps["fabric-loader"]
                elif "forge" in deps:
                    loader, loader_version = "forge", deps["forge"]
                elif "neoforge" in deps:
                    raise RuntimeError("This modpack needs NeoForge, which LodeStone doesn't support yet.")
                elif "quilt-loader" in deps:
                    raise RuntimeError("This modpack needs Quilt, which LodeStone doesn't support yet.")

                name = unique_instance_name(index.get("name") or project.get("title") or "Modpack")
                self._make_instance(name, mc_version, loader, loader_version)
                game_dir = instance_game_dir(name)

                items = []
                for f in index.get("files", []):
                    if (f.get("env") or {}).get("client") == "unsupported":
                        continue
                    rel = f.get("path", "")
                    urls = f.get("downloads") or []
                    if not rel or not urls or not is_within(game_dir, game_dir / rel):
                        continue
                    items.append(({
                        "path": rel, "url": urls[0], "size": f.get("fileSize"),
                        "sha1": (f.get("hashes") or {}).get("sha1"),
                    }, game_dir))

                tracker.reset(sum(entry_weight(e) for e, _ in items) + 1)
                tracker.set_task("Downloading modpack files", force=True)
                download_all(items, tracker, workers=8)

                tracker.set_task("Applying overrides", force=True)
                for member in zf.namelist():
                    for prefix in ("overrides/", "client-overrides/"):
                        if member.startswith(prefix):
                            rel = member[len(prefix):]
                            if not rel:
                                break
                            dest = game_dir / rel
                            if not is_within(game_dir, dest):
                                break
                            if member.endswith("/"):
                                dest.mkdir(parents=True, exist_ok=True)
                            else:
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                dest.write_bytes(zf.read(member))
                            break

            self.push_state()
            self.emit("addonFinished", {"name": name})
        except Exception as exc:
            log_exception("modpack install failed")
            self.emit("addonError", {"message": str(exc) or "Modpack install failed."})
        finally:
            if pack_path:
                try:
                    Path(pack_path).unlink(missing_ok=True)
                except OSError:
                    pass
            with self._proc_lock:
                self._addon_busy = False
