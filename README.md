# LOADSTONE

A Minecraft launcher for Windows, written in Python with no third-party
dependencies. It runs a small local web server and opens itself in your browser.

## Instances

Run as many separate Minecraft installs as you like. Each instance gets its own
game directory, mods folder, config, saves and server data, and can be renamed,
given an icon, or deleted independently.

Per-instance playtime is tracked, and the launcher shows the disk space each
instance and each server actually occupies so you can see what is worth
deleting.

## Versions and loaders

Vanilla, Fabric and Forge. NeoForge and Quilt modpacks are detected and refused
with a clear message rather than failing halfway through an install.

The full Minecraft version list comes from Mojang's own manifest, and each
version is set up correctly on its own terms — asset index, library rules and
native extraction are all resolved per version rather than assumed.

## Java

If no suitable Java is found on the machine, LOADSTONE downloads the correct
runtime itself and keeps it inside its own directory. It looks at the Java
already installed on the system first, understands Mojang's bundled runtimes and
Adoptium builds, and verifies a runtime actually works before relying on it.
A custom Java path can be set per instance in Settings.

## Mods, resource packs and modpacks

A built-in catalog of mods, resource packs, modpacks and shader packs, browsable
from inside the launcher with icons and metadata read straight out of each jar.
Installed content is listed per instance, so you can see what is in a given
install without opening a folder.

Pack files can be installed directly. Dependencies are surfaced, and the
launcher will offer to install CustomSkinLoader onto Fabric or Forge instances
that can use it.

## Skins and capes

Local skins and capes, applied through CustomSkinLoader rather than by
uploading anything to Mojang. A skin is applied to every instance that has the
mod installed, and LOADSTONE offers to install it where it is missing. Vanilla
instances cannot use local skins and say so.

All 33 official capes are browsable and can be applied the same way. Skins can
also be saved to a personal library and re-applied later.

## Offline accounts

Accounts are just usernames. There is no sign-in, no Microsoft account, and no
token stored anywhere — the launcher starts Minecraft in offline mode.

## Minecraft servers

Vanilla, Paper and Fabric servers, managed from the same window as everything
else.

A live console with a scrolling log, the online player list, and the ability to
send console commands. Players can be given operator status, healed, fed,
kicked or banned without leaving the launcher. Paper builds are looked up by
version so you can pick the one you want.

`server.properties` is edited through a real settings form rather than by hand,
gamerules are applied without restart-level guesswork, and the plugin folder is
readable so you can see what is installed before starting up.

## Importing from other launchers

Existing instances can be adopted from Prism Launcher, PolyMC, SKLauncher and
Fast Client. LOADSTONE finds each launcher, reads its instances and versions,
copies the game directories across, and reports what it found before you commit
to anything.

## Interface

Six colour themes and an optional Minecraft-style font. Instance and server
lists, per-instance screenshots, and an update check with a changelog are all
built in.

## No advertisements

None.
