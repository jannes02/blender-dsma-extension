# SPDX-License-Identifier: MIT
#
# Extracts the data needed by the DSM/DSA writers directly from Blender, so no
# intermediate .md5mesh / .md5anim files are required. The output uses the same
# namedtuples md5_to_dsma builds after parsing those files.

import re

import bpy

from .md5_math import Joint, Vert, Weight, Mesh, Vector, Quaternion, \
    world_to_joint_space

# Matches the bone name in an fcurve data path like pose.bones["Spine"].location
_BONE_PATH = re.compile(r'pose\.bones\["([^"]+)"\]')

MAX_BONES = 30

# A vertex group whose weight is below this is treated as "not influencing".
WEIGHT_EPSILON = 0.01


class ExportError(Exception):
    """Raised when the scene does not satisfy the DS skeletal-mesh constraints."""
    pass


def _bv(v):
    """Blender Vector/Sequence -> md5_math.Vector"""
    return Vector(v[0], v[1], v[2])


def _bq(q):
    """Blender Quaternion -> md5_math.Quaternion"""
    return Quaternion(q.w, q.x, q.y, q.z)


def build_bone_order(armature_obj):
    """
    Returns (ordered_names, name_to_index) with parents always appearing before
    their children, which is required so parent indices are valid.
    """
    armature = armature_obj.data

    ordered = []

    def visit(bone):
        ordered.append(bone)
        for child in bone.children:
            visit(child)

    for bone in armature.bones:
        if bone.parent is None:
            visit(bone)

    if len(ordered) > MAX_BONES:
        raise ExportError(
            f"The armature has {len(ordered)} bones, but the DS matrix stack "
            f"only allows up to {MAX_BONES}.")

    name_to_index = {bone.name: i for i, bone in enumerate(ordered)}
    return [bone.name for bone in ordered], name_to_index


def _scaled(v, scale):
    return Vector(v.x * scale, v.y * scale, v.z * scale)


def extract_rest_joints(armature_obj, ordered_names, scale=1.0):
    """
    Absolute rest-pose transform of every bone, expressed in world space.

    Working in world space (instead of armature-object space) makes the export
    match the size seen in the viewport, even when the armature or mesh object
    has an unapplied object scale. Translations are then multiplied by `scale`;
    rotations are scale-invariant.
    """
    bones = armature_obj.data.bones
    arm_world = armature_obj.matrix_world

    joints = []
    for name in ordered_names:
        bone = bones[name]
        m = arm_world @ bone.matrix_local  # rest matrix in world space
        pos = _scaled(_bv(m.to_translation()), scale)
        orient = _bq(m.to_quaternion().normalized())
        parent = -1
        joints.append(Joint(name, parent, pos, orient))

    return joints


def _vertex_bone_index(vertex, vgroup_to_bone, vertex_index, force_single=True):
    """
    Resolve the single bone influencing a vertex. The DS skeletal format only
    supports one bone per vertex (rigid skinning).

    With force_single=True the bone with the highest weight wins when a vertex
    is influenced by several bones. With force_single=False this is treated as
    an error instead, so the user can keep skinning fully under control.
    """
    influences = []
    for g in vertex.groups:
        bone_index = vgroup_to_bone.get(g.group)
        if bone_index is None:
            continue  # vertex group not bound to a bone
        if g.weight > WEIGHT_EPSILON:
            influences.append((bone_index, g.weight))

    if len(influences) == 0:
        raise ExportError(
            f"Vertex {vertex_index} is not weighted to any bone. Every vertex "
            "must be assigned to at least one bone.")

    if len(influences) > 1 and not force_single:
        raise ExportError(
            f"Vertex {vertex_index} is influenced by {len(influences)} bones. "
            "The DS skeletal format only supports one bone per vertex (rigid "
            "skinning). Enable 'Force single bone per vertex' to auto-pick the "
            "dominant bone, or assign each vertex to a single bone.")

    # Highest-weight bone wins.
    influences.sort(key=lambda bw: bw[1], reverse=True)
    return influences[0][0]


IDENTITY_UV = (0.0, 0.0, 1.0, 1.0)


def extract_mesh(mesh_obj, armature_obj, rest_joints, vgroup_to_bone,
                 reverse_winding=True, scale=1.0, uv_transforms=None,
                 force_single=True):
    """
    Build a single Mesh (vertices + tris + weights) from a Blender mesh object.

    Vertices are split per unique (vertex, UV) pair so UV seams are preserved.
    `uv_transforms`, if given, maps a material index to an (off_u, off_v,
    scale_u, scale_v) tuple used to remap that material's UVs into a texture
    atlas. Returns an md5_math.Mesh.
    """
    mesh = mesh_obj.data

    if mesh.uv_layers.active is None:
        raise ExportError(
            f"Mesh '{mesh_obj.name}' has no UV map. A UV map is required to "
            "generate texture coordinates.")

    mesh.calc_loop_triangles()
    uv_data = mesh.uv_layers.active.data

    # The joints live in world space, so bring the vertices to world space too.
    to_world = mesh_obj.matrix_world

    # Resolve the bone of every original vertex once.
    vert_bone = [None] * len(mesh.vertices)
    vert_weight_pos = [None] * len(mesh.vertices)
    for i, v in enumerate(mesh.vertices):
        bone_index = _vertex_bone_index(v, vgroup_to_bone, i, force_single)
        vert_bone[i] = bone_index

        joint = rest_joints[bone_index]
        # rest_joints translations are already scaled, so scale the vertex too.
        co_world = _scaled(_bv(to_world @ v.co), scale)
        vert_weight_pos[i] = world_to_joint_space(co_world, joint.pos, joint.orient)

    verts = []
    weights = []
    tris = []
    unique = {}

    def get_index(orig_vidx, loop_idx, transform):
        uv = uv_data[loop_idx].uv
        off_u, off_v, scale_u, scale_v = transform
        # Remap into the atlas sub-rectangle (identity if not atlasing).
        au = off_u + uv[0] * scale_u
        av = off_v + uv[1] * scale_v

        u = round(au, 6)
        # Blender's UV origin is bottom-left; the DS/MD5 origin is top-left.
        v = round(1.0 - av, 6)
        key = (orig_vidx, u, v)

        idx = unique.get(key)
        if idx is not None:
            return idx

        idx = len(verts)
        verts.append(Vert((u, v), idx, 1))
        weights.append(Weight(vert_bone[orig_vidx], 1.0,
                              vert_weight_pos[orig_vidx]))
        unique[key] = idx
        return idx

    for lt in mesh.loop_triangles:
        if uv_transforms is None:
            transform = IDENTITY_UV
        else:
            transform = uv_transforms.get(lt.material_index, IDENTITY_UV)

        i0 = get_index(lt.vertices[0], lt.loops[0], transform)
        i1 = get_index(lt.vertices[1], lt.loops[1], transform)
        i2 = get_index(lt.vertices[2], lt.loops[2], transform)

        if reverse_winding:
            tris.append((i2, i1, i0))
        else:
            tris.append((i0, i1, i2))

    return Mesh(len(verts), verts, len(tris), tris, len(weights), weights)


def extract_frames(context, armature_obj, action, ordered_names, skip_frames,
                   scale=1.0):
    """
    Sample an action and return a list of frames, each frame being a list of
    absolute Joint transforms (in armature space), ready for save_animation().
    """
    scene = context.scene

    fr_start = int(round(action.frame_range[0]))
    fr_end = int(round(action.frame_range[1]))
    frame_numbers = list(range(fr_start, fr_end + 1))
    frame_numbers = frame_numbers[::skip_frames + 1]

    if armature_obj.animation_data is None:
        armature_obj.animation_data_create()

    prev_action = armature_obj.animation_data.action
    prev_frame = scene.frame_current

    armature_obj.animation_data.action = action

    # Previous frame's orientation per bone, used to keep the quaternions on a
    # consistent hemisphere. The DSA interpolation in dsma.c is a plain
    # component-wise nlerp, so a sign flip between frames (q vs -q, same
    # rotation) makes it interpolate the long way and deforms the model.
    prev_q = [None] * len(ordered_names)

    frames = []
    try:
        for f in frame_numbers:
            scene.frame_set(f)

            # Re-read each frame in case the armature object itself is animated.
            arm_world = armature_obj.matrix_world

            joints = []
            for i, name in enumerate(ordered_names):
                pbone = armature_obj.pose.bones[name]
                m = arm_world @ pbone.matrix  # pose matrix in world space
                pos = _scaled(_bv(m.to_translation()), scale)

                bq = m.to_quaternion().normalized()
                q = (bq.w, bq.x, bq.y, bq.z)
                pq = prev_q[i]
                if pq is not None:
                    dot = q[0] * pq[0] + q[1] * pq[1] + q[2] * pq[2] + q[3] * pq[3]
                    if dot < 0.0:
                        q = (-q[0], -q[1], -q[2], -q[3])
                prev_q[i] = q

                orient = Quaternion(q[0], q[1], q[2], q[3])
                joints.append(Joint(name, -1, pos, orient))
            frames.append(joints)
    finally:
        armature_obj.animation_data.action = prev_action
        scene.frame_set(prev_frame)

    return frames


def collect_armature_actions(armature_obj):
    """
    Actions that actually animate this armature's bones, so unrelated actions
    in the file (other objects, materials, ...) are not exported.
    """
    bone_names = set(armature_obj.data.bones.keys())

    actions = []
    for action in bpy.data.actions:
        for fc in action.fcurves:
            m = _BONE_PATH.search(fc.data_path)
            if m is not None and m.group(1) in bone_names:
                actions.append(action)
                break

    # Always include the action currently assigned to the armature.
    ad = armature_obj.animation_data
    if ad is not None and ad.action is not None and ad.action not in actions:
        actions.append(ad.action)

    return actions


def build_vgroup_to_bone(mesh_obj, name_to_index):
    """Map vertex-group indices of a mesh object to bone indices."""
    mapping = {}
    for vg in mesh_obj.vertex_groups:
        bone_index = name_to_index.get(vg.name)
        if bone_index is not None:
            mapping[vg.index] = bone_index
    return mapping
