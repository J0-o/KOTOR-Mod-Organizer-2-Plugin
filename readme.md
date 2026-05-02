# <img width="256" height="93" alt="KOTORganizer" src="https://raw.githubusercontent.com/J0-o/kotorganizer/refs/heads/main/kotorganizer_logo_256.png" />

# KOTORganizer MO2 Plugin

KOTORganizer extends Mod Organizer 2 for both `STAR WARS Knights of the Old Republic` and `STAR WARS Knights of the Old Republic II: The Sith Lords`.

## What It Does

- proper support for KOTOR 1 and KOTOR 2 inside MO2
- folder mapping for KOTOR-specific paths such as `Override`, `Modules`, `Lips`, `Movies`, `StreamVoice`, and `TexturePacks`
- `dialog.tlk` support from active mods
- save-game integration
- custom tabs for `Textures`, `Patcher`, `Sync`, and `Info`
- KOTOR-specific mod layout validation and auto-fixing

## The Main Tabs

### Textures

The `Textures` tab is a conflict browser for texture files in the active mod stack.

It helps you:

- see which mod currently wins for a texture
- spot common texture conflicts
- hide or unhide individual textures
- run an `Auto Fix` pass after larger changes

If you spend time resolving visual conflicts, this is one of the most useful parts of the plugin.

### Patcher

The `Patcher` tab gives MO2 a workflow for mods that ship with `tslpatchdata`.

- detect TSLPatcher-based mods automatically
- review available patches and enable only the ones you want
- inspect patch details, including descriptions, INI data, parsed operations, and logs
- prepare and run patches without leaving MO2
- test individual patches in isolation

### Sync

The `Sync` tab installs a curated KSON( KOTOR JSON ;) ) build into MO2 from a manifest.

- load or fetch the latest KSON file for the current game
- show the full mod list from that manifest
- validate local archives before install
- download missing archives when possible
- rebuild the MO2 mod list from the synced manifest

After sync completes, the plugin can continue into the patcher and texture cleanup workflow so the setup ends in a usable state instead of stopping halfway through.

## Other Useful Behavior

### Save Support

The plugin integrates the game's `saves` folder directly into MO2. Save entries can show timestamps, basic metadata, and screenshot previews.

### Mod Layout Fixing

KOTOR mods are often packed inconsistently. The plugin includes a KOTOR-specific mod data checker that can recognize common layouts and fix many of them automatically, especially loose files and patcher-style archives that would otherwise need manual cleanup.

### Workshop Warning

If the game is installed through Steam and Workshop content is detected, the plugin warns about it. The intended workflow is MO2-managed content, not a mixed MO2 plus Workshop setup.

## Included Tools

- `HoloPatcher.exe` - CLI support to run tlspatchdata mods
- `DeadlyScraper.exe` - Scraper and downloader for deadlystream.com
- `7z.exe` - archive managment 
- `xxhsum.exe` - very very fast hashing tool




