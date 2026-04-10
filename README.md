![Brazil Banner](https://iili.io/BGIqZYl.png)

# S27 YTD Manager

Blender add-on for building, organizing, resizing, exporting, and reinjecting GTA V `.ytd` texture packages from Sollumz assets.

Designed for production-oriented workflows that need clean texture packaging, fast iteration, and predictable output from inside Blender.

### Prerequisites

**Texconv.exe** is required for texture conversion and compression. You can download the latest release from the official Microsoft GitHub repository:

**Download:** [Texconv · microsoft/DirectXTex Wiki](https://github.com/microsoft/DirectXTex/wiki/texconv)

> **Note:** After downloading, ensure you place the `texconv.exe` path file in the specify its location in the add-on preferences.

## Overview

`S27 YTD Manager` turns Sollumz Drawable and Fragment assets into export-ready YTD texture packages without requiring manual texture bookkeeping.

The add-on can:

- scan selected assets
- build unique texture lists
- suggest compression automatically
- resize textures without upscaling
- export DDS files plus OpenFormats XML
- inject exported DDS files back into the scene

## Core Capabilities

| Feature | Description |
| --- | --- |
| Multi-pack workflow | Create and manage multiple YTD packs in the same Blender scene |
| Sollumz-aware asset scan | Collects textures from selected Drawable / Fragment roots using Sollumz material nodes |
| Unique texture grouping | Deduplicates logical textures inside each pack |
| Auto compression suggestion | Recommends `DXT1` or `DXT5` based on source and alpha usage |
| Manual compression override | Override textures to `Auto`, `DXT1`, `DXT5`, or `ARGB8` |
| Per-texture resize | Reduce texture size by limiting the largest side while preserving aspect ratio |
| Resize All | Apply one resize target to every unique texture in the active pack |
| Safe resize behavior | Never enlarges textures; only shrinks when the chosen value is smaller |
| Placeholder texture filtering | Ignores empty Blender placeholder textures so they do not export as black DDS files |
| Embedded texture support | Handles embedded-only, external-only, and mixed embedded/external textures |
| DDS export | Converts supported sources into DDS through `texconv.exe` |
| XML generation | Writes OpenFormats `.ytd.xml` with the exported dimensions and mip count |
| DDS reinjection | Loads exported DDS files back into Sollumz nodes for validation |
| Output cleanup | Removes stale DDS files that no longer belong to the current pack |

## Requirements

- Blender 5.0+
- Sollumz enabled
- `texconv.exe`

`texconv.exe` can be configured in the add-on preferences, or placed here:

```text
s27_ytd_manager/bin/texconv.exe
```

## Installation

1. Zip the `s27_ytd_manager` folder, or use a packaged zip release.
2. In Blender, go to `Edit > Preferences > Add-ons`.
3. Click `Install from Disk`.
4. Select the add-on zip.
5. Enable `S27 YTD Manager`.
6. Open the panel in:

```text
View3D > Sidebar > S27 YTD
```

## Quick Start

1. Enable Sollumz.
2. Configure `texconv.exe`
3. Create a pack with `Add YTD`.
4. Select valid Drawable / Fragment roots.
5. Click `Add Selected`.
6. Review the `Unique Textures` list.
7. Adjust `Compression` and `Resize` as needed.
8. Click `Export`.
9. Optionally click `Inject DDS`.

## Panel Workflow

### Texture Packages

Each YTD pack is an independent export group.

Use packs to:

- keep multiple YTDs in one scene
- isolate asset groups
- refresh one pack without touching the others
- export or inject one pack at a time

### Meshes

The mesh list shows which scanned assets belong to the active pack.

This helps you:

- confirm the selected roots
- remove an asset from a pack
- rebuild the texture list from the current scene state

### Unique Textures

This is the main review area for the pack.

For each texture, the add-on can display:

- source file name
- suggested compression
- dimensions
- alpha coverage hint
- sampler names
- embedded/external state
- duplicate-name warnings
- compression override
- resize target

## Compression System

### Auto Compression

When `Compression` is set to `Auto`, the add-on evaluates the texture using:

- source image format
- alpha channel presence
- alpha coverage percentage
- fake-alpha threshold preferences

### Manual Overrides

Each texture can be forced to:

- `Auto`
- `DXT1`
- `DXT5`
- `ARGB8`

This is useful when a texture should stay uncompressed or when a texture needs a manual format override.

## Resize System

### Per-Texture Resize

Each texture has its own `Resize` dropdown with valid smaller targets only.

Available values:

- `Original`
- `2048`
- `1024`
- `512`
- `256`
- `128`
- `64`
- `32`
- `16`
- `4`

The add-on never offers a value that would enlarge the image.

Examples:

- `1024x1024` can offer `512`, `256`, `128`, `64`, `32`, `16`, `4`
- `800x400` can offer `512`, `256`, `128`, `64`, `32`, `16`, `4`
- `400x800` follows the same rule because the largest side is used
- choosing `1024` for `800x400` would do nothing, so that value is not used as a shrink target

### Resize All

The `Resize All` controls apply one target across every unique texture in the active pack.

Behavior:

- only textures larger than the selected target are changed
- smaller textures stay untouched
- the aspect ratio is preserved automatically
- each texture still keeps its own final resize setting

## Export Behavior

### Supported Source Formats

The add-on can export from:

- `.png`
- `.jpg`
- `.jpeg`
- `.tga`
- `.bmp`
- `.tif`
- `.tiff`
- `.webp`
- `.dds`

### Missing Source File Fallback

If the original source file no longer exists on disk but the image is still loaded in Blender, the add-on creates a temporary PNG from the current image buffer and exports from that.

### Placeholder Image Protection

Empty Blender placeholder images, such as default generated `Texture` slots with no real source, are treated as missing textures and ignored during scanning/export.

This prevents accidental black DDS output from empty texture slots.

### Duplicate Texture Names

If two textures share the same logical texture name but point to different source files:

- the first discovered source is kept
- the texture is flagged with a warning
- the pack can still export, but the conflict is made visible in the UI

### XML File Names

The generated XML stores only the DDS file name:

```text
TextureName.dds
```

## Mipmap Rules

Mip generation is based on the final exported dimensions.

Rules:

- power-of-two textures use a full mip chain
- non-power-of-two textures export with `1` mip level only
- the XML `MipLevels` value matches the exported DDS behavior
- resize is evaluated before mip generation, so the mip rule always matches the final output size

Examples:

- `1024x1024` exports with a full mip chain
- `512x256` exports with a full mip chain
- `800x400` exports with `1` mip level
- `400x800` exports with `1` mip level

## Embedded Texture Handling

The add-on supports three common cases.

### External Only

- exported to the pack DDS folder
- included in the `.ytd.xml`

### Embedded Only

- exported only to the embedded cache
- not included in the `.ytd.xml`

### Mixed Embedded + External

- external DDS is written for YTD/XML use
- embedded DDS is written separately for reinjection

## Output Structure

```text
<Build Root>/
+-- ytd_name.ytd.xml
+-- <PackFolder>/
|   +-- texture_a.dds
|   `-- texture_b.dds
`-- EmbeddedTexture/
    `-- <PackFolder>/
        +-- texture_a.dds
        `-- texture_b.dds
```

## Injection Workflow

After export, `Inject DDS` or `Inject All DDS` can load the resulting DDS files back into the matching Sollumz texture nodes.

Useful for:

- validating compression visually
- checking resized textures directly in Blender
- confirming embedded/external routing

## Add-on Preferences

The preferences panel includes:

- `Texconv Path`
- `Default Build Folder`
- `Auto Alpha Tolerance`
- `Fake Alpha Cutoff`

These settings control where `texconv.exe` is found, where exports go by default, and how automatic alpha-based compression suggestions are calculated.

## Troubleshooting

### Panel appears but nothing else shows

The panel includes internal draw protection. If a UI error happens, the panel should display the error instead of silently appearing empty.

### Black DDS exports from empty slots

Refresh the pack and export again. Placeholder image filtering should prevent empty `Texture` slots from producing black DDS files.

### Texture is not being resized

Make sure:

- the selected resize value is smaller than the largest side of the source image
- the pack was refreshed after source changes
- you are checking the normal export output folder after export

### Export fails because `texconv.exe` is missing

Set `Texconv Path` in the add-on preferences, or place `texconv.exe` in:

```text
s27_ytd_manager/bin/texconv.exe
```

### No textures are found

Check that:

- Sollumz is enabled
- the selected object belongs to a valid Drawable or Fragment hierarchy
- the materials use supported Sollumz texture image nodes

## Summary

`S27 YTD Manager` is built to make YTD authoring inside Blender practical, fast, and predictable for GTA V workflows.

It combines:

- pack management
- texture scanning
- compression control
- resize tools
- mip handling
- clean DDS/XML export
- embedded texture support
- DDS reinjection

inside one Blender sidebar workflow.
