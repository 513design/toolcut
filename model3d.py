#!/usr/bin/env python3
"""
model3d.py — turn an edited 2D cutout outline into a printable 3D tray (STL).

A block is built around the tool (rectangular, or following the tool's shape),
and the tool outline is cut into the top face to a chosen depth, leaving a
floor beneath. All units are millimeters.
"""

import trimesh
from shapely.geometry import Polygon


def _clamp(v, lo, hi):
    return max(lo, min(hi, float(v)))


def build_tray(rings_mm, pocket_depth, wall, floor, style="rect"):
    """rings_mm: [exterior_ring, *hole_rings], each a list of [x, y] in mm."""
    pocket_depth = _clamp(pocket_depth, 1, 300)
    wall = _clamp(wall, 0, 300)
    floor = _clamp(floor, 0.5, 200)

    ext = rings_mm[0]
    holes = rings_mm[1:] if len(rings_mm) > 1 else None
    tool = Polygon(ext, holes).buffer(0)              # clean any self-touch
    if tool.geom_type == "MultiPolygon":
        tool = max(tool.geoms, key=lambda g: g.area)
    if tool.area <= 0:
        raise ValueError("outline has no area")

    minx, miny, maxx, maxy = tool.bounds
    height = pocket_depth + floor

    if style == "contour":
        bp = tool.buffer(wall, join_style=1)
        if bp.geom_type == "MultiPolygon":
            bp = max(bp.geoms, key=lambda g: g.area)
        block_poly = Polygon(bp.exterior)
    else:                                             # rectangular tray
        block_poly = Polygon([(minx - wall, miny - wall), (maxx + wall, miny - wall),
                              (maxx + wall, maxy + wall), (minx - wall, maxy + wall)])

    block = trimesh.creation.extrude_polygon(block_poly, height=height)
    pocket = trimesh.creation.extrude_polygon(tool, height=pocket_depth + 1.0)
    pocket.apply_translation((0, 0, height - pocket_depth))   # open at the top
    tray = trimesh.boolean.difference([block, pocket], engine="manifold")

    bw = round(block_poly.bounds[2] - block_poly.bounds[0], 1)
    bh = round(block_poly.bounds[3] - block_poly.bounds[1], 1)
    return tray, (bw, bh, round(height, 1))


def build_tray_stl(rings_mm, pocket_depth, wall, floor, style="rect"):
    """Returns (stl_bytes, (width_mm, depth_mm, height_mm))."""
    tray, dims = build_tray(rings_mm, pocket_depth, wall, floor, style)
    if not tray.is_watertight:
        tray.fill_holes()
    return tray.export(file_type="stl"), dims
