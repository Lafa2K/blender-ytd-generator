import bpy
from . import model, operators, ui

bl_info = {
    "name": "S27 YTD Manager",
    "author": "Lafa2K + Codex",
    "version": (1, 0, 13),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > S27 Tab",
    "description": "Manages YTD files for CodeWalker",
    "category": "Import-Export",
}

def register():
    model.register()
    operators.register()
    ui.register()

def unregister():
    ui.unregister()
    operators.unregister()
    model.unregister()
