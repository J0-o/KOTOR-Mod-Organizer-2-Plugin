# Mod Organizer 2 Plugin  
## Star Wars: Knights of the Old Republic  
## Star Wars: Knights of the Old Republic II – The Sith Lords

Enables full modding support for both games in Mod Organizer 2.

## Features
- Assisted mod installation for archives containing:
  - Multiple TSLPatcher installs  
  - A single TSLPatcher install  
  - Multiple loose-file install options  
  - Simple loose-file installs
- Modding support for all required game folders (the Data folder and non-game asset directories are excluded)
- **HK Reassembler** – a TSLPatcher mod manager
- **Save Files tab** with thumbnails
- **Texture Conflicts tab**
- Steam Workshop subscription detection (warns you to unsubscribe)

## Usage
Mod Organizer 2 **3.x dev build** is required. This pre-release build is available on their [Discord](https://discord.gg/ewUVAqyrQX).

Download the KOTOR plugin from the Releases page and extract it into your MO2 directory.

When downloading mods, place them in the **Downloads** folder under your MO2 directory or drag them directly into the **Downloads** tab.

### Installing Mods
- Double-click a mod in the **Downloads** tab to start installation.  
  If the file/folder structure is recognized, MO2 will guide you through the process.
- TSLPatcher mods are installed as:  
  `MODNAME/tslpatchdata/`
- Mods containing multiple TSLPatchers will prompt you to choose which ones to add.  
  If you need to install multiple components from a single archive, double-click the archive again and install the next part under a different name.
- TSLPatcher mods **are not managed by MO2**, so enabling them in the mod list is not required.  
  Their **order still matters**, and HK Reassembler respects that order.
- To manage TSLPatcher mods, launch **HK Reassembler** from the dropdown next to the **Run** button (top right).  
  Check the TSLPatcher parts you want to install and click *Save*. They will be applied in **modlist order**.  
  This creates a new mod called **HK_REASSEMBLER**. Enable it and move it wherever you want in your load order.

### Texture Conflicts Tab
The textures tab shows texture conflicts across all installed mods.  
This matters because the game accepts several texture formats (**TPC, TGA, DDS, TXI**), and the priority order can cause unexpected overrides.

- **TPC always overrides all other formats**, so these are shown as minor warnings.  
- A **TPC and TXI with the same name is a major warning** because it can cause a game crash.  
  To fix it, right-click the unwanted texture and choose *Hide*.
- Minor warnings can be ignored, but remember that a TPC will always win the conflict.

## General Mod Organizer 2 Guides
https://www.modorganizer.org/

## Holo Patcher
[Holo Patcher](https://github.com/th3w1zard1/HoloPatcher) is used to install TSLPatcher mods in the HK Reassembler mod manager.
