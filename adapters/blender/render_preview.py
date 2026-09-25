"""Render deterministic front-isometric and bottom mesh previews in Blender."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import bpy
from mathutils import Vector


def point_camera(camera: bpy.types.Object, target: Vector) -> None:
    camera.rotation_euler = (target - camera.location).to_track_quat("-Z", "Y").to_euler()


def render_view(
    scene: bpy.types.Scene,
    camera: bpy.types.Object,
    target: Vector,
    location: Vector,
    output: Path,
) -> None:
    camera.location = location
    point_camera(camera, target)
    scene.render.filepath = str(output)
    bpy.ops.render.render(write_still=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--front", required=True)
    parser.add_argument("--bottom", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1 :])

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    bpy.ops.wm.stl_import(filepath=str(Path(args.input)))
    obj = next(item for item in bpy.context.selected_objects if item.type == "MESH")
    coordinates = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    minimum = Vector(tuple(min(point[i] for point in coordinates) for i in range(3)))
    maximum = Vector(tuple(max(point[i] for point in coordinates) for i in range(3)))
    center = (minimum + maximum) / 2.0
    span = max(maximum - minimum)

    material = bpy.data.materials.new("AgentSlicerPreview")
    material.use_nodes = True
    principled = material.node_tree.nodes.get("Principled BSDF")
    principled.inputs["Base Color"].default_value = (0.08, 0.42, 0.8, 1.0)
    principled.inputs["Roughness"].default_value = 0.38
    obj.data.materials.append(material)

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 800
    scene.render.resolution_y = 800
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    scene.world.use_nodes = True
    background = scene.world.node_tree.nodes.get("Background")
    background.inputs["Color"].default_value = (0.12, 0.12, 0.12, 1.0)
    background.inputs["Strength"].default_value = 0.55

    bpy.ops.object.camera_add()
    camera = bpy.context.active_object
    camera.data.lens = 58
    scene.camera = camera

    for location, energy, size in (
        (center + Vector((span, -span, span * 1.4)), 1400, span),
        (center + Vector((-span, span * 0.5, span * 0.6)), 900, span),
    ):
        bpy.ops.object.light_add(type="AREA", location=location)
        light = bpy.context.active_object
        light.data.energy = energy
        light.data.shape = "DISK"
        light.data.size = size
        point_camera(light, center)
    bpy.ops.object.light_add(
        type="SUN",
        location=center + Vector((0.0, 0.0, span * 2.0)),
    )
    bpy.context.active_object.data.energy = 2.0
    bpy.context.active_object.rotation_euler = (0.45, -0.35, -0.5)

    front = Path(args.front)
    bottom = Path(args.bottom)
    front.parent.mkdir(parents=True, exist_ok=True)
    bottom.parent.mkdir(parents=True, exist_ok=True)
    render_view(
        scene,
        camera,
        center,
        center + Vector((span * 1.35, -span * 1.65, span * 0.8)),
        front,
    )
    render_view(
        scene,
        camera,
        Vector((center.x, center.y, minimum.z)),
        Vector((center.x, center.y, minimum.z - span * 1.8)),
        bottom,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
