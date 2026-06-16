# Nitro Engine DSMA exporter (Blender add-on)

Exports a rigged Blender mesh **directly** to the format Nitro Engine consumes,
without any intermediate `.md5mesh` / `.md5anim` files. One export produces:

- `model.dsm` — the mesh display list
- `model.dsa` — the base (rest) pose, so the model is displayable on its own
- `model_<action>.dsa` — one file per animation/action
- `graphics/<texture>.png` + `graphics/<texture>.grit` — every texture used by
  the mesh, packed as atlas + plus a grit configuration (`-gx -gb -gB16 -gT!`, 16-bit + alpha)

The conversion math is taken verbatim from Nitro Engine's [md5_to_dsma](https://github.com/AntonioND/nitro-engine/tree/master/tools/md5_to_dsma) tool, so
the binary output is byte-for-byte identical to running that tool on an
equivalent MD5 export (verified against the bundled `robot` example).

## Notice

Developed with AI assistance (Claude).

## Requirements

- **Blender 4.5 LTS or newer.** The add-on ships as an extension
  (`blender_manifest.toml`, `blender_version_min = 4.5.0`) and uses only the
  bundled Python — no external packages.

## Installation

1. Zip the `io_export_nds_dsma/` folder (the folder containing
   `blender_manifest.toml`).
2. In Blender: *Edit ▸ Preferences ▸ Get Extensions ▸ Install from Disk…* and
   pick the zip. (Or drag-and-drop the zip into Blender.)
3. Enable it if it isn't enabled automatically.

## Usage

1. Select the rigged mesh(es). They must all be parented/skinned to the **same**
   armature.
2. *File ▸ Export ▸ Nitro Engine model (.dsm/.dsa)*.
3. Pick an output location and a model name (the file name without extension is
   used as the model name).
4. Options:
   - **Pack textures into one atlas** — a DS model has a single texture, so all
     textures are packed into one power-of-two atlas and the UVs are remapped
     automatically. The atlas size is chosen automatically (textures are scaled
     down if they would not fit 1024×1024) and overrides the manual size. On by
     default. A single texture is simply re-emitted (padded to power-of-two).
     Materials that have **only a base color** (no image) get a small solid
     tile baked into the atlas, so flat-colored materials show up too. Mixed
     models (some textured, some flat-colored) work in one export.
   - **Texture width/height** — used only when atlasing is **off**; power-of-two
     8…1024.
   - **Add '.bin' suffix** — `model_dsm.bin` / `model_<anim>_dsa.bin` for direct
     BlocksDS embedding (matches the Nitro Engine examples). On by default.
   - **Export base pose** — also write `model.dsa` for the rest pose.
   - **Export animations** — write one DSA per action.
   - **Skip frames** — decimate animation frames (0 = all, 1 = half, …).
   - **Model scale** — uniform scale for mesh + skeleton. DS vertex coordinates
     must stay within ±8.0, so larger models must be scaled down here and scaled
     back up on the DS with `NE_ModelScale`. The export aborts with a suggested
     value if the geometry is too large.
   - **Blender fix** — rotate −90° on X to convert Blender Z-up to DS Y-up.
     Leave on (matches the examples' `--blender-fix`).
   - **Reverse winding** — off by default; Blender's native triangle order
     already matches the DS front-face convention (`NE_CULL_BACK`). Enable only
     if faces appear inside-out on hardware.
   - **data/ and graphics/ layout** — write DSM/DSA into `data/` and textures
     into `graphics/`, mirroring the Nitro Engine examples.

## Hard constraints (enforced at export)

These come from the DS hardware / the DSMA format and the export aborts with a
clear message if they are violated:

- **≤ 30 bones** — limited by the DS matrix stack.
- **Exactly one bone per vertex, weight 1.0** — rigid skinning only. Vertices
  with multiple influences or none are rejected.
- **A UV map is required.**
- **Power-of-two texture sizes** (8…1024); a non-power-of-two image only emits a
  warning, but it will not work on hardware.

## Known limitations

- **One texture space per `.dsm`.** A DS model is a single display list with one
  set of texture coordinates. With **Pack textures into one atlas** enabled
  (default) all textures are combined into one `<model>_atlas.png` and the UVs
  are remapped automatically, so multi-material meshes work out of the box.
  Textures that tile (UVs outside the 0…1 range) are not supported by atlasing.
  With atlasing disabled, each texture is exported separately and only one of
  them is actually usable per model.
- Modifiers are **not** applied automatically; apply them (mirror, subsurf, …)
  before exporting, or the rest mesh data is used as-is.
- Bone scale is ignored (only rotation + translation are used), as in the DSMA
  format.

## Project layout

| File | Role |
| --- | --- |
| `__init__.py` | Operator, export dialog, menu registration |
| `blender_extract.py` | Reads Blender mesh/armature → joints/meshes/frames |
| `dsma_export.py` | Writes `.dsm` and `.dsa` (ported from `md5_to_dsma.py`) |
| `display_list.py` | Display-list encoder (verbatim from Nitro Engine) |
| `md5_math.py` | Vector/Quaternion math (from the tool) + joint-space inverse |
| `texture_export.py` | PNG + grit writer |

## License

MIT — see [LICENSE](LICENSE). `display_list.py` and the DSM/DSA conversion math
are derived from Nitro Engine's `md5_to_dsma` tool (also MIT).
