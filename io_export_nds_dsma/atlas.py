# SPDX-License-Identifier: MIT
#
# Packs several textures into a single power-of-two atlas, because a DS model
# only supports one texture. Returns the atlas image plus, per source image, the
# UV transform (offset/scale, in Blender bottom-left convention) needed to remap
# that texture's UVs into the atlas.

import bpy

from .texture_export import VALID_TEXTURE_SIZES

# UV transform applied as: u' = off_u + u * scale_u (and likewise for v).
IDENTITY_UV = (0.0, 0.0, 1.0, 1.0)


def _ensure_data(img):
    if not img.has_data:
        try:
            img.reload()
        except RuntimeError:
            pass
    w, h = img.size
    return img.has_data and w > 0 and h > 0


def _rgba_pixels(img):
    """Return (width, height, [r,g,b,a,...]) for an image, padding to RGBA."""
    w, h = img.size
    ch = img.channels
    raw = [0.0] * (w * h * ch)
    img.pixels.foreach_get(raw)

    if ch == 4:
        return w, h, raw

    rgba = [0.0] * (w * h * 4)
    n = w * h
    if ch == 3:
        for i in range(n):
            rgba[i * 4 + 0] = raw[i * 3 + 0]
            rgba[i * 4 + 1] = raw[i * 3 + 1]
            rgba[i * 4 + 2] = raw[i * 3 + 2]
            rgba[i * 4 + 3] = 1.0
    elif ch == 1:
        for i in range(n):
            g = raw[i]
            rgba[i * 4 + 0] = g
            rgba[i * 4 + 1] = g
            rgba[i * 4 + 2] = g
            rgba[i * 4 + 3] = 1.0
    else:  # ch == 2 (grayscale + alpha) or anything unusual
        for i in range(n):
            g = raw[i * ch]
            a = raw[i * ch + 1] if ch >= 2 else 1.0
            rgba[i * 4 + 0] = g
            rgba[i * 4 + 1] = g
            rgba[i * 4 + 2] = g
            rgba[i * 4 + 3] = a
    return w, h, rgba


def _scaled_rgba(img, target_w, target_h):
    """Pixels of img resampled to (target_w, target_h) as an RGBA float list."""
    w, h, rgba = _rgba_pixels(img)

    if (w, h) == (target_w, target_h):
        return rgba

    # Use Blender's own resampler via a temporary image.
    tmp = bpy.data.images.new("__dsma_scale", width=w, height=h, alpha=True)
    try:
        tmp.pixels.foreach_set(rgba)
        tmp.scale(target_w, target_h)
        out = [0.0] * (target_w * target_h * 4)
        tmp.pixels.foreach_get(out)
    finally:
        bpy.data.images.remove(tmp)
    return out


def _shelf_pack(sizes, aw, ah):
    """
    Place rectangles using a simple shelf algorithm. Returns a list of (x, y)
    bottom-left positions (matching Blender's pixel origin) or None if they do
    not fit into aw x ah.
    """
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])

    placements = [None] * len(sizes)
    x = 0
    y = 0
    shelf_h = 0
    for i in order:
        w, h = sizes[i]
        if w > aw or h > ah:
            return None
        if x + w > aw:
            y += shelf_h
            x = 0
            shelf_h = 0
        if y + h > ah:
            return None
        placements[i] = (x, y)
        x += w
        shelf_h = max(shelf_h, h)
    return placements


def _fit_atlas(sizes, max_size):
    """Find the smallest power-of-two atlas that fits all rectangles."""
    pow2 = [s for s in VALID_TEXTURE_SIZES if s <= max_size]
    candidates = sorted(
        [(w, h) for w in pow2 for h in pow2],
        key=lambda d: (d[0] * d[1], max(d)))
    for (aw, ah) in candidates:
        placements = _shelf_pack(sizes, aw, ah)
        if placements is not None:
            return aw, ah, placements
    return None


def build_atlas(images, name, report=None, max_size=1024):
    """
    Build a single atlas image from `images`.

    Returns (atlas_image, atlas_w, atlas_h, transforms) where transforms maps
    each source image name to its (off_u, off_v, scale_u, scale_v). The caller
    owns atlas_image and must remove it after saving. Returns None if no usable
    image is found.
    """
    usable = [img for img in images if _ensure_data(img)]
    skipped = [img for img in images if img not in usable]
    if report is not None:
        for img in skipped:
            report({'WARNING'},
                   f"Texture '{img.name}' has no image data and was left out of "
                   "the atlas.")
    if not usable:
        return None

    # Shrink everything uniformly until it fits into max_size x max_size.
    scale = 1.0
    for _ in range(12):
        sizes = []
        for img in usable:
            w, h = img.size
            sizes.append((max(1, int(round(w * scale))),
                          max(1, int(round(h * scale)))))
        fit = _fit_atlas(sizes, max_size)
        if fit is not None:
            break
        scale *= 0.75
    else:
        raise RuntimeError(
            "Could not fit the textures into a "
            f"{max_size}x{max_size} atlas even after scaling them down.")

    atlas_w, atlas_h, placements = fit
    if scale < 1.0 and report is not None:
        report({'WARNING'},
               f"Textures were scaled to {scale:.2f}x to fit the "
               f"{atlas_w}x{atlas_h} atlas.")

    atlas = bpy.data.images.new(name, width=atlas_w, height=atlas_h, alpha=True)
    buf = [0.0] * (atlas_w * atlas_h * 4)

    transforms = {}
    for img, (px, py), (sw, sh) in zip(usable, placements, sizes):
        pixels = _scaled_rgba(img, sw, sh)
        for r in range(sh):
            dst = ((py + r) * atlas_w + px) * 4
            src = (r * sw) * 4
            buf[dst:dst + sw * 4] = pixels[src:src + sw * 4]

        transforms[img.name] = (px / atlas_w, py / atlas_h,
                                sw / atlas_w, sh / atlas_h)

    atlas.pixels.foreach_set(buf)
    atlas.update()

    return atlas, atlas_w, atlas_h, transforms
