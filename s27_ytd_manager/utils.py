from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from xml.sax.saxutils import escape

import bpy

from .model import ADDON_ID, bulk_resize_choice_for_dimensions, clamp_resize_choice

try:
    import numpy as np
except Exception:
    np = None


SOLLUMZ_FRAGMENT = "sollumz_fragment"
SOLLUMZ_DRAWABLE = "sollumz_drawable"
SOLLUMZ_DRAWABLE_MODEL = "sollumz_drawable_model"

LOD_ATTRS = ("very_high", "high", "medium", "low", "very_low")
NORMAL_HINT_TOKENS = ("bump", "normal", "distance")
ALPHA_CAPABLE_SOURCE_EXTS = {
    ".png",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
    ".dds",
}
SUPPORTED_SOURCE_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".tga",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".dds",
}


def get_preferences(context) -> object | None:
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


def is_sollumz_available() -> bool:
    return hasattr(bpy.types.Object, "sollum_type") and hasattr(bpy.types.ShaderNodeTexImage, "sollumz_texture_name")


def sanitize_name(value: str) -> str:
    value = (value or "").strip()
    safe = "".join(ch if ch not in '<>:"/\\|?*' else "_" for ch in value)
    return safe.strip(" .") or "new_ytd"


def normalize_path(path: str) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(path))


def find_ytd_root(obj: bpy.types.Object | None) -> bpy.types.Object | None:
    while obj:
        parent = getattr(obj, "parent", None)
        obj_type = getattr(obj, "sollum_type", None)

        if obj_type == SOLLUMZ_FRAGMENT:
            return obj

        if obj_type == SOLLUMZ_DRAWABLE and (parent is None or getattr(parent, "sollum_type", None) != SOLLUMZ_FRAGMENT):
            return obj

        obj = parent

    return None


def selected_roots(context) -> list[bpy.types.Object]:
    roots: dict[int, bpy.types.Object] = {}
    for obj in context.selected_objects:
        root = find_ytd_root(obj)
        if root is not None:
            roots[root.as_pointer()] = root
    return list(roots.values())


def iter_asset_tree(root_obj: bpy.types.Object):
    yield root_obj
    for child in getattr(root_obj, "children_recursive", []):
        yield child


def iter_model_meshes(root_obj: bpy.types.Object):
    seen: set[int] = set()
    for child in iter_asset_tree(root_obj):
        if getattr(child, "sollum_type", None) != SOLLUMZ_DRAWABLE_MODEL:
            continue

        yielded_lod = False
        lods = getattr(child, "sz_lods", None)
        if lods is not None:
            for attr in LOD_ATTRS:
                lod = getattr(lods, attr, None)
                mesh = getattr(lod, "mesh", None) if lod is not None else None
                if mesh is None:
                    continue

                mesh_key = mesh.as_pointer()
                if mesh_key in seen:
                    continue

                seen.add(mesh_key)
                yielded_lod = True
                yield mesh

        if yielded_lod:
            continue

        mesh = getattr(child, "data", None)
        if mesh is not None:
            mesh_key = mesh.as_pointer()
            if mesh_key not in seen:
                seen.add(mesh_key)
                yield mesh


def derive_texture_name(image: bpy.types.Image | None) -> str:
    if image is None:
        return ""

    filepath = bpy.path.abspath(image.filepath) if image.filepath else ""
    if filepath:
        return os.path.splitext(os.path.basename(filepath))[0]

    return os.path.splitext(image.name)[0]


def resolve_image_source_path(image: bpy.types.Image | None) -> str:
    if image is None or not image.filepath:
        return ""

    path = bpy.path.abspath(image.filepath)
    if path and os.path.isfile(path):
        return os.path.abspath(path)

    return ""


def is_placeholder_image(image: bpy.types.Image | None) -> bool:
    if image is None:
        return True

    if image.filepath or getattr(image, "packed_file", None):
        return False

    if getattr(image, "source", "") not in {"GENERATED", "VIEWER"}:
        return False

    image_name = (image.name or "").strip().lower()
    return image_name == "texture" or image_name.startswith("texture.")


def is_supported_source_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_SOURCE_EXTS


def is_sollumz_texture_node(node: bpy.types.Node) -> bool:
    if not isinstance(node, bpy.types.ShaderNodeTexImage):
        return False

    return bool(getattr(node, "is_sollumz", False) or hasattr(node, "sollumz_texture_name"))


def collect_asset_texture_refs(root_obj: bpy.types.Object) -> list[dict]:
    refs: list[dict] = []
    seen: set[tuple] = set()

    for mesh in iter_model_meshes(root_obj):
        for mat in getattr(mesh, "materials", []):
            if mat is None or not getattr(mat, "use_nodes", False) or mat.node_tree is None:
                continue

            for node in mat.node_tree.nodes:
                if not is_sollumz_texture_node(node):
                    continue

                image = node.image
                if image is None or is_placeholder_image(image):
                    continue

                texture_name = getattr(node, "sollumz_texture_name", "") or derive_texture_name(image)
                if not texture_name:
                    continue

                key = (mat.as_pointer(), node.name, texture_name)
                if key in seen:
                    continue
                seen.add(key)

                embedded = bool(getattr(getattr(node, "texture_properties", None), "embedded", False))
                refs.append(
                    {
                        "material": mat,
                        "image": image,
                        "sampler_name": node.name,
                        "node_name": node.name,
                        "texture_name": texture_name,
                        "source_path": resolve_image_source_path(image),
                        "embedded": embedded,
                        "color_space": image.colorspace_settings.name,
                    }
                )

    return refs


def _append_csv_value(csv_text: str, value: str) -> str:
    values = [item for item in csv_text.split(",") if item]
    if value and value not in values:
        values.append(value)
    return ",".join(values)


def _alpha_cutoff_value(prefs) -> float:
    cutoff = getattr(prefs, "fake_alpha_cutoff", 250) if prefs else 250
    return max(0.0, min(255.0, float(cutoff))) / 255.0


def compute_alpha_coverage_pct(image: bpy.types.Image | None, prefs) -> float:
    if image is None:
        return 0.0

    width, height = image.size
    if width <= 0 or height <= 0 or getattr(image, "channels", 4) < 4:
        return 0.0

    try:
        pixels = image.pixels[:]
    except Exception:
        return 0.0

    pixel_count = len(pixels) // 4
    if pixel_count <= 0:
        return 0.0

    cutoff = _alpha_cutoff_value(prefs)

    if np is not None:
        alpha = np.array(pixels[3::4], dtype=np.float32)
        count = int(np.count_nonzero(alpha < cutoff))
    else:
        count = 0
        for idx in range(3, len(pixels), 4):
            if pixels[idx] < cutoff:
                count += 1

    return (count / pixel_count) * 100.0


def _sampler_prefers_data(sampler_hints: str) -> bool:
    text = (sampler_hints or "").lower()
    return any(token in text for token in NORMAL_HINT_TOKENS)


def _texture_source_extension(texture_item) -> str:
    source_path = bpy.path.abspath(texture_item.source_path) if texture_item.source_path else ""
    return os.path.splitext(source_path)[1].lower() if source_path else ""


def suggest_compression(texture_item, prefs) -> str:
    source_ext = _texture_source_extension(texture_item)
    if source_ext and source_ext not in ALPHA_CAPABLE_SOURCE_EXTS:
        return "DXT1"

    image = texture_item.image
    if image is None:
        return "DXT1"

    if getattr(image, "channels", 4) < 4:
        return "DXT1"

    alpha_pct = compute_alpha_coverage_pct(image, prefs)
    tolerance = getattr(prefs, "alpha_tolerance_pct", 2.0) if prefs else 2.0
    return "DXT5" if alpha_pct > tolerance else "DXT1"


def describe_suggestion(texture_item) -> str:
    source_ext = _texture_source_extension(texture_item)
    if source_ext and source_ext not in ALPHA_CAPABLE_SOURCE_EXTS:
        return f"Source format {source_ext}"

    image = texture_item.image
    if image is None:
        return "Default"

    if getattr(image, "channels", 4) < 4:
        return "No alpha channel"

    return "Alpha scan"


def should_review_sampler_alpha(texture_item) -> bool:
    return _sampler_prefers_data(texture_item.sampler_hints) and texture_item.suggested_compression == "DXT1"


def refresh_unique_texture_metadata(texture_item, prefs):
    image = texture_item.image
    if image is not None:
        texture_item.width = int(image.size[0])
        texture_item.height = int(image.size[1])
        texture_item.alpha_coverage_pct = compute_alpha_coverage_pct(image, prefs)
        texture_item.suggested_compression = suggest_compression(texture_item, prefs)
        texture_item.suggested_reason = describe_suggestion(texture_item)
    else:
        texture_item.width = 0
        texture_item.height = 0
        texture_item.alpha_coverage_pct = 0.0
        texture_item.suggested_compression = "DXT1"
        texture_item.suggested_reason = "Default"

    texture_item.source_exists = bool(texture_item.source_path and os.path.isfile(bpy.path.abspath(texture_item.source_path)))
    texture_item.resize_max_dimension = clamp_resize_choice(
        getattr(texture_item, "resize_max_dimension", "ORIGINAL"),
        texture_item.width,
        texture_item.height,
    )


def get_texture_dimensions(texture_item) -> tuple[int, int]:
    width = int(getattr(texture_item, "width", 0) or 0)
    height = int(getattr(texture_item, "height", 0) or 0)

    image = getattr(texture_item, "image", None)
    if (width <= 0 or height <= 0) and image is not None:
        image_width, image_height = image.size
        width = width or int(image_width)
        height = height or int(image_height)

    return max(0, width), max(0, height)


def get_texture_resize_limit(texture_item) -> int | None:
    width, height = get_texture_dimensions(texture_item)
    resize_choice = clamp_resize_choice(getattr(texture_item, "resize_max_dimension", "ORIGINAL"), width, height)
    if resize_choice == "ORIGINAL":
        return None
    return int(resize_choice)


def calculate_resize_dimensions(width: int, height: int, resize_limit: int | None) -> tuple[int, int]:
    width = int(width or 0)
    height = int(height or 0)
    if width <= 0 or height <= 0 or resize_limit is None:
        return max(0, width), max(0, height)

    largest_side = max(width, height)
    if largest_side <= resize_limit:
        return width, height

    scale = resize_limit / float(largest_side)
    resized_width = max(1, int(round(width * scale)))
    resized_height = max(1, int(round(height * scale)))
    return resized_width, resized_height


def get_texture_output_dimensions(texture_item) -> tuple[int, int]:
    width, height = get_texture_dimensions(texture_item)
    return calculate_resize_dimensions(width, height, get_texture_resize_limit(texture_item))


def describe_resize_setting(texture_item) -> str:
    width, height = get_texture_dimensions(texture_item)
    if width <= 0 or height <= 0:
        return "Resize: dimensions unavailable"

    output_width, output_height = get_texture_output_dimensions(texture_item)
    if output_width == width and output_height == height:
        return f"Resize: keeps {width}x{height}"
    return f"Resize: {width}x{height} -> {output_width}x{output_height}"


def apply_resize_all_to_pack(pack) -> tuple[int, int]:
    changed = 0
    resized = 0
    target = getattr(pack, "resize_all_target", "512")

    for texture in pack.textures:
        width, height = get_texture_dimensions(texture)
        new_choice = bulk_resize_choice_for_dimensions(width, height, target)
        if new_choice != "ORIGINAL":
            resized += 1
        if texture.resize_max_dimension != new_choice:
            texture.resize_max_dimension = new_choice
            changed += 1

    return changed, resized


def _find_texture_item(pack, texture_name: str):
    for item in pack.textures:
        if item.texture_name == texture_name:
            return item
    return None


def _capture_texture_settings(pack) -> dict[str, dict]:
    settings = {}
    for item in pack.textures:
        texture_name = getattr(item, "texture_name", "")
        if not texture_name:
            continue
        settings[texture_name] = {
            "compression": getattr(item, "compression", "AUTO"),
            "resize_max_dimension": getattr(item, "resize_max_dimension", "ORIGINAL"),
            "expanded": bool(getattr(item, "expanded", False)),
        }
    return settings


def rebuild_pack_from_assets(context, pack):
    prefs = get_preferences(context)
    existing_texture_settings = _capture_texture_settings(pack)
    pack.textures.clear()

    for asset in pack.assets:
        asset.textures.clear()
        root_obj = asset.asset
        if root_obj is None:
            continue

        asset.asset_name = root_obj.name
        asset.asset_type = getattr(root_obj, "sollum_type", "")

        refs = collect_asset_texture_refs(root_obj)
        for ref in refs:
            ref_item = asset.textures.add()
            ref_item.material = ref["material"]
            ref_item.image = ref["image"]
            ref_item.sampler_name = ref["sampler_name"]
            ref_item.node_name = ref["node_name"]
            ref_item.texture_name = ref["texture_name"]
            ref_item.source_path = ref["source_path"]
            ref_item.embedded = ref["embedded"]
            ref_item.color_space = ref["color_space"]

            unique = _find_texture_item(pack, ref["texture_name"])
            if unique is None:
                unique = pack.textures.add()
                unique.texture_name = ref["texture_name"]
                unique.image = ref["image"]
                unique.source_path = ref["source_path"]
                unique.embedded = ref["embedded"]
                unique.embedded_ref_count = 1 if ref["embedded"] else 0
                unique.external_ref_count = 0 if ref["embedded"] else 1
                unique.sampler_hints = ref["sampler_name"]
                unique.ref_count = 1
                refresh_unique_texture_metadata(unique, prefs)
                existing_settings = existing_texture_settings.get(ref["texture_name"])
                if existing_settings:
                    unique.compression = existing_settings["compression"]
                    unique.resize_max_dimension = clamp_resize_choice(
                        existing_settings["resize_max_dimension"],
                        unique.width,
                        unique.height,
                    )
                    unique.expanded = existing_settings["expanded"]
            else:
                unique.ref_count += 1
                unique.embedded = unique.embedded or ref["embedded"]
                unique.embedded_ref_count += 1 if ref["embedded"] else 0
                unique.external_ref_count += 0 if ref["embedded"] else 1
                unique.sampler_hints = _append_csv_value(unique.sampler_hints, ref["sampler_name"])
                if not ref["embedded"] and ref["image"] is not None:
                    unique.image = ref["image"]
                if not ref["embedded"] and ref["source_path"]:
                    unique.source_path = ref["source_path"]
                if not unique.image and ref["image"] is not None:
                    unique.image = ref["image"]
                if not unique.source_path and ref["source_path"]:
                    unique.source_path = ref["source_path"]

                existing_source = normalize_path(unique.source_path)
                new_source = normalize_path(ref["source_path"])
                if existing_source and new_source and existing_source != new_source:
                    unique.has_conflict = True
                    unique.warning = "Duplicate logical texture name with different source files. The first source will be used."

                refresh_unique_texture_metadata(unique, prefs)

            ref_item.pack_texture_name = unique.texture_name

    pack.status = f"{len(pack.assets)} asset(s), {len(pack.textures)} unique texture(s)"


def add_selected_assets_to_pack(context, pack) -> int:
    existing = {asset.asset.as_pointer() for asset in pack.assets if asset.asset is not None}
    count = 0

    for root in selected_roots(context):
        if root.as_pointer() in existing:
            continue
        item = pack.assets.add()
        item.asset = root
        item.asset_name = root.name
        item.asset_type = getattr(root, "sollum_type", "")
        count += 1

    rebuild_pack_from_assets(context, pack)
    return count


def remove_asset_from_pack(context, pack, asset_index: int):
    if 0 <= asset_index < len(pack.assets):
        pack.assets.remove(asset_index)
        pack.active_asset_index = min(max(0, pack.active_asset_index), max(0, len(pack.assets) - 1))
        rebuild_pack_from_assets(context, pack)


def get_build_root(context) -> str:
    scene_dir = getattr(context.scene, "s27_ytd_build_dir", "") or ""
    prefs = get_preferences(context)
    root = scene_dir or (prefs.default_output_dir if prefs else "") or "//S27_YTD_Build"
    return bpy.path.abspath(root)


def get_export_root(context, pack) -> str:
    return get_build_root(context)


def get_pack_folder_name(pack) -> str:
    return sanitize_name(pack.output_subdir or pack.name)


def get_pack_output_dir(context, pack, export_root: str | None = None) -> str:
    root = export_root or get_export_root(context, pack)
    folder_name = get_pack_folder_name(pack)
    return os.path.join(root, folder_name)


def get_embedded_output_dir(context, pack, export_root: str | None = None) -> str:
    root = export_root or get_export_root(context, pack)
    return os.path.join(root, "EmbeddedTexture", get_pack_folder_name(pack))


def get_texconv_path(context) -> str:
    prefs = get_preferences(context)
    candidates = []
    if prefs and prefs.texconv_path:
        candidates.append(bpy.path.abspath(prefs.texconv_path))

    local_candidate = os.path.join(os.path.dirname(__file__), "bin", "texconv.exe")
    candidates.append(local_candidate)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)

    return ""


def _save_image_copy_to_png(image: bpy.types.Image, filepath: str):
    temp_image = image.copy()
    try:
        temp_image.filepath_raw = filepath
        temp_image.file_format = "PNG"
        temp_image.save()
    finally:
        bpy.data.images.remove(temp_image)


def _image_has_exportable_data(image: bpy.types.Image | None) -> bool:
    if image is None:
        return False

    width, height = image.size
    if width <= 0 or height <= 0:
        return False

    has_data = getattr(image, "has_data", None)
    if has_data is not None:
        return bool(has_data)

    try:
        _ = image.pixels[0]
    except Exception:
        return False

    return True


def prepare_source_file(texture_item, tmpdir: str) -> tuple[str, str | None]:
    texture_name = sanitize_name(texture_item.texture_name)
    source_path = bpy.path.abspath(texture_item.source_path) if texture_item.source_path else ""

    if source_path and os.path.isfile(source_path) and is_supported_source_path(source_path):
        ext = os.path.splitext(source_path)[1].lower()
        temp_input = os.path.join(tmpdir, f"{texture_name}{ext}")
        shutil.copy2(source_path, temp_input)
        return temp_input, None

    if texture_item.image is None or is_placeholder_image(texture_item.image):
        return "", f"Texture '{texture_item.texture_name}' has no source image."

    if not _image_has_exportable_data(texture_item.image):
        return "", f"Texture '{texture_item.texture_name}' has no image data."

    temp_input = os.path.join(tmpdir, f"{texture_name}.png")
    try:
        _save_image_copy_to_png(texture_item.image, temp_input)
    except Exception as exc:
        return "", f"Texture '{texture_item.texture_name}' could not be exported from Blender image data: {exc}"

    return temp_input, None


def resolve_compression(texture_item) -> str:
    return texture_item.compression if texture_item.compression != "AUTO" else texture_item.suggested_compression


def is_power_of_two(value: int) -> bool:
    value = int(value or 0)
    return value > 0 and (value & (value - 1)) == 0


def uses_single_mip_level(width: int, height: int) -> bool:
    width = int(width or 0)
    height = int(height or 0)
    if width <= 0 or height <= 0:
        return True
    return not (is_power_of_two(width) and is_power_of_two(height))


def get_mip_level_count(width: int, height: int) -> int:
    width = max(1, int(width or 1))
    height = max(1, int(height or 1))

    if uses_single_mip_level(width, height):
        return 1

    levels = 1
    while width > 1 or height > 1:
        width = max(1, width // 2)
        height = max(1, height // 2)
        levels += 1
    return levels


def compression_to_texconv_flag(compression: str) -> tuple[str, str]:
    if compression == "DXT1":
        return "DXT1", "D3DFMT_DXT1"
    if compression == "DXT5":
        return "DXT5", "D3DFMT_DXT5"
    if compression == "ARGB8":
        return "R8G8B8A8_UNORM", "D3DFMT_A8B8G8R8"
    raise ValueError(f"Unsupported compression '{compression}'")


def run_texconv(
    texconv_path: str,
    input_path: str,
    output_dir: str,
    compression: str,
    width: int,
    height: int,
    resize_image: bool = False,
) -> str:
    texconv_format, _ = compression_to_texconv_flag(compression)
    mip_count = get_mip_level_count(width, height)
    command = [
        texconv_path,
        "-nologo",
        "-y",
        "-m",
        str(mip_count),
        "-f",
        texconv_format,
    ]

    if resize_image:
        command.extend(
            [
                "-w",
                str(width),
                "-h",
                str(height),
            ]
        )

    command.extend(
        [
            "-o",
            output_dir,
            input_path,
        ]
    )

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "texconv failed without output."
        raise RuntimeError(message)

    base_name = os.path.splitext(os.path.basename(input_path))[0]
    candidates = (
        os.path.join(output_dir, f"{base_name}.dds"),
        os.path.join(output_dir, f"{base_name}.DDS"),
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    raise RuntimeError(f"texconv succeeded but no DDS file was found for '{base_name}'.")


def build_ytd_xml(texture_rows: list[dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', "<TextureDictionary>"]

    for row in texture_rows:
        lines.extend(
            [
                "  <Item>",
                f"    <Name>{escape(row['name'])}</Name>",
                '    <Unk32 value="0" />',
                "    <Usage>DEFAULT</Usage>",
                "    <UsageFlags>NOT_HALF</UsageFlags>",
                '    <ExtraFlags value="0" />',
                f'    <Width value="{row["width"]}" />',
                f'    <Height value="{row["height"]}" />',
                f'    <MipLevels value="{row["mip_levels"]}" />',
                f"    <Format>{row['format']}</Format>",
                f"    <FileName>{escape(row['file_name'])}</FileName>",
                "  </Item>",
            ]
        )

    lines.append("</TextureDictionary>")
    return "\n".join(lines) + "\n"


def _cleanup_stale_dds(output_dir: str, keep_names: set[str]) -> int:
    if not os.path.isdir(output_dir):
        return 0

    removed = 0
    for entry in os.listdir(output_dir):
        full_path = os.path.join(output_dir, entry)
        if not os.path.isfile(full_path):
            continue
        if os.path.splitext(entry)[1].lower() != ".dds":
            continue
        if entry not in keep_names:
            os.remove(full_path)
            removed += 1
    return removed


def export_pack(context, pack) -> tuple[str, int, int]:
    rebuild_pack_from_assets(context, pack)

    texconv_path = get_texconv_path(context)
    if not texconv_path:
        raise RuntimeError("texconv.exe not found. Configure it in the add-on preferences.")

    if not pack.textures:
        raise RuntimeError("This YTD pack has no textures to export.")

    build_root = get_export_root(context, pack)
    output_dir = get_pack_output_dir(context, pack, build_root)
    pack_folder_name = get_pack_folder_name(pack)
    embedded_output_dir = get_embedded_output_dir(context, pack, build_root)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(embedded_output_dir, exist_ok=True)

    exported_rows: list[dict] = []
    ytd_keep_names: set[str] = set()
    embedded_keep_names: set[str] = set()
    skipped_missing_count = 0
    with tempfile.TemporaryDirectory(prefix="s27_ytd_") as tmpdir:
        for texture in pack.textures:
            wants_ytd = texture.external_ref_count > 0
            wants_embedded = texture.embedded_ref_count > 0

            if not wants_ytd and not wants_embedded:
                continue

            input_path, skip_reason = prepare_source_file(texture, tmpdir)
            if not input_path:
                texture.exported_path = ""
                texture.embedded_exported_path = ""
                skipped_missing_count += 1
                continue

            resolved_compression = resolve_compression(texture)
            _, ytd_format = compression_to_texconv_flag(resolved_compression)
            final_name = f"{sanitize_name(texture.texture_name)}.dds"
            source_width, source_height = get_texture_dimensions(texture)
            output_width, output_height = get_texture_output_dimensions(texture)
            should_resize = output_width > 0 and output_height > 0 and (
                output_width != source_width or output_height != source_height
            )
            mip_levels = get_mip_level_count(output_width, output_height)
            if wants_ytd:
                dds_path = run_texconv(
                    texconv_path,
                    input_path,
                    output_dir,
                    resolved_compression,
                    output_width,
                    output_height,
                    resize_image=should_resize,
                )
                final_path = os.path.join(output_dir, final_name)
                if normalize_path(dds_path) != normalize_path(final_path):
                    if os.path.isfile(final_path):
                        os.remove(final_path)
                    os.replace(dds_path, final_path)
                    dds_path = final_path

                texture.exported_path = dds_path
                ytd_keep_names.add(final_name)
                exported_rows.append(
                    {
                        "name": texture.texture_name,
                        "width": output_width,
                        "height": output_height,
                        "mip_levels": mip_levels,
                        "format": ytd_format,
                        "file_name": os.path.basename(dds_path),
                    }
                )
            else:
                texture.exported_path = ""

            if wants_embedded:
                embedded_dds_path = run_texconv(
                    texconv_path,
                    input_path,
                    embedded_output_dir,
                    resolved_compression,
                    output_width,
                    output_height,
                    resize_image=should_resize,
                )
                embedded_final_path = os.path.join(embedded_output_dir, final_name)
                if normalize_path(embedded_dds_path) != normalize_path(embedded_final_path):
                    if os.path.isfile(embedded_final_path):
                        os.remove(embedded_final_path)
                    os.replace(embedded_dds_path, embedded_final_path)
                    embedded_dds_path = embedded_final_path

                texture.embedded_exported_path = embedded_dds_path
                embedded_keep_names.add(final_name)
            else:
                texture.embedded_exported_path = ""

    if not exported_rows:
        if skipped_missing_count > 0:
            raise RuntimeError(f"No exportable textures were written. Skipped {skipped_missing_count} missing/no-data texture(s).")
        raise RuntimeError("No textures are enabled for export in this YTD pack.")

    removed_ytd = _cleanup_stale_dds(output_dir, ytd_keep_names)
    removed_embedded = _cleanup_stale_dds(embedded_output_dir, embedded_keep_names)

    legacy_xml_path = os.path.join(output_dir, f"{sanitize_name(pack.name)}.ytd.xml")
    if os.path.isfile(legacy_xml_path):
        os.remove(legacy_xml_path)

    xml_path = os.path.join(build_root, f"{sanitize_name(pack.name)}.ytd.xml")
    with open(xml_path, "w", encoding="utf-8") as handle:
        handle.write(build_ytd_xml(exported_rows))

    pack.last_export_dir = output_dir
    pack.status = (
        f"Exported {len(exported_rows)} texture(s) to {output_dir}, "
        f"skipped {skipped_missing_count} missing/no-data texture(s), "
        f"cleaned {removed_ytd + removed_embedded} stale DDS file(s), XML in {build_root}"
    )
    return xml_path, len(exported_rows), skipped_missing_count


def inject_pack(context, pack) -> int:
    output_dir = pack.last_export_dir or get_pack_output_dir(context, pack)
    if not output_dir or not os.path.isdir(output_dir):
        raise RuntimeError("Export folder not found. Export the YTD pack before injecting DDS files.")

    texture_map = {texture.texture_name: texture for texture in pack.textures}
    if not texture_map:
        raise RuntimeError("There are no enabled textures to inject.")

    injected = 0
    for asset in pack.assets:
        for ref in asset.textures:
            texture = texture_map.get(ref.pack_texture_name)
            if texture is None:
                continue

            if ref.embedded:
                dds_path = texture.embedded_exported_path or os.path.join(
                    get_embedded_output_dir(context, pack),
                    f"{sanitize_name(texture.texture_name)}.dds",
                )
            else:
                dds_path = texture.exported_path or os.path.join(output_dir, f"{sanitize_name(texture.texture_name)}.dds")
            if not os.path.isfile(dds_path):
                continue

            material = ref.material
            if material is None or material.node_tree is None:
                continue

            node = material.node_tree.nodes.get(ref.node_name)
            if not isinstance(node, bpy.types.ShaderNodeTexImage):
                continue

            image = bpy.data.images.load(dds_path, check_existing=True)
            if _sampler_prefers_data(ref.sampler_name):
                image.colorspace_settings.is_data = True

            node.image = image
            ref.image = image
            ref.source_path = dds_path
            if ref.embedded:
                texture.embedded_exported_path = dds_path
            else:
                texture.image = image
                texture.source_path = dds_path
                texture.source_exists = True

            injected += 1

    pack.status = f"Injected {injected} DDS assignment(s)"
    return injected
