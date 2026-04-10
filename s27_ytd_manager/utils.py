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
DIFFUSE_SAMPLER_NAMES = {
    "diffusesampler",
    "diffusesampler2",
    "diffuseextrasampler",
    "platebgsampler",
}
ALPHA_RENDER_BUCKETS = {
    "ALPHA",
    "ADAPTIVE_ALPHA",
    "DECAL",
    "CUTOUT",
    "DISPLACEMENT_ALPHA",
}
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
ORIGINAL_SOURCE_PATH_PROP = "s27_ytd_original_source_path"
ORIGINAL_IMAGE_NAME_PROP = "s27_ytd_original_image_name"
INJECTED_DDS_PATH_PROP = "s27_ytd_injected_dds_path"
INJECTED_IMAGE_NAME_PROP = "s27_ytd_injected_image_name"
INJECTED_TEXTURE_NAME_PROP = "s27_ytd_injected_texture_name"
ALPHA_SCAN_SESSION_CACHE_LIMIT = 1024
ALPHA_SCAN_SESSION_CACHE: dict[tuple, float] = {}


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


def _png_dimensions(source_path: str) -> tuple[int, int] | None:
    try:
        with open(source_path, "rb") as handle:
            header = handle.read(24)
        if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return None
        return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    except Exception:
        return None


def _bmp_dimensions(source_path: str) -> tuple[int, int] | None:
    try:
        with open(source_path, "rb") as handle:
            header = handle.read(26)
        if len(header) < 26 or header[:2] != b"BM":
            return None
        width = int.from_bytes(header[18:22], "little", signed=True)
        height = int.from_bytes(header[22:26], "little", signed=True)
        return abs(width), abs(height)
    except Exception:
        return None


def _tga_dimensions(source_path: str) -> tuple[int, int] | None:
    try:
        with open(source_path, "rb") as handle:
            header = handle.read(18)
        if len(header) != 18:
            return None
        width = int.from_bytes(header[12:14], "little")
        height = int.from_bytes(header[14:16], "little")
        return width, height
    except Exception:
        return None


def _dds_dimensions(source_path: str) -> tuple[int, int] | None:
    try:
        with open(source_path, "rb") as handle:
            header = handle.read(20)
        if len(header) < 20 or header[:4] != b"DDS ":
            return None
        height = int.from_bytes(header[12:16], "little")
        width = int.from_bytes(header[16:20], "little")
        return width, height
    except Exception:
        return None


def _jpeg_dimensions(source_path: str) -> tuple[int, int] | None:
    try:
        with open(source_path, "rb") as handle:
            if handle.read(2) != b"\xff\xd8":
                return None
            while True:
                marker_start = handle.read(1)
                if not marker_start:
                    return None
                if marker_start != b"\xff":
                    continue
                marker = handle.read(1)
                while marker == b"\xff":
                    marker = handle.read(1)
                if not marker:
                    return None
                marker_value = marker[0]
                if marker_value in {0xD8, 0xD9}:
                    continue
                if marker_value == 0xDA:
                    return None
                segment_size_raw = handle.read(2)
                if len(segment_size_raw) != 2:
                    return None
                segment_size = int.from_bytes(segment_size_raw, "big")
                if segment_size < 2:
                    return None
                if marker_value in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }:
                    data = handle.read(5)
                    if len(data) != 5:
                        return None
                    height = int.from_bytes(data[1:3], "big")
                    width = int.from_bytes(data[3:5], "big")
                    return width, height
                handle.seek(segment_size - 2, os.SEEK_CUR)
    except Exception:
        return None


def get_source_file_dimensions(source_path: str) -> tuple[int, int] | None:
    source_path = bpy.path.abspath(source_path) if source_path else ""
    if not source_path or not os.path.isfile(source_path):
        return None

    ext = os.path.splitext(source_path)[1].lower()
    readers = {
        ".png": _png_dimensions,
        ".jpg": _jpeg_dimensions,
        ".jpeg": _jpeg_dimensions,
        ".bmp": _bmp_dimensions,
        ".tga": _tga_dimensions,
        ".dds": _dds_dimensions,
    }
    reader = readers.get(ext)
    if reader is None:
        return None

    dimensions = reader(source_path)
    if dimensions is None:
        return None

    width, height = dimensions
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _reload_image_if_source_dimensions_changed(image: bpy.types.Image | None, source_path: str, source_dimensions: tuple[int, int] | None):
    if image is None or not source_path or source_dimensions is None:
        return

    if bool(getattr(image, "is_dirty", False)):
        return

    image_path = resolve_image_source_path(image)
    if not image_path or normalize_path(image_path) != normalize_path(source_path):
        return

    image_width, image_height = image.size
    if (int(image_width), int(image_height)) == source_dimensions:
        return

    try:
        image.reload()
    except Exception:
        pass


def _get_node_string_prop(node: bpy.types.Node, key: str) -> str:
    try:
        return str(node.get(key, "") or "")
    except Exception:
        return ""


def _set_node_string_prop(node: bpy.types.Node, key: str, value: str):
    try:
        if value:
            node[key] = value
        elif key in node:
            del node[key]
    except Exception:
        pass


def _clear_node_injection_source_props(node: bpy.types.Node):
    for key in (
        ORIGINAL_SOURCE_PATH_PROP,
        ORIGINAL_IMAGE_NAME_PROP,
        INJECTED_DDS_PATH_PROP,
        INJECTED_IMAGE_NAME_PROP,
        INJECTED_TEXTURE_NAME_PROP,
    ):
        _set_node_string_prop(node, key, "")


def _find_image_by_source_path(source_path: str) -> bpy.types.Image | None:
    source_path = bpy.path.abspath(source_path) if source_path else ""
    if not source_path:
        return None

    normalized_path = normalize_path(source_path)
    for image in bpy.data.images:
        image_path = resolve_image_source_path(image)
        if image_path and normalize_path(image_path) == normalized_path:
            return image

    if os.path.isfile(source_path):
        try:
            return bpy.data.images.load(source_path, check_existing=True)
        except Exception:
            return None

    return None


def _find_original_node_image(node: bpy.types.Node, source_path: str) -> bpy.types.Image | None:
    image_name = _get_node_string_prop(node, ORIGINAL_IMAGE_NAME_PROP)
    if image_name:
        image = bpy.data.images.get(image_name)
        if image is not None:
            image_path = resolve_image_source_path(image)
            if not source_path or not image_path or normalize_path(image_path) == normalize_path(source_path):
                return image

    return _find_image_by_source_path(source_path)


def _normalize_texture_token(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return os.path.splitext(os.path.basename(value))[0].strip().lower()


def _node_texture_name(node: bpy.types.ShaderNodeTexImage, image: bpy.types.Image | None = None) -> str:
    return getattr(node, "sollumz_texture_name", "") or derive_texture_name(image or node.image)


def _node_texture_name_changed_since_injection(node: bpy.types.ShaderNodeTexImage) -> bool:
    injected_texture_name = _get_node_string_prop(node, INJECTED_TEXTURE_NAME_PROP)
    if not injected_texture_name:
        return False

    current_texture_name = _node_texture_name(node)
    if not current_texture_name:
        return False

    return _normalize_texture_token(current_texture_name) != _normalize_texture_token(injected_texture_name)


def _node_still_uses_injected_dds(node: bpy.types.ShaderNodeTexImage) -> bool:
    image = node.image
    if image is None:
        return False

    injected_path = _get_node_string_prop(node, INJECTED_DDS_PATH_PROP)
    image_path = resolve_image_source_path(image)
    if injected_path and image_path and normalize_path(image_path) == normalize_path(injected_path):
        return True

    injected_image_name = _get_node_string_prop(node, INJECTED_IMAGE_NAME_PROP)
    return bool(injected_image_name and image.name == injected_image_name)


def _resolve_node_export_source(node: bpy.types.ShaderNodeTexImage) -> tuple[bpy.types.Image | None, str]:
    original_source_path = _get_node_string_prop(node, ORIGINAL_SOURCE_PATH_PROP)
    original_source_path = bpy.path.abspath(original_source_path) if original_source_path else ""
    original_image = _find_original_node_image(node, original_source_path)

    if original_source_path or original_image is not None:
        current_image = node.image
        if current_image is not None and (
            _node_texture_name_changed_since_injection(node) or not _node_still_uses_injected_dds(node)
        ):
            _clear_node_injection_source_props(node)
            return current_image, resolve_image_source_path(current_image)
        return original_image, original_source_path or resolve_image_source_path(original_image)

    image = node.image
    return image, resolve_image_source_path(image)


def _remember_node_original_source(node: bpy.types.ShaderNodeTexImage, ref, texture, injected_dds_path: str, injected_image=None):
    _set_node_string_prop(node, INJECTED_DDS_PATH_PROP, bpy.path.abspath(injected_dds_path))
    _set_node_string_prop(node, INJECTED_IMAGE_NAME_PROP, getattr(injected_image, "name", "") if injected_image is not None else "")
    _set_node_string_prop(node, INJECTED_TEXTURE_NAME_PROP, getattr(texture, "texture_name", "") or getattr(ref, "texture_name", ""))

    if _get_node_string_prop(node, ORIGINAL_SOURCE_PATH_PROP) or _get_node_string_prop(node, ORIGINAL_IMAGE_NAME_PROP):
        return

    injected_path = normalize_path(injected_dds_path)
    original_image = getattr(ref, "image", None) or getattr(texture, "image", None) or node.image
    original_path = ""
    path_candidates = (
        getattr(ref, "source_path", ""),
        getattr(texture, "source_path", ""),
        resolve_image_source_path(original_image),
        resolve_image_source_path(node.image),
    )
    for candidate in path_candidates:
        candidate = bpy.path.abspath(candidate) if candidate else ""
        if candidate and normalize_path(candidate) != injected_path:
            original_path = candidate
            break

    if original_image is not None:
        image_path = resolve_image_source_path(original_image)
        if image_path and normalize_path(image_path) == injected_path:
            original_image = None

    _set_node_string_prop(node, ORIGINAL_SOURCE_PATH_PROP, original_path)
    _set_node_string_prop(node, ORIGINAL_IMAGE_NAME_PROP, getattr(original_image, "name", "") if original_image is not None else "")


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


def _sampler_is_diffuse(sampler_name: str) -> bool:
    sampler_name = (sampler_name or "").strip().lower()
    return sampler_name in DIFFUSE_SAMPLER_NAMES


def _material_alpha_hint(material: bpy.types.Material | None, sampler_name: str) -> str:
    if material is None or not _sampler_is_diffuse(sampler_name):
        return ""

    shader_props = getattr(material, "shader_properties", None)
    render_bucket = str(getattr(shader_props, "renderbucket", "") or "").upper()
    if render_bucket in ALPHA_RENDER_BUCKETS or "ALPHA" in render_bucket or "CUTOUT" in render_bucket:
        return f"Sollumz {render_bucket} material"

    shader_filename = str(getattr(shader_props, "filename", "") or "").lower()
    shader_name = str(getattr(shader_props, "name", "") or "").lower()
    shader_text = f"{shader_filename} {shader_name}"
    if any(token in shader_text for token in ("alpha", "cutout")):
        return "Sollumz alpha shader"

    return ""


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

                image, source_path = _resolve_node_export_source(node)
                if (image is None or is_placeholder_image(image)) and not source_path:
                    continue

                texture_name = _node_texture_name(node, image)
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
                        "source_path": source_path,
                        "embedded": embedded,
                        "color_space": (image or node.image).colorspace_settings.name if (image or node.image) is not None else "sRGB",
                        "alpha_material_hint": _material_alpha_hint(mat, node.name),
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


def is_alpha_scanner_enabled(prefs) -> bool:
    return bool(getattr(prefs, "auto_alpha_scanner_enabled", True))


def is_fix_power_of_two_enabled(prefs) -> bool:
    return bool(getattr(prefs, "fix_power_of_two_image", True))


def _alpha_cache_key(image: bpy.types.Image, prefs, stop_after_pct: float | None = None) -> tuple:
    width, height = image.size
    source_path = resolve_image_source_path(image)
    source_mtime = os.path.getmtime(source_path) if source_path and os.path.isfile(source_path) else 0.0
    packed = getattr(image, "packed_file", None)
    return (
        image.as_pointer(),
        image.name,
        int(width),
        int(height),
        int(getattr(image, "channels", 4) or 4),
        bool(getattr(image, "is_dirty", False)),
        source_path,
        source_mtime,
        bool(packed),
        _alpha_cutoff_value(prefs),
        None if stop_after_pct is None else float(stop_after_pct),
    )


def _alpha_session_cache_key(image: bpy.types.Image, prefs, stop_after_pct: float | None = None) -> tuple | None:
    if bool(getattr(image, "is_dirty", False)):
        return None

    source_path = resolve_image_source_path(image)
    if not source_path or not os.path.isfile(source_path):
        return None

    width, height = image.size
    return (
        normalize_path(source_path),
        os.path.getmtime(source_path),
        os.path.getsize(source_path),
        int(width),
        int(height),
        int(getattr(image, "channels", 4) or 4),
        _alpha_cutoff_value(prefs),
        None if stop_after_pct is None else float(stop_after_pct),
    )


def _remember_alpha_session_cache(cache_key: tuple | None, alpha_pct: float):
    if cache_key is None:
        return

    if len(ALPHA_SCAN_SESSION_CACHE) >= ALPHA_SCAN_SESSION_CACHE_LIMIT:
        ALPHA_SCAN_SESSION_CACHE.clear()
    ALPHA_SCAN_SESSION_CACHE[cache_key] = alpha_pct


def compute_alpha_coverage_pct(
    image: bpy.types.Image | None,
    prefs,
    alpha_cache: dict | None = None,
    stop_after_pct: float | None = None,
) -> float:
    if image is None:
        return 0.0

    width, height = image.size
    channels = int(getattr(image, "channels", 4) or 4)
    if width <= 0 or height <= 0 or channels < 4:
        return 0.0

    cache_key = _alpha_cache_key(image, prefs, stop_after_pct) if alpha_cache is not None else None
    if cache_key is not None and cache_key in alpha_cache:
        return alpha_cache[cache_key]

    session_cache_key = _alpha_session_cache_key(image, prefs, stop_after_pct)
    if session_cache_key is not None and session_cache_key in ALPHA_SCAN_SESSION_CACHE:
        alpha_pct = ALPHA_SCAN_SESSION_CACHE[session_cache_key]
        if cache_key is not None:
            alpha_cache[cache_key] = alpha_pct
        return alpha_pct

    pixel_count = int(width) * int(height)
    if pixel_count <= 0:
        return 0.0

    cutoff = _alpha_cutoff_value(prefs)
    stop_after_count = None
    if stop_after_pct is not None:
        stop_after_count = int(pixel_count * max(0.0, min(100.0, float(stop_after_pct))) / 100.0)

    try:
        if np is not None and stop_after_count is None:
            pixels = np.empty(pixel_count * channels, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            alpha = pixels[channels - 1 :: channels]
            count = int(np.count_nonzero(alpha < cutoff))
        else:
            count = 0
            chunk_pixel_count = 262144
            for pixel_start in range(0, pixel_count, chunk_pixel_count):
                pixel_end = min(pixel_count, pixel_start + chunk_pixel_count)
                value_start = pixel_start * channels
                value_end = pixel_end * channels
                pixels = image.pixels[value_start:value_end]

                if np is not None:
                    alpha = np.asarray(pixels, dtype=np.float32)[channels - 1 :: channels]
                    count += int(np.count_nonzero(alpha < cutoff))
                else:
                    for idx in range(channels - 1, len(pixels), channels):
                        if pixels[idx] < cutoff:
                            count += 1

                if stop_after_count is not None and count > stop_after_count:
                    break
    except Exception:
        return 0.0

    alpha_pct = (count / pixel_count) * 100.0
    if cache_key is not None:
        alpha_cache[cache_key] = alpha_pct
    _remember_alpha_session_cache(session_cache_key, alpha_pct)
    return alpha_pct


def _sampler_prefers_data(sampler_hints: str) -> bool:
    text = (sampler_hints or "").lower()
    return any(token in text for token in NORMAL_HINT_TOKENS)


def _texture_source_extension(texture_item) -> str:
    source_path = bpy.path.abspath(texture_item.source_path) if texture_item.source_path else ""
    return os.path.splitext(source_path)[1].lower() if source_path else ""


def _texture_source_path(texture_item) -> str:
    source_path = bpy.path.abspath(texture_item.source_path) if texture_item.source_path else ""
    return os.path.abspath(source_path) if source_path and os.path.isfile(source_path) else ""


def _png_may_have_alpha(source_path: str) -> bool | None:
    try:
        with open(source_path, "rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return None

            ihdr_length = int.from_bytes(handle.read(4), "big")
            if handle.read(4) != b"IHDR" or ihdr_length < 13:
                return None

            ihdr = handle.read(ihdr_length)
            handle.read(4)  # CRC
            color_type = ihdr[9]
            if color_type in {4, 6}:
                return True

            # PNG color types 0, 2 and 3 only carry transparency through tRNS.
            while True:
                raw_length = handle.read(4)
                if len(raw_length) != 4:
                    return False
                chunk_length = int.from_bytes(raw_length, "big")
                chunk_type = handle.read(4)
                if chunk_type == b"tRNS":
                    return True
                if chunk_type == b"IDAT":
                    return False
                handle.seek(chunk_length + 4, os.SEEK_CUR)
    except Exception:
        return None


def _tga_may_have_alpha(source_path: str) -> bool | None:
    try:
        with open(source_path, "rb") as handle:
            header = handle.read(18)
        if len(header) != 18:
            return None

        pixel_depth = header[16]
        alpha_bits = header[17] & 0x0F
        return pixel_depth >= 32 and alpha_bits > 0
    except Exception:
        return None


def _source_file_may_have_alpha(texture_item) -> bool:
    source_ext = _texture_source_extension(texture_item)
    if source_ext and source_ext not in ALPHA_CAPABLE_SOURCE_EXTS:
        return False

    source_path = _texture_source_path(texture_item)
    if not source_path:
        return True

    if source_ext == ".png":
        result = _png_may_have_alpha(source_path)
        if result is not None:
            return result

    if source_ext == ".tga":
        result = _tga_may_have_alpha(source_path)
        if result is not None:
            return result

    return True


def _can_source_have_alpha(texture_item) -> bool:
    return _source_file_may_have_alpha(texture_item)


def compute_texture_alpha_coverage_pct(
    texture_item,
    prefs,
    alpha_cache: dict | None = None,
    stop_after_pct: float | None = None,
) -> float:
    if not is_alpha_scanner_enabled(prefs):
        return 0.0

    if not _can_source_have_alpha(texture_item):
        return 0.0

    image = texture_item.image
    if image is None or getattr(image, "channels", 4) < 4:
        return 0.0

    return compute_alpha_coverage_pct(image, prefs, alpha_cache, stop_after_pct)


def suggest_compression(texture_item, prefs, alpha_pct: float | None = None, alpha_cache: dict | None = None) -> str:
    if getattr(texture_item, "alpha_material_hint", ""):
        return "DXT5"

    if not is_alpha_scanner_enabled(prefs):
        return "DXT1"

    if not _can_source_have_alpha(texture_item):
        return "DXT1"

    image = texture_item.image
    if image is None:
        return "DXT1"

    if getattr(image, "channels", 4) < 4:
        return "DXT1"

    tolerance = getattr(prefs, "alpha_tolerance_pct", 2.0) if prefs else 2.0
    if alpha_pct is None:
        alpha_pct = compute_texture_alpha_coverage_pct(texture_item, prefs, alpha_cache, tolerance)
    return "DXT5" if alpha_pct > tolerance else "DXT1"


def describe_suggestion(texture_item, prefs=None) -> str:
    if getattr(texture_item, "alpha_material_hint", ""):
        return texture_item.alpha_material_hint

    source_ext = _texture_source_extension(texture_item)
    if source_ext and source_ext not in ALPHA_CAPABLE_SOURCE_EXTS:
        return f"Source format {source_ext}"

    image = texture_item.image
    if image is None:
        return "Default"

    if getattr(image, "channels", 4) < 4:
        return "No alpha channel"

    if not is_alpha_scanner_enabled(prefs):
        return "Alpha scanner off"

    if not _source_file_may_have_alpha(texture_item):
        return "No source alpha"

    return "Alpha scan"


def should_review_sampler_alpha(texture_item) -> bool:
    return _sampler_prefers_data(texture_item.sampler_hints) and texture_item.suggested_compression == "DXT1"


def refresh_unique_texture_metadata(texture_item, prefs, alpha_cache: dict | None = None):
    image = texture_item.image
    source_path = _texture_source_path(texture_item)
    source_dimensions = get_source_file_dimensions(source_path)
    _reload_image_if_source_dimensions_changed(image, source_path, source_dimensions)

    if image is not None or source_dimensions is not None:
        if source_dimensions is not None:
            texture_item.width = int(source_dimensions[0])
            texture_item.height = int(source_dimensions[1])
        else:
            texture_item.width = int(image.size[0])
            texture_item.height = int(image.size[1])

        if getattr(texture_item, "alpha_material_hint", ""):
            alpha_pct = 0.0
        elif image is not None:
            tolerance = getattr(prefs, "alpha_tolerance_pct", 2.0) if prefs else 2.0
            alpha_pct = compute_texture_alpha_coverage_pct(texture_item, prefs, alpha_cache, tolerance)
        else:
            alpha_pct = 0.0
        texture_item.alpha_coverage_pct = alpha_pct
        texture_item.suggested_compression = suggest_compression(texture_item, prefs, alpha_pct, alpha_cache)
        texture_item.suggested_reason = describe_suggestion(texture_item, prefs)
    else:
        texture_item.width = 0
        texture_item.height = 0
        texture_item.alpha_coverage_pct = 0.0
        texture_item.suggested_compression = "DXT1"
        texture_item.suggested_reason = "Default"

    texture_item.source_exists = bool(texture_item.source_path and os.path.isfile(bpy.path.abspath(texture_item.source_path)))
    texture_item.metadata_signature = _metadata_signature_text(_texture_metadata_signature(texture_item, prefs))
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


def floor_power_of_two(value: int) -> int:
    value = int(value or 0)
    if value <= 0:
        return 0
    power = 1
    while (power * 2) <= value:
        power *= 2
    return power


def fix_power_of_two_dimensions(width: int, height: int, prefs=None) -> tuple[int, int]:
    width = int(width or 0)
    height = int(height or 0)
    if width <= 0 or height <= 0 or not is_fix_power_of_two_enabled(prefs):
        return max(0, width), max(0, height)
    return max(1, floor_power_of_two(width)), max(1, floor_power_of_two(height))


def get_texture_output_dimensions(texture_item, prefs=None) -> tuple[int, int]:
    width, height = get_texture_dimensions(texture_item)
    resized_width, resized_height = calculate_resize_dimensions(width, height, get_texture_resize_limit(texture_item))
    return fix_power_of_two_dimensions(resized_width, resized_height, prefs)


def describe_resize_setting(texture_item, prefs=None) -> str:
    width, height = get_texture_dimensions(texture_item)
    if width <= 0 or height <= 0:
        return "Resize: dimensions unavailable"

    output_width, output_height = get_texture_output_dimensions(texture_item, prefs)
    if output_width == width and output_height == height:
        return f"Resize: keeps {width}x{height}"
    if is_fix_power_of_two_enabled(prefs):
        return f"Resize: {width}x{height} -> {output_width}x{output_height} (Power2 fix)"
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


def _capture_texture_settings(pack, prefs) -> dict[str, dict]:
    settings = {}
    for item in pack.textures:
        texture_name = getattr(item, "texture_name", "")
        if not texture_name:
            continue
        settings[texture_name] = {
            "compression": getattr(item, "compression", "AUTO"),
            "resize_max_dimension": getattr(item, "resize_max_dimension", "ORIGINAL"),
            "expanded": bool(getattr(item, "expanded", False)),
            "metadata_signature": getattr(item, "metadata_signature", ""),
            "width": int(getattr(item, "width", 0) or 0),
            "height": int(getattr(item, "height", 0) or 0),
            "alpha_coverage_pct": float(getattr(item, "alpha_coverage_pct", 0.0) or 0.0),
            "source_exists": bool(getattr(item, "source_exists", False)),
        }
    return settings


def _alpha_metadata_prefs_signature(prefs) -> tuple:
    tolerance = getattr(prefs, "alpha_tolerance_pct", 2.0) if prefs else 2.0
    return (
        is_alpha_scanner_enabled(prefs),
        _alpha_cutoff_value(prefs),
        float(tolerance),
    )


def _texture_metadata_signature(texture_item, prefs) -> tuple | None:
    image = getattr(texture_item, "image", None)
    if image is not None and bool(getattr(image, "is_dirty", False)):
        return None

    source_path = bpy.path.abspath(texture_item.source_path) if getattr(texture_item, "source_path", "") else ""
    if not source_path or not os.path.isfile(source_path):
        return None

    source_dimensions = get_source_file_dimensions(source_path)
    image_width, image_height = source_dimensions or (image.size if image is not None else (0, 0))
    return (
        normalize_path(source_path),
        os.path.getmtime(source_path),
        os.path.getsize(source_path),
        int(image_width or 0),
        int(image_height or 0),
        int(getattr(image, "channels", 4) or 4) if image is not None else 0,
        _alpha_metadata_prefs_signature(prefs),
    )


def _metadata_signature_text(signature: tuple | None) -> str:
    return repr(signature) if signature is not None else ""


def _refresh_suggestion_from_cached_alpha(texture_item, prefs):
    texture_item.suggested_compression = suggest_compression(
        texture_item,
        prefs,
        float(getattr(texture_item, "alpha_coverage_pct", 0.0) or 0.0),
        None,
    )
    texture_item.suggested_reason = describe_suggestion(texture_item, prefs)


def _reuse_texture_metadata_if_current(texture_item, existing_settings: dict | None, prefs) -> bool:
    if not existing_settings:
        return False

    existing_signature = existing_settings.get("metadata_signature", "")
    current_signature = _metadata_signature_text(_texture_metadata_signature(texture_item, prefs))
    if not existing_signature or not current_signature or existing_signature != current_signature:
        return False

    texture_item.width = int(existing_settings.get("width", 0) or 0)
    texture_item.height = int(existing_settings.get("height", 0) or 0)
    texture_item.alpha_coverage_pct = float(existing_settings.get("alpha_coverage_pct", 0.0) or 0.0)
    texture_item.source_exists = bool(existing_settings.get("source_exists", True))
    texture_item.metadata_signature = current_signature
    _refresh_suggestion_from_cached_alpha(texture_item, prefs)
    texture_item.resize_max_dimension = clamp_resize_choice(
        getattr(texture_item, "resize_max_dimension", "ORIGINAL"),
        texture_item.width,
        texture_item.height,
    )
    return True


def _set_progress(context, current: int, total: int):
    wm = getattr(context, "window_manager", None)
    if wm is None or total <= 0:
        return
    try:
        wm.progress_update(min(max(current, 0), total))
    except Exception:
        pass


def rebuild_pack_from_assets(context, pack):
    prefs = get_preferences(context)
    existing_texture_settings = _capture_texture_settings(pack, prefs)
    alpha_cache: dict = {}
    metadata_current_texture_names: set[str] = set()
    pack.textures.clear()
    total_assets = max(1, len(pack.assets))
    wm = getattr(context, "window_manager", None)

    try:
        if wm is not None:
            wm.progress_begin(0, total_assets)

        for asset_index, asset in enumerate(pack.assets, start=1):
            asset.textures.clear()
            root_obj = asset.asset
            if root_obj is None:
                _set_progress(context, asset_index, total_assets)
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
                    unique.alpha_material_hint = ref["alpha_material_hint"]
                    unique.ref_count = 1
                    existing_settings = existing_texture_settings.get(ref["texture_name"])
                    if existing_settings:
                        unique.compression = existing_settings["compression"]
                        unique.resize_max_dimension = existing_settings["resize_max_dimension"]
                        unique.expanded = existing_settings["expanded"]
                    if not _reuse_texture_metadata_if_current(unique, existing_settings, prefs):
                        refresh_unique_texture_metadata(unique, prefs, alpha_cache)
                        if existing_settings:
                            unique.resize_max_dimension = clamp_resize_choice(
                                existing_settings["resize_max_dimension"],
                                unique.width,
                                unique.height,
                            )
                    metadata_current_texture_names.add(unique.texture_name)
                else:
                    previous_alpha_hint = unique.alpha_material_hint
                    previous_source_path = unique.source_path
                    previous_image_pointer = unique.image.as_pointer() if unique.image is not None else 0
                    unique.ref_count += 1
                    unique.embedded = unique.embedded or ref["embedded"]
                    unique.embedded_ref_count += 1 if ref["embedded"] else 0
                    unique.external_ref_count += 0 if ref["embedded"] else 1
                    unique.sampler_hints = _append_csv_value(unique.sampler_hints, ref["sampler_name"])
                    unique.alpha_material_hint = _append_csv_value(unique.alpha_material_hint, ref["alpha_material_hint"])
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

                    current_image_pointer = unique.image.as_pointer() if unique.image is not None else 0
                    source_changed = (
                        unique.texture_name not in metadata_current_texture_names
                        or normalize_path(previous_source_path) != normalize_path(unique.source_path)
                        or (not unique.source_path and previous_image_pointer != current_image_pointer)
                    )
                    if source_changed:
                        refresh_unique_texture_metadata(unique, prefs, alpha_cache)
                        metadata_current_texture_names.add(unique.texture_name)
                    elif previous_alpha_hint != unique.alpha_material_hint:
                        _refresh_suggestion_from_cached_alpha(unique, prefs)

                ref_item.pack_texture_name = unique.texture_name

            _set_progress(context, asset_index, total_assets)
    finally:
        if wm is not None:
            try:
                wm.progress_end()
            except Exception:
                pass

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


def is_block_compression_compatible(width: int, height: int) -> bool:
    width = int(width or 0)
    height = int(height or 0)
    return width > 0 and height > 0 and width % 4 == 0 and height % 4 == 0


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


def get_compression_validation_warning(texture_item, compression: str | None = None, prefs=None) -> str:
    resolved_compression = compression or resolve_compression(texture_item)
    if resolved_compression not in {"DXT1", "DXT5"}:
        return ""

    output_width, output_height = get_texture_output_dimensions(texture_item, prefs)
    if is_block_compression_compatible(output_width, output_height):
        return ""

    return (
        f"{resolved_compression} requires width and height multiples of 4. "
        f"Current output is {output_width}x{output_height}."
    )


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


def load_image_for_injection(dds_path: str, is_data: bool) -> bpy.types.Image:
    normalized_path = normalize_path(dds_path)
    image = None

    for candidate in bpy.data.images:
        candidate_path = bpy.path.abspath(candidate.filepath) if candidate.filepath else ""
        if candidate_path and normalize_path(candidate_path) == normalized_path:
            image = candidate
            break

    if image is None:
        image = bpy.data.images.load(dds_path, check_existing=False)
    else:
        image.filepath = dds_path
        try:
            image.reload()
        except Exception:
            # If Blender refuses to reload the existing datablock, keep the source cache intact
            # and assign a fresh image datablock for this injection only.
            image = bpy.data.images.load(dds_path, check_existing=False)

    if is_data:
        image.colorspace_settings.is_data = True

    return image


def export_pack(context, pack) -> tuple[str, int, int]:
    rebuild_pack_from_assets(context, pack)

    prefs = get_preferences(context)
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
    total_textures = max(1, len(pack.textures))
    wm = getattr(context, "window_manager", None)

    try:
        if wm is not None:
            wm.progress_begin(0, total_textures)

        with tempfile.TemporaryDirectory(prefix="s27_ytd_") as tmpdir:
            for texture_index, texture in enumerate(pack.textures, start=1):
                wants_ytd = texture.external_ref_count > 0
                wants_embedded = texture.embedded_ref_count > 0

                if not wants_ytd and not wants_embedded:
                    _set_progress(context, texture_index, total_textures)
                    continue

                input_path, skip_reason = prepare_source_file(texture, tmpdir)
                if not input_path:
                    texture.exported_path = ""
                    texture.embedded_exported_path = ""
                    skipped_missing_count += 1
                    _set_progress(context, texture_index, total_textures)
                    continue

                resolved_compression = resolve_compression(texture)
                _, ytd_format = compression_to_texconv_flag(resolved_compression)
                final_name = f"{sanitize_name(texture.texture_name)}.dds"
                source_width, source_height = get_texture_dimensions(texture)
                output_width, output_height = get_texture_output_dimensions(texture, prefs)
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

                _set_progress(context, texture_index, total_textures)
    finally:
        if wm is not None:
            try:
                wm.progress_end()
            except Exception:
                pass

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
    total_refs = max(1, sum(len(asset.textures) for asset in pack.assets))
    processed_refs = 0
    wm = getattr(context, "window_manager", None)

    try:
        if wm is not None:
            wm.progress_begin(0, total_refs)

        for asset in pack.assets:
            for ref in asset.textures:
                processed_refs += 1
                _set_progress(context, processed_refs, total_refs)

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

                image = load_image_for_injection(dds_path, _sampler_prefers_data(ref.sampler_name))

                _remember_node_original_source(node, ref, texture, dds_path, image)
                node.image = image
                if ref.embedded:
                    texture.embedded_exported_path = dds_path
                else:
                    texture.exported_path = dds_path

                injected += 1
    finally:
        if wm is not None:
            try:
                wm.progress_end()
            except Exception:
                pass

    pack.status = f"Injected {injected} DDS assignment(s)"
    return injected
