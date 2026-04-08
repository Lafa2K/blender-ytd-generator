import os
import bpy
from . import operators, utils


def _compression_icon(texture) -> str:
    resolved = utils.resolve_compression(texture)
    if texture.has_conflict:
        return "ERROR"
    if resolved == "ARGB8":
        return "EVENT_A"
    if resolved == "DXT5":
        return "IMAGE_ALPHA"
    return "CHECKMARK"


def _active_pack(scene):
    pack_index = getattr(scene, "s27_ytd_active_pack_index", -1)
    if 0 <= pack_index < len(scene.s27_ytd_packs):
        return scene.s27_ytd_packs[pack_index], pack_index
    return None, -1


class S27YTD_UL_packs(bpy.types.UIList):
    bl_idname = "S27YTD_UL_packs"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.prop(item, "name", text="", emboss=False, icon="TEXTURE_DATA")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon="TEXTURE_DATA")


class S27YTD_UL_assets(bpy.types.UIList):
    bl_idname = "S27YTD_UL_assets"

    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        label = item.asset_name or "<missing asset>"
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(text=label, icon="MESH_CUBE")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon="MESH_CUBE")


class S27YTD_PT_MainPanel(bpy.types.Panel):
    bl_label = "S27 YTD Manager"
    bl_idname = "S27YTD_PT_MainPanel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "S27 YTD"

    def draw(self, context):
        try:
            self._draw_content(context)
        except Exception as exc:
            layout = self.layout
            error_box = layout.box()
            error_box.label(text="Panel failed to render.", icon="ERROR")
            error_box.label(text=str(exc))

    def _draw_content(self, context):
        layout = self.layout
        scene = context.scene
        scene_build_dir = getattr(scene, "s27_ytd_build_dir", "") or ""

        header = layout.box()
        header.label(text="Build multiple YTD packs from Sollumz assets")
        header.prop(scene, "s27_ytd_build_dir")
        if not scene_build_dir:
            header.label(text="Using the default build folder from preferences.", icon="INFO")
            header.label(text=utils.get_build_root(context), icon="FILE_FOLDER")

        texconv_path = utils.get_texconv_path(context)
        texconv_row = header.row()
        texconv_row.label(
            text=os.path.basename(texconv_path) if texconv_path else "Texconv.exe not configured",
            icon="CHECKMARK" if texconv_path else "ERROR",
        )

        if not utils.is_sollumz_available():
            warning = layout.box()
            warning.label(text="Enable Sollumz before using this addon.", icon="ERROR")
            return

        actions = layout.row(align=True)
        actions.operator(operators.S27YTD_OT_add_pack.bl_idname, icon="ADD", text="Add YTD")
        actions.operator(operators.S27YTD_OT_export_all_packs.bl_idname, icon="EXPORT", text="Export All")
        actions.operator(operators.S27YTD_OT_inject_all_packs.bl_idname, icon="TEXTURE", text="Inject All DDS")

        pack, pack_index = _active_pack(scene)
        if not scene.s27_ytd_packs:
            empty = layout.box()
            empty.label(text="No YTD packs yet.")
            empty.label(text="Create one, then use Add Selected on Drawable or Fragment assets.")
            return

        textures_box = layout.box()

        split = textures_box.split(factor=0.58)
        left = split.column(align=True)
        right = split.column(align=True)

        left_header = left.row(align=True)
        left_header.label(text="Texture Packages", icon="TEXTURE_DATA")
        if pack_index >= 0:
            remove_pack = left_header.operator(operators.S27YTD_OT_remove_pack.bl_idname, text="", icon="X")
            remove_pack.pack_index = pack_index
        left.template_list(
            S27YTD_UL_packs.bl_idname,
            "",
            scene,
            "s27_ytd_packs",
            scene,
            "s27_ytd_active_pack_index",
            rows=5,
        )

        right_header = right.row(align=True)
        right_header.label(text="Meshes", icon="MESH_CUBE")
        if pack and 0 <= pack.active_asset_index < len(pack.assets):
            remove_asset = right_header.operator(operators.S27YTD_OT_remove_asset.bl_idname, text="", icon="X")
            remove_asset.pack_index = pack_index
            remove_asset.asset_index = pack.active_asset_index

        if pack:
            right.template_list(
                S27YTD_UL_assets.bl_idname,
                "",
                pack,
                "assets",
                pack,
                "active_asset_index",
                rows=5,
            )
        else:
            right.label(text="Select a YTD package first.")

        if pack:
            buttons = textures_box.row(align=True)
            add_selected = buttons.operator(operators.S27YTD_OT_add_selected_assets.bl_idname, icon="ADD", text="Add Selected")
            add_selected.pack_index = pack_index
            refresh = buttons.operator(operators.S27YTD_OT_refresh_pack.bl_idname, icon="FILE_REFRESH", text="Refresh")
            refresh.pack_index = pack_index
            export = buttons.operator(operators.S27YTD_OT_export_pack.bl_idname, icon="EXPORT", text="Export")
            export.pack_index = pack_index
            inject = buttons.operator(operators.S27YTD_OT_inject_pack.bl_idname, icon="TEXTURE", text="Inject DDS")
            inject.pack_index = pack_index

            if pack.status:
                status_row = textures_box.row()
                status_row.label(text=pack.status, icon="INFO")

        unique_box = layout.box()
        unique_box.label(text="UNIQUE TEXTURES", icon="TEXTURE")

        if not pack:
            unique_box.label(text="Select a YTD package to inspect textures.")
            return

        resize_all_box = unique_box.box()
        resize_all_row = resize_all_box.row(align=True)
        resize_all_row.label(text="Resize All")
        resize_all_row.prop(pack, "resize_all_target", text="")
        apply_resize = resize_all_row.operator(operators.S27YTD_OT_apply_resize_all.bl_idname, text="Apply Resize")
        apply_resize.pack_index = pack_index
        resize_all_box.label(text="Makes images smaller and never larger.", icon="INFO")
        resize_all_box.label(text="Keeps aspect ratio based on the largest side.")

        if not pack.textures:
            unique_box.label(text="No textures found yet. Add assets or refresh the pack.")
            return

        for texture in pack.textures:
            self._draw_texture(unique_box, texture)

    def _draw_texture(self, layout, texture):
        texture_box = layout.box()

        top = texture_box.row(align=True)
        top.prop(
            texture,
            "expanded",
            text="",
            icon="TRIA_DOWN" if texture.expanded else "TRIA_RIGHT",
            emboss=False,
        )
        top.label(text=texture.texture_name, icon=_compression_icon(texture))
        top.label(text=f"({texture.compression})")

        if not texture.expanded:
            return

        body = texture_box.column(align=True)

        source_name = os.path.basename(texture.source_path) if texture.source_path else "<packed or generated>"
        body.label(text=f"Source: {source_name}", icon="FILE_IMAGE")
        if texture.suggested_reason == "Alpha scan":
            body.label(
                text=(
                    f"Suggested: {texture.suggested_compression} ({texture.suggested_reason}) | "
                    f"{texture.width}x{texture.height} | alpha {texture.alpha_coverage_pct:.2f}%"
                )
            )
        else:
            body.label(
                text=f"Suggested: {texture.suggested_compression} ({texture.suggested_reason}) | {texture.width}x{texture.height}"
            )
        if texture.sampler_hints:
            body.label(text=f"Samplers: {texture.sampler_hints}")
        if utils.should_review_sampler_alpha(texture):
            body.label(text="Suggested DXT1. Review manually if this map stores gloss/mask alpha.", icon="INFO")
        if texture.embedded_ref_count > 0 and texture.external_ref_count > 0:
            body.label(text="Embedded: Mixed (embedded + external)", icon="PACKAGE")
        else:
            body.label(text=f"Embedded: {'Yes' if texture.embedded else 'No'}", icon="PACKAGE" if texture.embedded else "BLANK1")

        if texture.external_ref_count > 0:
            body.label(text="YTD export: automatic for external textures.", icon="CHECKMARK")
        else:
            body.label(text="Embedded only: ignored in YTD XML export", icon="INFO")

        settings_row = body.row(align=True)
        settings_row.prop(texture, "compression", text="Compression")
        settings_row.prop(texture, "resize_max_dimension", text="Resize")
        body.label(text=utils.describe_resize_setting(texture), icon="FULLSCREEN_ENTER")

        if texture.has_conflict or texture.warning:
            body.label(text=texture.warning or "Duplicate logical texture name detected.", icon="ERROR")


CLASSES = (
    S27YTD_UL_packs,
    S27YTD_UL_assets,
    S27YTD_PT_MainPanel,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
