import bpy
from bpy.props import IntProperty

from . import utils


def _get_pack(scene, pack_index: int):
    if 0 <= pack_index < len(scene.s27_ytd_packs):
        return scene.s27_ytd_packs[pack_index]
    return None


class S27YTD_OT_add_pack(bpy.types.Operator):
    bl_idname = "s27_ytd.add_pack"
    bl_label = "Add YTD"
    bl_description = "Create a new YTD pack"

    def execute(self, context):
        pack = context.scene.s27_ytd_packs.add()
        pack.name = f"ytd_{len(context.scene.s27_ytd_packs):02d}"
        pack.status = "Empty pack"
        context.scene.s27_ytd_active_pack_index = len(context.scene.s27_ytd_packs) - 1
        return {"FINISHED"}


class S27YTD_OT_remove_pack(bpy.types.Operator):
    bl_idname = "s27_ytd.remove_pack"
    bl_label = "Remove YTD"
    bl_description = "Remove this YTD pack"

    pack_index: IntProperty()

    def execute(self, context):
        if 0 <= self.pack_index < len(context.scene.s27_ytd_packs):
            context.scene.s27_ytd_packs.remove(self.pack_index)
            context.scene.s27_ytd_active_pack_index = min(
                max(0, context.scene.s27_ytd_active_pack_index),
                max(0, len(context.scene.s27_ytd_packs) - 1),
            )
        return {"FINISHED"}


class S27YTD_OT_add_selected_assets(bpy.types.Operator):
    bl_idname = "s27_ytd.add_selected_assets"
    bl_label = "Add Selected"
    bl_description = "Add selected Sollumz assets to this YTD pack"

    pack_index: IntProperty()

    def execute(self, context):
        if not utils.is_sollumz_available():
            self.report({"ERROR"}, "Sollumz must be enabled before using S27 YTD Manager.")
            return {"CANCELLED"}

        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        added = utils.add_selected_assets_to_pack(context, pack)
        if added == 0:
            self.report({"WARNING"}, "No new valid Drawable or Fragment roots were found in the current selection.")
        else:
            self.report({"INFO"}, f"Added {added} asset(s) to '{pack.name}'.")
        return {"FINISHED"}


class S27YTD_OT_refresh_pack(bpy.types.Operator):
    bl_idname = "s27_ytd.refresh_pack"
    bl_label = "Refresh"
    bl_description = "Rescan assets and rebuild the texture list"

    pack_index: IntProperty()

    def execute(self, context):
        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        utils.rebuild_pack_from_assets(context, pack)
        self.report({"INFO"}, f"Refreshed '{pack.name}'.")
        return {"FINISHED"}


class S27YTD_OT_remove_asset(bpy.types.Operator):
    bl_idname = "s27_ytd.remove_asset"
    bl_label = "Remove Asset"
    bl_description = "Remove this asset from the YTD pack"

    pack_index: IntProperty()
    asset_index: IntProperty()

    def execute(self, context):
        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        utils.remove_asset_from_pack(context, pack, self.asset_index)
        return {"FINISHED"}


class S27YTD_OT_apply_resize_all(bpy.types.Operator):
    bl_idname = "s27_ytd.apply_resize_all"
    bl_label = "Apply Resize"
    bl_description = "Apply the selected resize limit to every unique texture in this YTD pack"

    pack_index: IntProperty()

    def execute(self, context):
        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        if not pack.textures:
            self.report({"WARNING"}, "This YTD pack has no textures to resize yet.")
            return {"CANCELLED"}

        changed, resized = utils.apply_resize_all_to_pack(pack)
        self.report(
            {"INFO"},
            f"Applied resize to '{pack.name}': {resized} texture(s) will shrink, {changed} entry change(s).",
        )
        return {"FINISHED"}


class S27YTD_OT_export_pack(bpy.types.Operator):
    bl_idname = "s27_ytd.export_pack"
    bl_label = "Export YTD"
    bl_description = "Convert textures to DDS and generate the OpenFormats YTD XML"

    pack_index: IntProperty()

    def execute(self, context):
        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        try:
            xml_path, count, skipped = utils.export_pack(context, pack)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if skipped > 0:
            self.report({"WARNING"}, f"Exported {count} texture(s) to {xml_path}. Skipped {skipped} missing/no-data texture(s).")
        else:
            self.report({"INFO"}, f"Exported {count} texture(s) to {xml_path}")
        return {"FINISHED"}


class S27YTD_OT_export_all_packs(bpy.types.Operator):
    bl_idname = "s27_ytd.export_all_packs"
    bl_label = "Export All"
    bl_description = "Export every YTD pack in the scene"

    def execute(self, context):
        exported = 0
        skipped_total = 0
        for pack in context.scene.s27_ytd_packs:
            try:
                _, _, skipped = utils.export_pack(context, pack)
                exported += 1
                skipped_total += skipped
            except Exception as exc:
                self.report({"ERROR"}, f"{pack.name}: {exc}")
                return {"CANCELLED"}

        if skipped_total > 0:
            self.report({"WARNING"}, f"Exported {exported} YTD pack(s). Skipped {skipped_total} missing/no-data texture(s).")
        else:
            self.report({"INFO"}, f"Exported {exported} YTD pack(s).")
        return {"FINISHED"}


class S27YTD_OT_inject_pack(bpy.types.Operator):
    bl_idname = "s27_ytd.inject_pack"
    bl_label = "Inject DDS"
    bl_description = "Load exported DDS files and assign them back into the Sollumz texture nodes"

    pack_index: IntProperty()

    def execute(self, context):
        pack = _get_pack(context.scene, self.pack_index)
        if pack is None:
            return {"CANCELLED"}

        try:
            injected = utils.inject_pack(context, pack)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, f"Injected {injected} DDS assignment(s) into '{pack.name}'.")
        return {"FINISHED"}


class S27YTD_OT_inject_all_packs(bpy.types.Operator):
    bl_idname = "s27_ytd.inject_all_packs"
    bl_label = "Inject All DDS"
    bl_description = "Inject DDS files for every YTD pack in the scene"

    def execute(self, context):
        injected_total = 0
        processed = 0

        for pack in context.scene.s27_ytd_packs:
            try:
                injected_total += utils.inject_pack(context, pack)
                processed += 1
            except Exception as exc:
                self.report({"ERROR"}, f"{pack.name}: {exc}")
                return {"CANCELLED"}

        self.report({"INFO"}, f"Injected {injected_total} DDS assignment(s) across {processed} YTD pack(s).")
        return {"FINISHED"}


CLASSES = (
    S27YTD_OT_add_pack,
    S27YTD_OT_remove_pack,
    S27YTD_OT_add_selected_assets,
    S27YTD_OT_refresh_pack,
    S27YTD_OT_remove_asset,
    S27YTD_OT_apply_resize_all,
    S27YTD_OT_export_pack,
    S27YTD_OT_export_all_packs,
    S27YTD_OT_inject_pack,
    S27YTD_OT_inject_all_packs,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
