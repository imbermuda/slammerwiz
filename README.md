# SlammerWiz

**Chaos Orb safety net for Path of Exile 2.** A Windows overlay that reads your hovered item's tooltip after every left-click and locks the gate the instant a target mod appears, so you can't accidentally re-slam a perfect roll.

Hit `F9`, spam-slam, and the moment your tablet rolls the mod you want, the overlay closes itself before your next click reaches the game. Right-click to release the gate and move on.

---

## Install

1. Download **`SlammerWiz.exe`** from the [latest release](https://github.com/imbermuda/slammerwiz/releases/latest).
2. Run it. Windows SmartScreen will warn (unsigned binary) - click *More info → Run anyway*.
3. The overlay docks top-right of your primary monitor.

> **Heads up.** SlammerWiz only ever reads your own screen / clipboard and only ever blocks your own left-clicks. It does **not** read game memory, modify game files, send any input into PoE, or do anything you couldn't do manually. Reading clipboard + dropping local mouse clicks is the safest path I could design, but I'm not GGG. Use at your own risk.

## How to use it

1. **Open the rules editor.** Click `Rules…` and pick the mod(s) you're slamming for (e.g. *"#% increased Rarity of Items found in Map"*).
2. **Set up in-game.** Hover a chaos orb on the tablet you want to slam.
3. **Arm.** Press `F9`. The button turns red.
4. **Slam.** Left-click as fast as you want. Each click triggers an internal Ctrl+C, the overlay parses the new tooltip, and either lets the next click through or locks the gate.
5. **Hit detected → gate locked.** Big red `HIT` flashes; clicks are dropped.
6. **Right-click** to release the lock and continue (next item / move / etc.).
7. `F9` again to disarm when done.

| Key | Action |
| --- | --- |
| `F9` | Arm / disarm |
| `Right-click` | Release the gate after a hit |
| `Ctrl+Shift+Q` | Panic - instant disarm |
| `Ctrl+Q` | Quit |

## How it works (briefly)

A Win32 low-level mouse hook intercepts every left-click. On click-down the overlay synthesises a `Ctrl+C` (PoE's native export-item-to-clipboard), reads the resulting tooltip, parses the mods, and decides:

- **Miss** → next click is allowed through.
- **Match** → all further left-clicks are dropped until you right-click.

No OCR, no screen scraping, no game memory access. Just the clipboard PoE itself populates.

## Telemetry

Every confirmed slam is hashed and sent to `poewiz.com` to build community stats on mod probability per league/patch. To opt out, edit `config.json`:

```json
"sync_enabled": false
```

`config.json` lives next to the exe.

## Build from source

```powershell
git clone https://github.com/imbermuda/slammerwiz.git
cd slammerwiz
.\install.bat
.\run.bat
```

Requires Python 3.12+. To build a single-file exe:

```powershell
.\build.bat
```

Output lands in `dist\SlammerWiz.exe`.

## Status

**v1.2** - Amulets + Rings added to the catalog pull (`?slot=tablet,bow,amulet,ring`). New `[x] Show only ★ god mods` filter in the Rules dialog, on by default (toggle to see the full pool). `min_observed` lowered from 10 to 5 to surface long-tail rolls on accessories without flooding noise.

**v1.1** - multi-slot catalog (`?slot=Tablet,bow`); bow rules now available alongside Precursor Tablet with empirical roll ranges. Empty-remote-response fallback hardening. Shipped-fallback fix under PyInstaller onefile (`sys._MEIPASS`). Catalog request timeout raised to 20s.

**v1.0** - clipboard-only architecture, six guard invariant tests green, validated on PoE 2 Fate of the Vaal league.

## License

MIT.
