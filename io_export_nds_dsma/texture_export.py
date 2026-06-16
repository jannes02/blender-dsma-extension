# SPDX-License-Identifier: MIT
#
# Exports the textures used by a mesh as PNG files plus a matching .grit file,
# following the layout used by Nitro Engine's examples (graphics/<name>.png +
# graphics/<name>.grit).

import os
import re

import bpy

# Fixed grit configuration: 16-bit texture bitmap, force the alpha bit to 1.
# This matches examples/loading/animated_model/graphics/texture.grit.
GRIT_CONTENT = "# 16 bit texture bitmap, force alpha bit to 1\n-gx -gb -gB16 -gT!\n"

VALID_TEXTURE_SIZES = [8, 16, 32, 64, 128, 256, 512, 1024]


def is_valid_texture_size(size):
    return size in VALID_TEXTURE_SIZES


def _sanitize(name):
    name = os.path.splitext(name)[0]
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if name and name[0].isdigit():
        name = "_" + name
    return name.lower() or "texture"


def material_image(material):
    """First image texture used by a material, or None."""
    if material is None or not material.use_nodes:
        return None
    for node in material.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image is not None:
            return node.image
    return None


def material_base_color(material):
    """
    Flat RGBA color of a material that has no image texture: the Principled
    BSDF base color if available, otherwise the viewport diffuse color. Returns
    None only for an empty material slot.
    """
    if material is None:
        return None

    if material.use_nodes:
        for node in material.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bc = node.inputs.get("Base Color")
                if bc is not None and not bc.is_linked:
                    v = bc.default_value
                    return (v[0], v[1], v[2], v[3])

    c = material.diffuse_color
    return (c[0], c[1], c[2], c[3])


def make_solid_image(rgba, name, size=8):
    """Create a small solid-color image datablock (caller must remove it)."""
    img = bpy.data.images.new(name, width=size, height=size, alpha=True)
    img.pixels.foreach_set(list(rgba) * (size * size))
    img.update()
    return img


def collect_images(mesh_obj):
    """Return the unique images referenced by the mesh object's materials."""
    images = []
    seen = set()
    for slot in mesh_obj.material_slots:
        mat = slot.material
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image is not None:
                img = node.image
                if img.name not in seen:
                    seen.add(img.name)
                    images.append(img)
    return images


def _save_png(img, png_path, report):
    """
    Write `img` to a PNG without disturbing the user's datablock. Returns True
    on success, False (with a warning) if the image has no usable pixel data.

    The pixels are copied into a fresh image instead of using img.copy(): a copy
    of an unpacked, file-less image (e.g. an edited in-memory texture) loses its
    pixel data and fails to save, even though the original renders fine.
    """
    if not img.has_data:
        try:
            img.reload()
        except RuntimeError:
            pass

    width, height = img.size
    if not img.has_data or width == 0 or height == 0:
        if report is not None:
            report({'WARNING'},
                   f"Texture '{img.name}' has no image data (missing source "
                   "file?) and was skipped.")
        return False

    tmp = bpy.data.images.new(name="__dsma_tmp", width=width, height=height,
                              alpha=True)
    try:
        buf = [0.0] * (len(img.pixels))
        img.pixels.foreach_get(buf)
        tmp.pixels.foreach_set(buf)

        tmp.file_format = 'PNG'
        tmp.filepath_raw = png_path
        tmp.save()
    finally:
        bpy.data.images.remove(tmp)
    return True


def write_grit(grit_path):
    with open(grit_path, "w") as f:
        f.write(GRIT_CONTENT)


def save_atlas(atlas_img, base, graphics_dir, report=None):
    """Save an already-built atlas image as PNG + grit. Returns written paths."""
    os.makedirs(graphics_dir, exist_ok=True)
    png_path = os.path.join(graphics_dir, base + ".png")
    grit_path = os.path.join(graphics_dir, base + ".grit")

    if not _save_png(atlas_img, png_path, report):
        return []

    write_grit(grit_path)
    return [png_path, grit_path]


def export_textures(mesh_obj, graphics_dir, report=None):
    """
    Save every texture of the mesh as a PNG and write a matching .grit file.
    Returns a list of written file paths.
    """
    os.makedirs(graphics_dir, exist_ok=True)

    written = []
    images = collect_images(mesh_obj)

    if not images and report is not None:
        report({'WARNING'}, "No image texture found on the mesh's materials.")

    if len(images) > 1 and report is not None:
        report({'WARNING'},
               f"{len(images)} textures found on '{mesh_obj.name}'. A DS model "
               "uses a single texture space; bake them into one atlas or split "
               "the mesh into separate models.")

    for img in images:
        base = _sanitize(img.name)
        png_path = os.path.join(graphics_dir, base + ".png")
        grit_path = os.path.join(graphics_dir, base + ".grit")

        if not _save_png(img, png_path, report):
            continue

        width, height = img.size
        if not (is_valid_texture_size(width) and is_valid_texture_size(height)):
            if report is not None:
                report({'WARNING'},
                       f"Texture '{img.name}' is {width}x{height}; the DS needs "
                       f"power-of-two sizes ({VALID_TEXTURE_SIZES}).")

        with open(grit_path, "w") as f:
            f.write(GRIT_CONTENT)

        written.append(png_path)
        written.append(grit_path)

    return written
