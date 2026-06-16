# SPDX-License-Identifier: MIT
#
# DSM (mesh display list) and DSA (animation) writers. The logic is ported
# directly from Nitro Engine's md5_to_dsma.py so that the binary output is
# compatible with the dsma-library / Nitro Engine runtime.

from .display_list import DisplayList, float_to_f32
from .md5_math import Quaternion, Vector, joint_info_to_m4x3


def generate_dsm(joints, meshes, output_file, texture_size,
                 draw_normal_polygons=False):
    """
    Build a DSM display list from the intermediate joints/meshes representation
    (see md5_math for the namedtuples) and write it to output_file.

    This mirrors convert_md5mesh() in md5_to_dsma.py.
    """
    dl = DisplayList()
    dl.switch_vtxs("triangles")

    base_matrix = 30 - len(joints) + 1
    last_joint_index = None

    for mesh in meshes:
        # Per-triangle normals, computed from the reconstructed world positions.
        tri_normal = []
        for tri in mesh.tris:
            verts = [mesh.verts[i] for i in tri]
            weights = [mesh.weights[v.startWeight] for v in verts]

            vtx = []
            for vert, weight in zip(verts, weights):
                joint = joints[weight.joint]
                m = joint_info_to_m4x3(joint.orient, joint.pos)
                final = weight.pos.mul_m4x3(m)
                vtx.append(final)

            a = vtx[0].sub(vtx[1])
            b = vtx[1].sub(vtx[2])

            n = a.cross(b)

            if n.length() > 0:
                n = n.normalize()
                tri_normal.append(n)
            else:
                tri_normal.append(Vector(0, 0, 0))

        for tri, norm in zip(mesh.tris, tri_normal):
            verts = [mesh.verts[i] for i in tri]
            weights = [mesh.weights[v.startWeight] for v in verts]

            finals = []

            for vert, weight in zip(verts, weights):

                # Texture
                # -------

                st = vert.st
                # In the MD5 format (0, 0) is the top-left corner, same as what
                # the GPU of the DS expects.
                u = st[0] * texture_size[0]
                v = st[1] * texture_size[1]
                dl.texcoord(u, v)

                # Vertex and normal
                # -----------------

                # Load joint matrix. When drawing normal polygons it has to be
                # loaded every time, because drawing the normal restores the
                # original matrix.

                joint_index = weight.joint
                if draw_normal_polygons or joint_index != last_joint_index:
                    dl.mtx_restore(base_matrix + joint_index)
                    last_joint_index = joint_index

                # Calculate normal in joint space

                joint = joints[joint_index]

                q = joint.orient
                qt = q.complement()
                n = norm.to_q()

                # Transform by the inverted quaternion
                n = qt.mul(n).mul(q).to_v3()
                if n.length() > 0:
                    n = n.normalize()
                dl.normal(n.x, n.y, n.z)

                # The vertex is already in joint space

                dl.vtx(weight.pos.x, weight.pos.y, weight.pos.z)

                if draw_normal_polygons:
                    q = joint.orient
                    qt = q.complement()
                    v = weight.pos.to_q()

                    delta = q.mul(v).mul(qt).to_v3()

                    final = joint.pos.add(delta)
                    finals.append(final)

            if draw_normal_polygons:
                dl.mtx_restore(1)

                vert_avg = Vector(
                    (finals[0].x + finals[1].x + finals[2].x) / 3,
                    (finals[0].y + finals[1].y + finals[2].y) / 3,
                    (finals[0].z + finals[1].z + finals[2].z) / 3
                )

                vert_avg_end = vert_avg.add(norm)

                dl.texcoord(0, 0)

                dl.color(1, 0, 0)
                dl.vtx(vert_avg.x + 0.1, vert_avg.y, vert_avg.z)
                dl.vtx(vert_avg.x, vert_avg.y, vert_avg.z)
                dl.color(0, 1, 0)
                dl.vtx(vert_avg_end.x, vert_avg_end.y, vert_avg_end.z)

                dl.color(1, 0, 0)
                dl.vtx(vert_avg.x, vert_avg.y, vert_avg.z)
                dl.vtx(vert_avg.x, vert_avg.y + 0.1, vert_avg.z)
                dl.color(0, 1, 0)
                dl.vtx(vert_avg_end.x, vert_avg_end.y, vert_avg_end.z)

                dl.color(1, 0, 0)
                dl.vtx(vert_avg.x, vert_avg.y, vert_avg.z)
                dl.vtx(vert_avg.x, vert_avg.y, vert_avg.z + 0.1)
                dl.color(0, 1, 0)
                dl.vtx(vert_avg_end.x, vert_avg_end.y, vert_avg_end.z)

    dl.end_vtxs()
    dl.finalize()

    dl.save_to_file(output_file)


def save_animation(frames, output_file, blender_fix=True):
    """
    Write a list of frames (each frame is a list of absolute Joint transforms)
    to a DSA file. Mirrors save_animation() in md5_to_dsma.py.
    """
    version = 1
    num_frames = len(frames)
    num_bones = len(frames[0])

    u32_array = [version, num_frames, num_bones]

    for joints in frames:
        if num_bones != len(joints):
            raise ValueError("Different number of bones across frames")

        for joint in joints:
            this_pos = joint.pos
            this_orient = joint.orient

            if blender_fix:
                # All bones store absolute transformations, so rotate every bone
                # -90 degrees on the X axis to convert from Blender's Z-up to the
                # DS' Y-up coordinate system.
                q_rot = Quaternion(0.7071068, -0.7071068, 0, 0)
                this_orient = q_rot.mul(this_orient)
                this_pos = Vector(this_pos.x, this_pos.z, -this_pos.y)

            pos = [float_to_f32(this_pos.x), float_to_f32(this_pos.y),
                   float_to_f32(this_pos.z)]
            orient = [float_to_f32(this_orient.w), float_to_f32(this_orient.x),
                      float_to_f32(this_orient.y), float_to_f32(this_orient.z)]

            u32_array.extend(pos)
            u32_array.extend(orient)

    with open(output_file, "wb") as f:
        for u32 in u32_array:
            b = [u32 & 0xFF,
                 (u32 >> 8) & 0xFF,
                 (u32 >> 16) & 0xFF,
                 (u32 >> 24) & 0xFF]
            f.write(bytearray(b))
