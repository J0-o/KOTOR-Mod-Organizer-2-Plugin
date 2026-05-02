# <img width="256" height="93" alt="KOTORganizer" src="https://raw.githubusercontent.com/J0-o/kotorganizer/refs/heads/main/kotorganizer_logo_256.png" />

# KOTORganizer MO2 Plugin

KOTORganizer extends Mod Organizer 2 for both `STAR WARS Knights of the Old Republic` and `STAR WARS Knights of the Old Republic II: The Sith Lords`.

## Why Mod Organizer 2?

Mod Organizer 2 already provides a stable foundation for mod management across many games. Extending it for *Star Wars: Knights of the Old Republic* and *Star Wars: Knights of the Old Republic II: The Sith Lords* gives KOTOR modding access to MO2’s biggest strength: the virtual file system.

With MO2, the game sees modded files as if they are in the game directory, while the real game folder stays untouched. This keeps the base install closer to vanilla, makes mods easier to enable or disable, and gives better visibility into how files overlap or conflict.

MO2 does have a learning curve, but for advanced KOTOR/KOTOR2 builds, the extra structure is worth it.

## Who Is This For?

This plugin is mainly intended for larger KOTOR/KOTOR2 mod builds, repeatable installs, and users who want better insight into file conflicts.

If you only install a few simple loose-file mods, this may be more tool than you need. If you are building a large mod list with many TSLPatcher mods, texture overrides, and compatibility concerns, MO2 provides a much cleaner workflow.

## Installing

Mod Organizer 2.5.3 Beta is required. The current unreleased beta builds include features that KOTOR support depends on.

A beta build can be acquired from the [Mod Organizer 2 Discord](https://discord.com/invite/ewUVAqyrQX) in the `dev-build` channel. Development has been more active recently, so an official release should be available soon.

Once you download and extract MO2, download and extract KOTORganizer to the MO2 directory. Launch ModOrganizer.exe and you can verify the plugin is loaded by opening the Info tab on the right.

## Texture Management

KOTOR uses several texture-related file types, including `.tpc`, `.tga`, `.dds`, and `.txi`. MO2 normally only detects exact filename conflicts, but KOTOR has additional texture priority rules that can cause problems even when the extensions differ.

The texture management tab helps bridge that gap. It detects conflicts across supported texture formats and highlights them by severity. The highest-severity conflicts are cases that may cause hard crashes in-game. Lower-severity warnings include cases where a `.tpc` takes priority over a `.tga` or `.dds` unexpectedly.

An Auto Fix button is included to resolve supported texture conflicts based on the current MO2 mod priority order.

## TSLPatcher Management

Many KOTOR mods use TSLPatcher-style installers instead of simple loose-file installs. The patcher tab lists enabled mods that contain a `tslpatchdata` folder and can consolidate their patched output into a single MO2-managed mod.

Double-clicking a patcher mod shows more detail, including:

- the `changes.ini`
- parsed human-readable install actions
- detected conflicting patch actions
- the latest install log

A test tab is also available for simulating or testing a single mod install before running the full patcher process.

When the patcher process is run, enabled patcher-style mods are processed in priority order and consolidated into a `[PATCHER FILES]` mod in MO2’s left panel.

## Automatic Mod Deployment

The sync tab is designed to help download, validate, and install mod builds that follow the instructions from the KOTOR Mod Builds site.

The Refresh button pulls the latest instruction set and checks for missing files. Download All starts downloading missing mods in sequence.

Nexus Mods downloads require logging in with a Nexus account inside MO2. Deadly Stream mods can be downloaded automatically when supported. Mods from other sources open in an Edge browser window, where the user only needs to click the download button. The browser window closes automatically after the download completes.

Downloads can still fail. Servers can be slow, unavailable, or inconsistent, and some files may need to be retried manually.

Once all files are validated through Refresh, the Sync button becomes available. Sync extracts the downloaded files, runs the multi-patcher, and applies supported texture conflict fixes. When the patcher summary appears with the number of errors and warnings, the process is complete.

## Manual Mod Installing

KOTOR mods are often packed inconsistently. The plugin includes a KOTOR-specific mod data checker that recognizes common archive layouts and can fix many of them automatically.

This is especially useful for loose-file mods and patcher-style archives that would otherwise require manual cleanup before MO2 can install them correctly.

## Steam Workshop Warning

If the game is installed through Steam and Workshop content is detected, the plugin warns the user.

The intended workflow is MO2-managed content, not a mixed MO2 plus Steam Workshop setup. Mixing both can make conflicts harder to understand and can lead to unexpected file priority issues.

## Save Support

The plugin integrates the game’s saves folder into MO2. Save entries can show timestamps, basic metadata, and screenshot previews.

Profile-specific saves can also be enabled through MO2 settings.

## Limitations

This plugin is meant to reduce repetitive setup work, improve conflict visibility, and make large KOTOR/KOTOR2 builds easier to manage. It does not remove the need to read mod instructions.

Mods with unusual installers, custom compatibility patches, manual edit requirements, or unsupported archive layouts may still require manual review.

## Included Tools

- `HoloPatcher.exe` - CLI support to run tlspatchdata mods
- `DeadlyScraper.exe` - Scraper and downloader for deadlystream.com
- `7z.exe` - archive managment 
- `xxhsum.exe` - very very fast hashing tool




