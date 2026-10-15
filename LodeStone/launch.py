"""Assembling the java command line for an instance."""
from pathlib import Path
import os
from .instances import instance_dir, instance_game_dir
from .net import classpath_sep
from .paths import ASSETS_DIR, DATA_DIR, LIBRARIES_DIR
from .rules import DEFAULT_MODERN_JVM_ARGS, resolve_argument_list, substitute



def build_launch_command(meta: dict, account: dict, memory_mb: int, java_exe: str) -> list:
    name = meta["name"]
    game_dir = instance_game_dir(name)
    natives_dir = instance_dir(name) / "natives"
    game_dir.mkdir(parents=True, exist_ok=True)

    classpath_list = [str(DATA_DIR / rel) for rel in meta["classpath"]]

    replacements = {
        "auth_player_name": account["name"],
        # Offline session. The client treats a "0" token as an unauthenticated
        # session, which is exactly the mode we want.
        "auth_uuid": account["uuid"],
        "auth_access_token": "0",
        "auth_session": "0",
        "auth_xuid": "0",
        "clientid": "0",
        "user_type": "legacy",
        "user_properties": "{}",
        "version_name": meta["version"],
        "version_type": "release",
        "game_directory": str(game_dir),
        "assets_root": str(ASSETS_DIR),
        "assets_index_name": meta.get("asset_index") or meta["version"],
        "natives_directory": str(natives_dir),
        "library_directory": str(LIBRARIES_DIR),
        "classpath_separator": classpath_sep(),
        "classpath": classpath_sep().join(classpath_list),
        "launcher_name": "LodeStone",
        "launcher_version": "1.0",
    }

    features = {}
    args_spec = meta.get("arguments") or {}

    if args_spec.get("jvm"):
        jvm_args = resolve_argument_list(args_spec["jvm"], features, replacements)
    else:
        jvm_args = [substitute(a, replacements) for a in DEFAULT_MODERN_JVM_ARGS]

    if not any(a in ("-cp", "-classpath") for a in jvm_args):
        jvm_args = ["-cp", replacements["classpath"]] + jvm_args
    if not any(a.startswith("-Djava.library.path=") for a in jvm_args):
        jvm_args = [f"-Djava.library.path={natives_dir}"] + jvm_args

    if args_spec.get("game"):
        game_args = resolve_argument_list(args_spec["game"], features, replacements)
    elif meta.get("minecraft_arguments"):
        game_args = [substitute(tok, replacements)
                     for tok in meta["minecraft_arguments"].split(" ") if tok]
    else:
        game_args = [
            "--username", replacements["auth_player_name"],
            "--version", replacements["version_name"],
            "--gameDir", replacements["game_directory"],
            "--assetsDir", replacements["assets_root"],
            "--assetIndex", replacements["assets_index_name"],
            "--uuid", replacements["auth_uuid"],
            "--accessToken", replacements["auth_access_token"],
            "--userType", replacements["user_type"],
            "--versionType", replacements["version_type"],
        ]

    command = [java_exe, f"-Xmx{memory_mb}M", f"-Xms{min(memory_mb, 1024)}M"]
    command.extend(jvm_args)
    command.append(meta["main_class"])
    command.extend(game_args)
    return command
def _log_tail(path: Path, max_bytes: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""
def _explain_crash(tail: str) -> str:
    if "UnsupportedClassVersionError" in tail or "class file version" in tail:
        return "This Minecraft version needs a newer Java. Clear the custom Java path in Settings so LodeStone can pick one."
    if "Could not create the Java Virtual Machine" in tail or "Invalid maximum heap size" in tail:
        return "Java couldn't start with that memory setting. Try lowering the allocated memory in Settings."
    if "ClassNotFoundException" in tail or "NoClassDefFoundError" in tail:
        return "A library is missing. Delete the instance's files by re-installing it, then try again."
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    return lines[-1][:220] if lines else "Check launch_output.log in the instance folder."
