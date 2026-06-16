# SPDX-License-Identifier: MIT
#
# Nitro Engine DSMA exporter — exports a rigged Blender mesh directly to the
# DSM/DSA format used by Nitro Engine, plus the textures (PNG) and .grit files.

import os

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper

from . import atlas
from . import blender_extract
from . import dsma_export
from . import texture_export
from .blender_extract import ExportError

# Kept for compatibility with legacy add-on loaders; on Blender 4.2+ the
# blender_manifest.toml is authoritative.
bl_info = {
    "name": "Nitro Engine DSMA exporter",
    "author": "Nitro Engine community",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "File > Export > Nitro Engine model (.dsm/.dsa)",
    "description": "Export a rigged mesh to Nitro Engine DSM/DSA, PNG and grit",
    "category": "Import-Export",
}

_SIZE_ITEMS = [(str(s), str(s), "") for s in texture_export.VALID_TEXTURE_SIZES]

# DS vertices are 4.12 signed fixed point, so coordinates must stay in
# [-8.0, 8.0). Anything at or above this overflows the VTX_16 command.
V16_MAX = 0x7FFF / (1 << 12)


def _max_abs_coord(meshes):
    m = 0.0
    for mesh in meshes:
        for w in mesh.weights:
            m = max(m, abs(w.pos.x), abs(w.pos.y), abs(w.pos.z))
    return m


def _anim_name(action_name):
    """robot 'Walk Cycle.001' -> 'walk_cycle_001', matching md5_to_dsma."""
    return action_name.replace(".", "_").replace(" ", "_").lower()


class ExportNitroDSMA(Operator, ExportHelper):
    """Export the selected rigged mesh to Nitro Engine DSM/DSA format"""
    bl_idname = "export_scene.nitro_dsma"
    bl_label = "Export Nitro Engine model"
    bl_options = {'PRESET'}

    filename_ext = ".dsm"
    filter_glob: StringProperty(default="*.dsm", options={'HIDDEN'})

    pack_atlas: BoolProperty(
        name="Pack textures into one atlas",
        description="A DS model has a single texture. When enabled, all textures "
                    "are packed into one power-of-two atlas and the UVs are "
                    "remapped automatically. The atlas size is chosen "
                    "automatically and overrides the manual texture size",
        default=True)

    texture_width: EnumProperty(
        name="Texture width", items=_SIZE_ITEMS, default="256")
    texture_height: EnumProperty(
        name="Texture height", items=_SIZE_ITEMS, default="256")

    use_bin: BoolProperty(
        name="Add '.bin' suffix",
        description="Name files <model>_dsm.bin / <model>_<anim>_dsa.bin for "
                    "BlocksDS embedding (matches the Nitro Engine examples)",
        default=True)

    export_base_pose: BoolProperty(
        name="Export base pose",
        description="Also export the rest pose as <model>.dsa so the model can "
                    "be displayed without any animation",
        default=True)

    export_animations: BoolProperty(
        name="Export animations",
        description="Export every action as a separate DSA file",
        default=True)

    skip_frames: IntProperty(
        name="Skip frames",
        description="Frames to skip per exported frame (0 = all, 1 = half, ...)",
        default=0, min=0, soft_max=4)

    model_scale: FloatProperty(
        name="Model scale",
        description="Uniform scale applied to mesh and skeleton. DS vertex "
                    "coordinates must stay within +/-8.0, so large models must "
                    "be scaled down here and scaled back up at runtime with "
                    "NE_ModelScale",
        default=1.0, min=0.0, soft_min=0.001, soft_max=1.0)

    blender_fix: BoolProperty(
        name="Blender fix (-90° X)",
        description="Rotate the model to convert Blender's Z-up to the DS' Y-up",
        default=True)

    reverse_winding: BoolProperty(
        name="Reverse winding",
        description="Reverse triangle winding. Blender's native triangle order "
                    "already matches the DS front-face convention, so leave this "
                    "OFF. Enable only if faces appear inside-out on hardware",
        default=False)

    force_single_bone: BoolProperty(
        name="Force single bone per vertex",
        description="The DS only supports one bone per vertex (rigid skinning). "
                    "When enabled, the bone with the highest weight is picked "
                    "automatically for vertices skinned to several bones. When "
                    "disabled, such vertices abort the export instead",
        default=True)

    use_subfolders: BoolProperty(
        name="data/ and graphics/ layout",
        description="Write DSM/DSA into a 'data' folder and textures into a "
                    "'graphics' folder, like the Nitro Engine examples",
        default=True)

    def execute(self, context):
        try:
            return self._run(context)
        except ExportError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

    def _collect_objects(self, context):
        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        if not meshes and context.active_object and \
                context.active_object.type == 'MESH':
            meshes = [context.active_object]

        if not meshes:
            raise ExportError("Select at least one mesh object to export.")

        armature = meshes[0].find_armature()
        if armature is None:
            raise ExportError(
                f"Mesh '{meshes[0].name}' has no Armature modifier. A skeleton "
                "is required for the DSM/DSA format.")

        for m in meshes:
            if m.find_armature() is not armature:
                raise ExportError(
                    "All selected meshes must be rigged to the same armature.")

        return meshes, armature

    def _build_atlas(self, meshes, name):
        """
        Build a texture atlas across all meshes. Each material contributes either
        its image texture or, if it only has a base color, a small solid-color
        tile. Returns (atlas_image, (w, h), per_mesh_transforms) or None if there
        is nothing to pack. per_mesh_transforms[i] maps a material index to a UV
        transform.
        """
        images = []                 # unique source images to pack
        seen_img = set()            # names of real images already added
        color_images = {}           # rounded RGBA -> synthetic solid image
        solid_names = set()         # names of synthetic (solid-color) images
        slot_source = {}            # (mesh_index, slot_index) -> source image

        for mi, mesh_obj in enumerate(meshes):
            for si, slot in enumerate(mesh_obj.material_slots):
                mat = slot.material
                img = texture_export.material_image(mat)
                if img is not None:
                    if img.name not in seen_img:
                        seen_img.add(img.name)
                        images.append(img)
                    slot_source[(mi, si)] = img
                    continue

                color = texture_export.material_base_color(mat)
                if color is None:
                    continue  # empty material slot

                ckey = tuple(round(c, 4) for c in color)
                src = color_images.get(ckey)
                if src is None:
                    src = texture_export.make_solid_image(
                        color, f"{name}_color_{len(color_images)}")
                    color_images[ckey] = src
                    solid_names.add(src.name)
                    images.append(src)
                slot_source[(mi, si)] = src

        if not images:
            return None

        synthetic = list(color_images.values())
        try:
            result = atlas.build_atlas(images, name + "_atlas", self.report)
        except BaseException:
            for s in synthetic:
                bpy.data.images.remove(s)
            raise

        if result is None:
            for s in synthetic:
                bpy.data.images.remove(s)
            return None

        atlas_img, aw, ah, transforms = result

        def to_transform(src):
            t = transforms.get(src.name)
            if t is None:
                return None
            if src.name in solid_names:
                # Collapse to the tile center so arbitrary/missing UVs on
                # flat-colored faces cannot bleed into neighbouring tiles.
                off_u, off_v, scale_u, scale_v = t
                return (off_u + 0.5 * scale_u, off_v + 0.5 * scale_v, 0.0, 0.0)
            return t

        per_mesh = []
        for mi, mesh_obj in enumerate(meshes):
            mapping = {}
            for si in range(len(mesh_obj.material_slots)):
                src = slot_source.get((mi, si))
                if src is not None:
                    t = to_transform(src)
                    if t is not None:
                        mapping[si] = t
            per_mesh.append(mapping)

        for s in synthetic:
            bpy.data.images.remove(s)

        return atlas_img, (aw, ah), per_mesh

    def _run(self, context):
        meshes, armature = self._collect_objects(context)

        name = texture_export._sanitize(os.path.basename(self.filepath))
        root = os.path.dirname(self.filepath)

        if self.use_subfolders:
            data_dir = os.path.join(root, "data")
            graphics_dir = os.path.join(root, "graphics")
        else:
            data_dir = root
            graphics_dir = root
        os.makedirs(data_dir, exist_ok=True)

        ext_mesh = "_dsm.bin" if self.use_bin else ".dsm"
        ext_anim = "_dsa.bin" if self.use_bin else ".dsa"

        # Decide the texture layout first: with atlasing the atlas size becomes
        # the texture size and we get per-material UV transforms.
        atlas_img = None
        per_mesh_transforms = [None] * len(meshes)
        texture_size = (int(self.texture_width), int(self.texture_height))

        if self.pack_atlas:
            atlas_result = self._build_atlas(meshes, name)
            if atlas_result is not None:
                atlas_img, texture_size, per_mesh_transforms = atlas_result

        # Build the skeleton and meshes.
        ordered_names, name_to_index = blender_extract.build_bone_order(armature)
        rest_joints = blender_extract.extract_rest_joints(
            armature, ordered_names, self.model_scale)

        out_meshes = []
        for mesh_obj, transforms in zip(meshes, per_mesh_transforms):
            vgroup_to_bone = blender_extract.build_vgroup_to_bone(
                mesh_obj, name_to_index)
            out_meshes.append(blender_extract.extract_mesh(
                mesh_obj, armature, rest_joints, vgroup_to_bone,
                reverse_winding=self.reverse_winding, scale=self.model_scale,
                uv_transforms=transforms, force_single=self.force_single_bone))

        # Fail early with a helpful message instead of a raw OverflowError if
        # the geometry does not fit in the DS' +/-8.0 vertex range.
        max_coord = _max_abs_coord(out_meshes)
        if max_coord >= V16_MAX:
            suggested = round(V16_MAX / max_coord * self.model_scale, 4)
            raise ExportError(
                f"Vertex coordinate {max_coord:.3f} exceeds the DS limit of "
                f"{V16_MAX:.3f}. Lower 'Model scale' (try about {suggested}) and "
                "scale the model back up at runtime with NE_ModelScale.")

        written = []

        # Mesh display list
        dsm_path = os.path.join(data_dir, name + ext_mesh)
        dsma_export.generate_dsm(rest_joints, out_meshes, dsm_path, texture_size)
        written.append(dsm_path)

        # Base pose
        if self.export_base_pose:
            base_path = os.path.join(data_dir, name + ext_anim)
            dsma_export.save_animation([rest_joints], base_path, self.blender_fix)
            written.append(base_path)

        # Animations
        if self.export_animations:
            for action in blender_extract.collect_armature_actions(armature):
                frames = blender_extract.extract_frames(
                    context, armature, action, ordered_names, self.skip_frames,
                    scale=self.model_scale)
                anim_path = os.path.join(
                    data_dir, f"{name}_{_anim_name(action.name)}{ext_anim}")
                dsma_export.save_animation(frames, anim_path, self.blender_fix)
                written.append(anim_path)

        # Textures
        if atlas_img is not None:
            try:
                written.extend(texture_export.save_atlas(
                    atlas_img, name, graphics_dir, self.report))
            finally:
                bpy.data.images.remove(atlas_img)
        else:
            for mesh_obj in meshes:
                written.extend(texture_export.export_textures(
                    mesh_obj, graphics_dir, self.report))

        self.report({'INFO'},
                    f"Exported {len(written)} file(s) for model '{name}'.")
        print("Nitro Engine DSMA export wrote:")
        for path in written:
            print("  " + path)

        return {'FINISHED'}


def menu_func_export(self, context):
    self.layout.operator(ExportNitroDSMA.bl_idname,
                         text="Nitro Engine model (.dsm/.dsa)")


def register():
    bpy.utils.register_class(ExportNitroDSMA)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(ExportNitroDSMA)


if __name__ == "__main__":
    register()
