# Character packs

Character packs provide a static fallback and optional VFX. A reaction pack supplies the **character body sprite grid**; it is not a separate bubble sticker.

## Directories and priority

Bundled packs live under the installation directory; user packs use the same layout below `~/.engram` and take priority when valid.

```text
resource/character/sets/<id>/manifest.yaml
resource/character/sets/<id>/character.png
resource/character/sets/<id>/effects/idle.png
resource/character/sets/<id>/effects/click.png

~/.engram/character/sets/<id>/manifest.yaml
~/.engram/character/sets/<id>/character.png
~/.engram/character/sets/<id>/effects/idle.png
~/.engram/character/sets/<id>/effects/click.png

resource/character/reactions/<id>/manifest.yaml
resource/character/reactions/<id>/states.png

~/.engram/character/reactions/<id>/manifest.yaml
~/.engram/character/reactions/<id>/states.png
```

For both types, a valid user manifest is selected before the bundled manifest. A missing or invalid pack is disabled silently; legacy character/effects configuration remains available as fallback.

## Settings GUI source modes

The overlay settings window has three mutually exclusive character-source modes. Switching modes only changes which inputs are enabled; it does not erase values for the other modes.

| GUI mode | Persisted value | Required selection |
|---|---|---|
| `단일 이미지` | `static` | A readable `.png` file |
| `애니메이션 폴더` | `sequence` | A directory of animation frames |
| `스프라이트 그리드` | `sprite_grid` | A `.png`, columns, rows, cell width/height and `#RRGGBB` chroma key |

For an inline custom grid selected in the GUI, saving is blocked unless its dimensions are exactly `columns * cell_width` by `rows * cell_height`. The GUI inline format intentionally supplies generic default/idle/hover/click/input/work/error states using cell `0`; it cannot infer which drawing represents a semantic event. Use a reaction-pack manifest for a custom event-to-cell mapping, frame rotation, transforms, or VFX.

## Character-set manifest

`manifest.yaml` for a character set uses schema version 1. Extra fields are ignored.

```yaml
schema_version: 1
id: engram
display_name: Engram
character: character.png
effects:
  idle:
    asset: effects/idle.png
    thickness_px: 2
  click:
    asset: effects/click.png
    thickness_px: 3
```

Set `overlay.character.set` to the pack id. When `overlay.character.name` is empty or equals that id, the manifest `character` image is used. An absolute `character.name` path (and existing numbered-frame/name discovery) keeps its established priority, so existing custom image setups are not replaced by a set.

Manifest VFX assets take precedence over the legacy `overlay.character.effects.idle_asset` and `click_asset` paths. If a manifest does not provide an effect, the corresponding legacy path is used. `thickness_px` is clamped to 1–6 and expands the downscaled pixel-art VFX so thin lines remain visible; manifest thickness applies to manifest assets, while legacy `idle_thickness_px` and `click_thickness_px` apply to legacy assets.

## Reaction-pack manifest

Reaction packs are selected with `overlay.character.reactions.pack` and used when `overlay.character.source_mode: sprite_grid`. Their schema has a sprite grid and declarative `states`.

```yaml
schema_version: 1
id: engram
sprite_sheet: states.png
chroma_key: "#00FF00"
crop_y_offset_px: 32
grid:
  columns: 6
  rows: 4
  cell_width: 434
  cell_height: 408
states:
  default: { frames: [18], selection: fixed, frame_ms: 900, transform: none, vfx: none }
  idle: { frames: [18, 19, 20, 21, 22], selection: shuffle, frame_ms: 7200, transform: none, vfx: idle }
  click: { frames: [9, 10, 11], selection: random, frame_ms: 1000, dwell_ms: 1000, transform: none, vfx: sparkle_burst }
  input: { frames: [12, 14], selection: random, frame_ms: 1600, dwell_ms: 1600, transform: none, vfx: none }
  success: { frames: [16], selection: fixed, frame_ms: 2400, dwell_ms: 2400, transform: none, vfx: none }
```

Each state declares `frames`, `selection` (`fixed`, `random`, `sequence`, `sequence_once`, or `shuffle`), `frame_ms`, optional `dwell_ms`, `transform` (`none`, `breathe_mirror`, `hflip_squash`) and `vfx` (`none`, `twinkle`, `sparkle_burst`). `breathe_mirror` is a mild breathing squash with a random horizontal mirror; `hflip_squash` repeats a horizontal flip while vertically squashing; `twinkle` and `sparkle_burst` name the bundled subtle and burst-spark effects. The former event labels (`idle`, `hover`, `click`) plus the prior semantic names `hover_flip_squash`, `alternating_mirror_squash`, and `sparkle` remain valid read aliases and are normalized when the GUI saves. `shuffle` shows every frame exactly once per cycle and avoids an immediate repeat at a cycle boundary. `sequence_once` advances once and holds its final frame until the state dwell expires. Cell indexes start at zero and must be inside `columns * rows`. `chroma_key` must be a six-digit `#RRGGBB` value. The configured pack normally applies only when the selected character name has the same stem as the pack id. Set `apply_to_custom: true` to deliberately use it with another custom character.

The bundled Engram sheet has a small strip of the previous row at the top of later cells, so its manifest uses `crop_y_offset_px: 32`. The renderer removes only that top gutter, then aspect-fits the complete remaining image to the target height; it does not add transparent bottom padding or discard the current cell's bottom. Custom packs may use any value from zero through `cell_height - 1`. Users can override the manifest through `overlay.character.reactions.crop_y_offset_px` in `~/.engram/overlay.user.yaml`.

The grid may use any positive N-by-M dimensions. The sprite sheet itself must be exactly `(columns * cell_width)` pixels wide and `(rows * cell_height)` pixels high. Indexes advance from left to right and then top to bottom; for a 6-by-4 sheet the rows contain `0..5`, `6..11`, `12..17`, and `18..23`.

Reaction selection uses only the public bubble-event contract. A normal thought maps to `thought`; deep/long-thinking keywords map to `deep_thought`; tool invocation maps to `tool_use`; tool results and normal turn completion map to `success`; errors, waiting/retry text, and permission/blocked text map to their corresponding states. It does not inspect hidden chain-of-thought or other private event fields.

The full sheet is loaded once. Cells are lazily cropped, chroma-keyed, resized to the character height, and cached. Public bubble events select states: input → `input`, thought → `thought`, memory/search tools → `memory`/`search`, normal tools → `generating`, complete → `success`, provider failure → `provider_error`, other failure → `error`. Priority is error > success > click > hover > input > work > idle.

The bundled `engram` manifest is the default deployment layout: default `18`, idle candidates `19..22` (the actual idle shuffle pool includes default `18`, so `18..22`), hover `17`, click `9/10/11`, input `12/14`, generating `14`, search `0..4`, thought `5/7/13`, memory `6`, success `16`, provider error `8`, and other error `15`. Idle uses a 7200ms shuffle cycle; each click chooses one of `9/10/11` and holds it for 1000ms; input holds its random choice for 1600ms; success lasts 2400ms. The bundled click VFX is restricted to the default Engram character/Engram sprite pack and is not automatically composited over custom sources. Each state may independently choose its frame selection and declare `transform`/`vfx`; this is how a custom pack associates a state with its own pre-defined animation set.

## Safety rules

Every manifest asset path must be relative to its own pack directory. Absolute paths and any path containing `..` are rejected, including for `character`, VFX assets, and `sprite_sheet`. The manifest must reference files that exist inside that directory. Invalid YAML, missing required fields, invalid grid values, or invalid mapping indexes disable only that pack rather than preventing the overlay from starting.

## Live edits

The running overlay polls `overlay.yaml`, `~/.engram/overlay.user.yaml`, and the active character/reaction manifest plus its selected PNG assets. A completed save is applied in about one second without restarting; settings saved through the GUI call the same reload immediately. In a frozen install located inside a verified `Project_Engram` checkout, those editable checkout resources are used before the bundled `_internal` copy. User packs still take priority. During a partial or invalid YAML/image save, the last good profile and image cache remain visible; save a valid version to apply it on the next poll.

## GUI manifest editing and placement persistence

The Overlay settings tab includes a compact **Sprite state manifest** panel for `sprite_grid` packs. Select a state and edit comma-separated `frames`, `selection`, `frame_ms`, optional `dwell_ms`, `transform`, and `vfx`. The editor validates cell bounds, supported enum values, and positive timings. It preserves unknown state/manifest fields.

Built-in packs are read-only: the first GUI save or Advanced YAML open copies the active pack directory to `~/.engram/character/reactions/<id>/`, then writes its `manifest.yaml` by temporary-file replacement. This user pack takes precedence and is hot-reloaded.

Overlay-window placement and manually dragged speech/thought anchors are runtime state in `~/.engram/overlay.state.yaml`, separate from the editable configuration. They survive restart/rebuild and are clamped to the work area of the closest currently available monitor. Clearing a conversation does not reset those placements.
