"""LodeStone -- a Minecraft launcher for Windows.

Modules, in dependency order:

    paths      data locations, logging, JSON-backed stores
    net        HTTP, filesystem and host-platform helpers
    rules      version rule evaluation and argument templating
    downloads  library/asset resolution, progress and fetching
    java       locating, probing and installing Java runtimes
    meta       version metadata cache
    instances  instance layout, metadata, account lookups
    launch     assembling the java command line
    skins      local skins and capes via CustomSkinLoader
    catalog    the bundled mod/resource pack catalog
    importer   adopting instances from other launchers
    installer  Forge and Fabric installation
    servers    local Minecraft server management
    api        the RPC surface the UI talks to
    transport  the localhost HTTP + SSE server that fronts the API
"""

from . import api, catalog, downloads, importer, instances, installer, java, launch, meta
from . import net, paths, rules, servers, skins, transport
from .api import API
from .transport import serve

__all__ = ["API", "serve", "paths", "net", "rules", "downloads", "java", "meta",
           "instances", "launch", "skins", "catalog", "importer", "installer",
           "servers", "api", "transport"]
