<p align="center">
  <img src="https://raw.githubusercontent.com/yourusername/loadstone/main/assets/logo.png" alt="LOADSTONE" width="280">
</p>

<h1 align="center">LOADSTONE</h1>

<p align="center">
  <strong>A clean, zero-dependency Minecraft launcher for Windows</strong><br>
  Written in pure Python • Runs a local web server • Opens in your browser
</p>

---

## Features

### Instances
- Run as many separate Minecraft installs as you want  
- Each instance has its own game directory, mods, configs, saves, and server data  
- Rename, set custom icons, or delete instances independently  
- Tracks playtime per instance  
- Shows real disk space used by each instance and server

### Versions & Loaders
- Full support for **Vanilla**, **Fabric**, and **Forge**  
- NeoForge and Quilt modpacks are detected and refused with a clear message  
- Version list pulled directly from Mojang’s official manifest  
- Correct asset index, library rules, and native extraction for every version

### Java
- Automatically downloads the correct Java runtime if none is found  
- Prefers system Java, Mojang’s bundled runtimes, and Adoptium builds  
- Verifies the runtime actually works before using it  
- Custom Java path can be set per instance

### Mods, Resource Packs & Modpacks
- Built-in catalog of mods, resource packs, modpacks, and shader packs  
- Icons and metadata read straight from each jar  
- Per-instance content list so you always know what’s installed  
- Direct pack file installation with dependency detection  
- Offers to install CustomSkinLoader on compatible Fabric/Forge instances

### Skins & Capes
- Local skins and all 33 official capes applied via CustomSkinLoader  
- No upload to Mojang required  
- Apply a skin to every instance that has the mod (and offers to install it where missing)  
- Personal skin library for quick re-use  
- Vanilla instances clearly state they cannot use local skins

### Offline Accounts
- Just usernames — no Microsoft account, no sign-in, no tokens stored  
- Minecraft always starts in offline mode

### Minecraft Servers
- Manage Vanilla, Paper, and Fabric servers from the same window  
- Live console with scrolling log and online player list  
- Send console commands, OP players, heal, feed, kick, or ban  
- Paper builds looked up by version  
- Proper `server.properties` form editor and gamerule controls  
- Plugin folder browser

### Import from Other Launchers
- One-click import from **Prism Launcher**, **PolyMC**, **SKLauncher**, and **Fast Client**  
- Automatically finds instances, copies game directories, and shows a preview before committing

### Interface
- Six colour themes + optional Minecraft-style font  
- Instance & server lists with per-instance screenshots  
- Built-in update checker with changelog  

### No Ads
None. Ever.


## License

MIT (or whatever license you prefer)
