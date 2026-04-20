import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
    EnumProperty,
)
from bpy.types import AddonPreferences, PropertyGroup


ADDON_ID = "s27_ytd_manager"

COMPRESSION_ITEMS = (
    ("AUTO", "Auto", "Choose compression automatically"),
    ("DXT1", "DXT1", "BC1 / DXT1"),
    ("DXT5", "DXT5", "BC3 / DXT5"),
    ("ARGB8", "ARGB8", "R8G8B8A8_UNORM / uncompressed"),
)

RESIZE_ITEM_DEFS = (
    ("ORIGINAL", "Original", "Keep the original size"),
    ("2048", "2048", "Limit the largest side to 2048 pixels"),
    ("1024", "1024", "Limit the largest side to 1024 pixels"),
    ("512", "512", "Limit the largest side to 512 pixels"),
    ("256", "256", "Limit the largest side to 256 pixels"),
    ("128", "128", "Limit the largest side to 128 pixels"),
    ("64", "64", "Limit the largest side to 64 pixels"),
    ("32", "32", "Limit the largest side to 32 pixels"),
    ("16", "16", "Limit the largest side to 16 pixels"),
    ("4", "4", "Limit the largest side to 4 pixels"),
)
RESIZE_ITEM_MAP = {item[0]: item for item in RESIZE_ITEM_DEFS}
TEXTURE_RESIZE_ORDER = ("2048", "1024", "512", "256", "128", "64", "32", "16", "4")
RESIZE_ALL_ITEMS = (
    RESIZE_ITEM_MAP["2048"],
    RESIZE_ITEM_MAP["1024"],
    RESIZE_ITEM_MAP["512"],
    RESIZE_ITEM_MAP["256"],
    RESIZE_ITEM_MAP["128"],
    RESIZE_ITEM_MAP["64"],
    RESIZE_ITEM_MAP["32"],
    RESIZE_ITEM_MAP["16"],
    RESIZE_ITEM_MAP["4"],
)


def _max_dimension(width: int, height: int) -> int:
    return max(int(width or 0), int(height or 0))


def get_resize_values_for_dimensions(width: int, height: int) -> tuple[str, ...]:
    max_dimension = _max_dimension(width, height)
    values = ["ORIGINAL"]
    for value in TEXTURE_RESIZE_ORDER:
        if max_dimension > int(value):
            values.append(value)
    return tuple(values)


def texture_resize_items(self, _context):
    width = int(getattr(self, "width", 0) or 0)
    height = int(getattr(self, "height", 0) or 0)
    image = getattr(self, "image", None)
    if (width <= 0 or height <= 0) and image is not None:
        image_width, image_height = image.size
        width = width or int(image_width)
        height = height or int(image_height)
    return [RESIZE_ITEM_MAP[value] for value in get_resize_values_for_dimensions(width, height)]


def clamp_resize_choice(choice: str, width: int, height: int) -> str:
    allowed_values = get_resize_values_for_dimensions(width, height)
    return choice if choice in allowed_values else "ORIGINAL"


def bulk_resize_choice_for_dimensions(width: int, height: int, target: str) -> str:
    max_dimension = _max_dimension(width, height)
    if target not in RESIZE_ITEM_MAP or target == "ORIGINAL":
        return "ORIGINAL"
    return target if max_dimension > int(target) else "ORIGINAL"


class S27YTDManagerPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    texconv_path: StringProperty(
        name="Texconv Path",
        subtype="FILE_PATH",
        description="Path to texconv.exe used for DDS conversion",
    )
    default_output_dir: StringProperty(
        name="Default Build Folder",
        subtype="DIR_PATH",
        default="//S27_YTD_Build",
        description="Base folder used when a scene does not override the build directory",
    )
    fix_power_of_two_image: BoolProperty(
        name="Fix Power Of 2 Image",
        description="Automatically shrink exported output to power-of-two dimensions for each axis, never enlarging the image",
        default=True,
    )
    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "texconv_path")
        layout.prop(self, "default_output_dir")
        layout.prop(self, "fix_power_of_two_image")


class S27YTDTextureRef(PropertyGroup):
    material: PointerProperty(type=bpy.types.Material)
    image: PointerProperty(type=bpy.types.Image)
    sampler_name: StringProperty(name="Sampler")
    node_name: StringProperty(name="Node Name")
    texture_name: StringProperty(name="Texture Name")
    source_path: StringProperty(name="Source Path")
    embedded: BoolProperty(name="Embedded", default=False)
    pack_texture_name: StringProperty(name="Pack Texture Name")
    color_space: StringProperty(name="Color Space")


class S27YTDAsset(PropertyGroup):
    asset: PointerProperty(type=bpy.types.Object)
    asset_name: StringProperty(name="Asset Name")
    asset_type: StringProperty(name="Asset Type")
    expanded: BoolProperty(name="Expanded", default=True)
    textures: CollectionProperty(type=S27YTDTextureRef)


class S27YTDUniqueTexture(PropertyGroup):
    texture_name: StringProperty(name="Texture Name")
    expanded: BoolProperty(name="Expanded", default=False)
    image: PointerProperty(type=bpy.types.Image)
    source_path: StringProperty(name="Source Path")
    source_exists: BoolProperty(name="Source Exists", default=False)
    embedded: BoolProperty(name="Embedded", default=False)
    compression: EnumProperty(name="Compression", items=COMPRESSION_ITEMS, default="AUTO")
    resize_max_dimension: EnumProperty(name="Resize", items=texture_resize_items)
    suggested_compression: StringProperty(name="Suggested Compression", default="DXT1")
    suggested_reason: StringProperty(name="Suggested Reason")
    metadata_signature: StringProperty(name="Metadata Signature")
    sampler_hints: StringProperty(name="Sampler Hints")
    alpha_material_hint: StringProperty(name="Alpha Material Hint")
    warning: StringProperty(name="Warning")
    has_conflict: BoolProperty(name="Name Conflict", default=False)
    embedded_ref_count: IntProperty(name="Embedded Ref Count", default=0)
    external_ref_count: IntProperty(name="External Ref Count", default=0)
    width: IntProperty(name="Width", default=0)
    height: IntProperty(name="Height", default=0)
    ref_count: IntProperty(name="Reference Count", default=0)
    exported_path: StringProperty(name="Exported Path")
    embedded_exported_path: StringProperty(name="Embedded Exported Path")


class S27YTDPack(PropertyGroup):
    name: StringProperty(name="YTD Name", default="new_ytd")
    expanded: BoolProperty(name="Expanded", default=True)
    active_asset_index: IntProperty(name="Active Asset Index", default=0)
    output_subdir: StringProperty(
        name="Output Subfolder",
        description="Optional custom folder name inside the build directory. Leave blank to use the YTD name",
    )
    status: StringProperty(name="Status")
    last_export_dir: StringProperty(name="Last Export Folder")
    resize_all_target: EnumProperty(name="Resize All", items=RESIZE_ALL_ITEMS, default="512")
    assets: CollectionProperty(type=S27YTDAsset)
    textures: CollectionProperty(type=S27YTDUniqueTexture)


CLASSES = (
    S27YTDManagerPreferences,
    S27YTDTextureRef,
    S27YTDAsset,
    S27YTDUniqueTexture,
    S27YTDPack,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Scene.s27_ytd_packs = CollectionProperty(type=S27YTDPack)
    bpy.types.Scene.s27_ytd_active_pack_index = IntProperty(
        name="Active YTD Index",
        default=0,
    )
    bpy.types.Scene.s27_ytd_build_dir = StringProperty(
        name="Build Folder",
        subtype="DIR_PATH",
        default="",
        description="Base output folder for generated YTD XML and DDS files",
    )


def unregister():
    del bpy.types.Scene.s27_ytd_build_dir
    del bpy.types.Scene.s27_ytd_active_pack_index
    del bpy.types.Scene.s27_ytd_packs

    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
