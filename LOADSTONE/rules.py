"""Version rule evaluation and argument templating."""
import re
from .net import current_arch, current_os_name



def _arch_matches(rule_arch: str) -> bool:
    cur = current_arch()
    if rule_arch == "x86":
        return cur == "32"
    if rule_arch in ("arm64", "aarch64"):
        return cur == "arm64"
    if rule_arch in ("x86_64", "amd64", "x64"):
        return cur == "64"
    return True
def rule_matches(rule: dict, features: dict) -> bool:
    os_rule = rule.get("os")
    if os_rule:
        name = os_rule.get("name")
        if name and name != current_os_name():
            return False
        arch = os_rule.get("arch")
        if arch and not _arch_matches(arch):
            return False
    features_rule = rule.get("features")
    if features_rule:
        for key, expected in features_rule.items():
            if bool(features.get(key, False)) != bool(expected):
                return False
    return True
def rules_allow(rules, features=None) -> bool:
    if not rules:
        return True
    features = features or {}
    allowed = False
    for rule in rules:
        if rule_matches(rule, features):
            allowed = rule.get("action", "disallow") == "allow"
    return allowed
def substitute(template: str, replacements: dict) -> str:
    def repl(match):
        key = match.group(1)
        val = replacements.get(key)
        return str(val) if val is not None else match.group(0)
    return re.sub(r"\$\{([a-zA-Z0-9_.]+)\}", repl, template)
def resolve_argument_list(spec_list, features, replacements) -> list:
    out = []
    for item in spec_list:
        if isinstance(item, str):
            out.append(substitute(item, replacements))
        elif isinstance(item, dict):
            if rules_allow(item.get("rules", []), features):
                value = item.get("value", "")
                if isinstance(value, list):
                    out.extend(substitute(v, replacements) for v in value)
                else:
                    out.append(substitute(value, replacements))
    return out
def merge_arguments(parent, child):




    if not parent and not child:
        return None
    parent = parent or {}
    child = child or {}
    return {
        "jvm": list(parent.get("jvm", [])) + list(child.get("jvm", [])),
        "game": list(parent.get("game", [])) + list(child.get("game", [])),
    }
DEFAULT_MODERN_JVM_ARGS = [
    "-Djava.library.path=${natives_directory}",
    "-Dminecraft.launcher.brand=loadstone",
    "-Dminecraft.launcher.version=1.0",
    "-cp",
    "${classpath}",
]
