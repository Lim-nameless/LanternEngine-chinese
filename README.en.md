# Lantern Engine — Simplified Chinese Localization

Unofficial Simplified Chinese localisation for the [Lantern Engine](https://github.com/Awakenchan/LanternEngine) launcher.

The tools work on the launcher's own `Lantern Engine.exe` — the strings are rewritten in place inside the single-file bundle. **No game files are touched**, and the original is backed up automatically, so the change is fully reversible.

| | |
|---|---|
| Target | `Lantern Engine.exe` (`.NET 8` single-file bundle, WPF) |
| Verified against | `0.9.34-beta.9.8` (`bbhost-0.4.0-fps80`) |
| Coverage | 1,434 of 1,695 unique UI strings (~85%) |
| Writes | 1,434 `#US` records + 29 BAML records |
| File size after patching | identical to the original, byte for byte |
| Requires | Windows, Python 3.8+ |

![Main window](screenshots/01-play.png)

---

## Usage

1. Install Python 3.8 or newer and tick **Add Python to PATH**
   ([python.org](https://www.python.org/downloads/)).
2. Copy this whole `localization` folder into your game root, next to `Lantern Engine.exe`
   (the folder that also contains `engine\`).
3. Close the launcher, then double-click **`一键汉化.bat`** (one-click localise).

The script detects the launcher by itself — it looks for a known name first, then picks the
largest `.exe` over 5 MB that is not `Bloodborne.exe`. If the author renames the launcher
again, nothing needs editing.

To go back to English, run **`还原英文.bat`**. The original is kept as
`Lantern Engine.exe.zh-CN-backup`.

| Script | What it does |
|---|---|
| `一键汉化.bat` | Localise. Backs up, patches, then really launches the app to verify it starts |
| `还原英文.bat` | Restore the original English executable from backup |
| `自检.bat` | Read-only: report bundle layout, backup state, coverage |
| `预览汉化覆盖率.bat` | Dry run. Changes nothing; writes the untranslated list to `_work\` |
| `重建标识符清单.bat` | Optional. Rebuilds the identifier blacklist (needs the .NET SDK) |

Exit codes: `0` success, `2` already localised, `3` succeeded with a reduced dictionary, `1` failed.

---

## How it works

A .NET single-file bundle keeps the application assembly as one entry in a manifest of
`[offset][size][compressedSize][type][nameLen][name]`. The tool parses that manifest, extracts
`Lantern Engine.dll`, edits it, and writes it back at the same offset with the same length.

Inside the assembly the UI text lives in two places:

**`#US` string heap — C# string literals.** Stored as
`[ECMA-335 compressed length][UTF-16LE][trailing byte]`. Records are addressed by absolute
offset from `ldstr` tokens, so they do not need to be contiguous: shortening one is safe as
long as no other record moves. Every byte released by a shorter translation is filled with
`0x00`, and the declared length is written honestly — padding is never smuggled into the
string itself.

**BAML resources — XAML text.** Window titles, nav labels and search placeholders are compiled
into `.g.resources` as `[one-byte length][UTF-8]`. These are read sequentially with no external
offsets, so their byte length **must** stay the same; shorter translations are padded with
spaces.

The hard part is not writing Chinese, it is deciding which literals are text and which are
identifiers. `_tools/analyze.cs` walks the IL around every `ldstr` and classifies it:

- **show** — `set_Text`, `MessageBox.Show`: safe to translate.
- **cmp** — compared, used as a dictionary key, handed to `XmlConvert.VerifyNCName`, or added
  to a `HashSet` initialiser: **not** translated.
- **table** — immediately followed by `stelem.ref`, i.e. a static string table.

The `cmp` classification is the important one. Translating such a literal used to crash the
launcher the moment you pressed **Play**, because `Patches.Discover` builds `XName` objects from
values like `"Metadata"`, and `XmlConvert.VerifyNCName` rejects anything outside a valid XML
name. Translating a patch name also silently duplicated every patch entry, since discovered
patches are merged with the ones in `Bloodborne.xml` *by name*.

The result is `unsafe-values.json` (404 entries) — literals that must stay English. Anything in
there is skipped, and a few shared values are explicitly allowed back in via an `allow` list
when the same literal is used for both display and comparison and therefore changes
consistently.

### Safety measures

- The executable length never changes, and every difference falls inside a `#US` record or a
  BAML blob — verified before writing, not after.
- Each `#US` rewrite is read back and compared to the intended text. If any record disagrees,
  the whole write is abandoned before touching the file.
- The manifest is re-validated after the new assembly is injected (entry count, offsets,
  other entries untouched).
- The launcher is started once, headless, for 9 seconds to confirm it reaches the main window
  without crashing.
- `Lantern Engine.exe.zh-CN-backup` is created on the first run and never modified afterwards.

---

## What stays in English, on purpose

| Kept | Why |
|---|---|
| `Lantern Engine` (product name) | Used for the process name and Discord presence; only the window title is translated, from BAML |
| The 15 built-in patch names | Merged with `Bloodborne.xml` patch entries **by name**; translating them makes the patch list show 36 entries instead of 22 |
| Trophy grades `Platinum` / `Gold` / `Silver` / `Bronze` | Come from the game's own trophy XML |
| `FSR 4`, `DLSS`, `TAA`, `RCAS`, `PSSR` | Vendor technology names |
| Controller model names | Reported by SDL, not ours to invent |
| `Left Alt`, `Mouse2`, `Space`, … | Real values stored in `bbport.ini`; translating them breaks saved key bindings |
| `Frame stats:`, `Stall:`, `THD:` … | Prefixes used to parse the engine's English log output |

The remaining ~261 untranslated literals are resource keys, brush and style names, Steam VDF
keys, regular expressions, units, and language endonyms.

---

## Adding translations

Any `zh-CN*.json` file in this folder is merged automatically. `_`-prefixed keys are treated as
comments.

```json
{
  "_note": "anything you like",
  "Some new label": "某个新标签"
}
```

To find what is still untranslated, run `预览汉化覆盖率.bat` and read
`_work\<launcher name>-todo.json`; each entry has a `"zh": ""` field to fill in.

Two length limits apply, and the tool reports violations instead of truncating:

- `#US` records may shrink freely, so Chinese is almost never a problem here.
- BAML strings are capped at the original **byte** count (a Chinese character is 3 bytes in
  UTF-8), so long English labels can become short Chinese ones, never the reverse.

If the launcher is updated and a page stops being translated, the cause is usually a new
literal or a shifted BAML blob. `预览汉化覆盖率.bat` will list the new literals; BAML blobs are
located dynamically from the resource name table, so offset shifts do not matter.

---

## Not included

- **No game files.** Lantern Engine requires your own decrypted Bloodborne dump (CUSA03173)
  with update 1.09. This repository contains localisation scripts only.
- **No Python runtime.** The kit used to ship a portable Python; it is 23 MB of binaries and
  does not belong in a source repository. Install Python yourself.

---

## License

The launcher is `GPL-2.0-or-later` (see the repository [LICENSE](../LICENSE)), so these scripts
are distributed under the same terms. No game assets are included or redistributed.
