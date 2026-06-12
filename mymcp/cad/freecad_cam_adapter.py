"""Adapter layer for structured FreeCAD CAM operations."""

from __future__ import annotations

import json
import os
import re
from textwrap import dedent, indent
from typing import Any

from freecad_cam_constants import (
    CAM_RESULT_END,
    CAM_RESULT_START,
    DEFAULT_CLEARANCE_HEIGHT,
    DEFAULT_FEED_RATE,
    DEFAULT_PLUNGE_RATE,
    DEFAULT_SAFE_HEIGHT,
    DEFAULT_SPINDLE_RPM,
    DEFAULT_UNITS,
    ERROR_CAM_API_UNAVAILABLE,
    ERROR_EMPTY_TOOLPATH,
    ERROR_FEATURE_NOT_REACHABLE,
    ERROR_FINAL_DEPTH_OUT_OF_BOUNDS,
    ERROR_FREECAD_EXECUTION,
    ERROR_FREECAD_RESPONSE,
    ERROR_GCODE_LINT_FAILED,
    ERROR_INVALID_DEPTH,
    ERROR_INVALID_HEIGHT,
    ERROR_INVALID_INPUT,
    ERROR_SAFE_HEIGHT_LOW,
    ERROR_TOOL_DIAMETER_TOO_LARGE,
    ERROR_INVALID_TOOL,
    ERROR_TOOL_PRESET_NOT_FOUND,
    ERROR_UNSUPPORTED_POST,
    SUPPORTED_OPERATIONS,
    SUPPORTED_POSTS,
    TOOL_PRESETS,
)
from freecad_cam_response import error_response, success_response


def _tool_preset_map() -> dict[str, dict[str, Any]]:
    return {preset["id"]: preset for preset in TOOL_PRESETS}


def _format_diameter_id(diameter: float) -> str:
    return f"{diameter:g}".replace(".", "p")


def _dynamic_drill_preset(diameter: float) -> dict[str, Any]:
    diameter = float(diameter)
    return {
        "id": f"drill_{_format_diameter_id(diameter)}mm",
        "label": f"{diameter:g} mm Drill",
        "tool_type": "drill",
        "diameter_mm": diameter,
        "flutes": 2,
        "materials": ["aluminum", "plastic", "wood", "steel"],
        "recommended_rpm_range": [2000, 8000],
        "recommended_feed_range": [60, 180],
        "description": f"Runtime-created generic {diameter:g} mm drill for matching explicit hole diameters.",
        "dynamic": True,
    }


def _resolve_tool_preset(tool_preset_id: str) -> dict[str, Any] | None:
    preset = _tool_preset_map().get(tool_preset_id)
    if preset is not None:
        return preset
    if tool_preset_id.startswith("drill_") and tool_preset_id.endswith("mm"):
        raw_diameter = tool_preset_id.removeprefix("drill_").removesuffix("mm").replace("p", ".")
        try:
            diameter = float(raw_diameter)
        except ValueError:
            return None
        if diameter > 0:
            return _dynamic_drill_preset(diameter)
    return None


def _material_load_factor(material: str) -> float:
    factors = {
        "plastic": 1.1,
        "wood": 1.15,
        "aluminum": 1.0,
        "steel": 0.7,
    }
    return factors.get(material.lower(), 0.9)


def _recommend_cutting_values(preset: dict[str, Any], material: str) -> dict[str, float]:
    rpm_low, rpm_high = [float(value) for value in preset.get("recommended_rpm_range", [DEFAULT_SPINDLE_RPM] * 2)]
    feed_low, feed_high = [float(value) for value in preset.get("recommended_feed_range", [DEFAULT_FEED_RATE] * 2)]
    material_factor = _material_load_factor(material)
    spindle_rpm = round(((rpm_low + rpm_high) / 2.0) * material_factor, 3)
    feed_rate = round(((feed_low + feed_high) / 2.0) * material_factor, 3)
    plunge_rate = round(max(40.0, feed_rate * 0.4), 3)
    return {
        "spindle_rpm": spindle_rpm,
        "feed_rate": feed_rate,
        "plunge_rate": plunge_rate,
    }


def list_tool_presets() -> dict[str, Any]:
    return success_response(
        "Loaded CAM tool presets.",
        data={
            "tool_presets": TOOL_PRESETS,
            "supported_posts": list(SUPPORTED_POSTS),
            "default_units": DEFAULT_UNITS,
        },
    )


CAD_IMPORT_EXTENSIONS = {".step", ".stp", ".igs", ".iges"}


def import_cad(freecad: Any, file_path: str) -> dict[str, Any]:
    if not file_path:
        return error_response(ERROR_INVALID_INPUT, "file_path is required.")

    normalized_path = file_path.replace("\\", "/").strip()
    extension = os.path.splitext(normalized_path)[1].lower()
    if extension not in CAD_IMPORT_EXTENSIONS:
        return error_response(
            ERROR_INVALID_INPUT,
            f"Unsupported CAD import extension {extension!r}. Supported extensions: {', '.join(sorted(CAD_IMPORT_EXTENSIONS))}.",
        )

    payload = {"file_path": normalized_path, "extension": extension}
    body = """
import os
import Import

file_path = request["file_path"]
extension = request["extension"]
if not os.path.exists(file_path):
    fail("object_not_found", f'CAD file "{file_path}" was not found.')

Import.open(file_path)
active_doc = App.ActiveDocument
if active_doc is None:
    fail("freecad_response_error", "FreeCAD did not leave an active document after CAD import.")

shape_objects = []
for obj in getattr(active_doc, "Objects", []):
    if getattr(obj, "Shape", None) is None:
        continue
    bb = obj.Shape.BoundBox
    shape_objects.append(
        {
            "name": obj.Name,
            "label": obj.Label,
            "type_id": obj.TypeId,
            "bbox_mm": {
                "x": float(bb.XLength),
                "y": float(bb.YLength),
                "z": float(bb.ZLength),
            },
        }
    )

succeed(
    "CAD file imported successfully.",
    {
        "file_path": file_path,
        "extension": extension,
        "doc_name": active_doc.Name,
        "document_label": active_doc.Label,
        "shape_objects": shape_objects,
    },
)
"""
    return _run_cam_script(freecad, "cam_import_cad", payload, body)


def import_step(freecad: Any, step_path: str) -> dict[str, Any]:
    return import_cad(freecad, step_path)


def analyze_model_features(freecad: Any, doc_name: str, model_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "model_name": model_name}
    body = """
obj = doc.getObject(request["model_name"])
if obj is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.')

shape = getattr(obj, "Shape", None)
if shape is None:
    fail("invalid_input", f'Object "{obj.Name}" has no Shape and cannot be analyzed for CAM.')

bb = shape.BoundBox
face_types = {}
plane_faces = 0
cylindrical_faces = 0
hole_faces = []
planar_axis_areas = {"X": 0.0, "Y": 0.0, "Z": 0.0}
dominant_plane = None
bb_min_xy = min(float(bb.XLength), float(bb.YLength))
for face in getattr(shape, "Faces", []):
    surface_name = type(getattr(face, "Surface", None)).__name__
    face_types[surface_name] = face_types.get(surface_name, 0) + 1
    if "Plane" in surface_name:
        plane_faces += 1
        try:
            u0, _, v0, _ = face.ParameterRange
            normal = face.normalAt(u0, v0)
            axis_components = {
                "X": abs(float(normal.x)),
                "Y": abs(float(normal.y)),
                "Z": abs(float(normal.z)),
            }
            axis_name = max(axis_components, key=axis_components.get)
            if axis_components[axis_name] >= 0.95:
                planar_axis_areas[axis_name] += float(face.Area)
                if dominant_plane is None or float(face.Area) > dominant_plane["area"]:
                    dominant_plane = {"axis": axis_name, "area": float(face.Area)}
        except Exception:
            pass
    if "Cylinder" in surface_name:
        cylindrical_faces += 1
        try:
            bound = face.BoundBox
            diameter = min(float(bound.XLength), float(bound.YLength))
        except Exception:
            diameter = None
        # Treat only sub-features as candidate holes. The outer cylindrical wall
        # of a flange or boss should not be reported as a drillable hole.
        if diameter is not None and diameter < bb_min_xy * 0.95:
            hole_faces.append(face)

xy_edge_lengths = []
z_edge_lengths = []
for edge in getattr(shape, "Edges", []):
    if not hasattr(edge, "Length"):
        continue
    try:
        verts = edge.Vertexes
        if len(verts) >= 2:
            z_span = abs(float(verts[0].Z) - float(verts[-1].Z))
            if z_span <= 0.01:
                xy_edge_lengths.append(float(edge.Length))
            else:
                z_edge_lengths.append(float(edge.Length))
        else:
            xy_edge_lengths.append(float(edge.Length))
    except Exception:
        xy_edge_lengths.append(float(edge.Length))
noise_floor = max(0.2, bb_min_xy * 0.01)
usable_xy_edges = sorted(length for length in xy_edge_lengths if length >= noise_floor)
usable_z_edges = sorted(length for length in z_edge_lengths if length >= noise_floor)
min_xy_edge = min(usable_xy_edges) if usable_xy_edges else (min(xy_edge_lengths) if xy_edge_lengths else None)
min_z_step = min(usable_z_edges) if usable_z_edges else (min(z_edge_lengths) if z_edge_lengths else None)
conservative_xy_width = None
if usable_xy_edges:
    conservative_index = min(len(usable_xy_edges) - 1, max(0, int(len(usable_xy_edges) * 0.1)))
    conservative_xy_width = usable_xy_edges[conservative_index]
circle_radii = []
for edge in getattr(shape, "Edges", []):
    curve = getattr(edge, "Curve", None)
    if curve is not None and hasattr(curve, "Radius"):
        try:
            circle_radii.append(float(curve.Radius))
        except Exception:
            pass

hole_features = []
for index, face in enumerate(hole_faces, start=1):
    bound = face.BoundBox
    diameter = min(float(bound.XLength), float(bound.YLength))
    spans_full_depth = abs(float(bound.ZLength) - float(bb.ZLength)) < 0.25
    hole_features.append(
        {
            "name": f"HoleFace{index}",
            "diameter_mm": diameter,
            "depth_mm": float(bound.ZLength),
            "center_mm": [
                float((bound.XMin + bound.XMax) / 2.0),
                float((bound.YMin + bound.YMax) / 2.0),
                float((bound.ZMin + bound.ZMax) / 2.0),
            ],
            "top_z_mm": float(bound.ZMax),
            "bottom_z_mm": float(bound.ZMin),
            "classification": "through_hole" if spans_full_depth else "blind_or_side_hole",
        }
    )

z_span = float(bb.ZLength)
dominant_planar_axis = None
if any(area > 0 for area in planar_axis_areas.values()):
    dominant_planar_axis = max(planar_axis_areas, key=planar_axis_areas.get)
feature_summary = {
    "bbox_mm": {
        "xmin": float(bb.XMin),
        "xmax": float(bb.XMax),
        "ymin": float(bb.YMin),
        "ymax": float(bb.YMax),
        "zmin": float(bb.ZMin),
        "zmax": float(bb.ZMax),
        "x": float(bb.XLength),
        "y": float(bb.YLength),
        "z": z_span,
    },
    "face_count": len(getattr(shape, "Faces", [])),
    "edge_count": len(getattr(shape, "Edges", [])),
    "plane_face_count": plane_faces,
    "cylindrical_face_count": cylindrical_faces,
    "surface_type_histogram": face_types,
    "planar_axis_areas_mm2": {axis: round(area, 6) for axis, area in planar_axis_areas.items()},
    "dominant_planar_axis": dominant_planar_axis,
    "min_feature_width_mm": float(min_xy_edge) if min_xy_edge is not None else None,
    "conservative_feature_width_mm": float(conservative_xy_width) if conservative_xy_width is not None else (float(min_xy_edge) if min_xy_edge is not None else None),
    "min_z_step_mm": float(min_z_step) if min_z_step is not None else None,
    "estimated_depth_mm": z_span,
    "has_holes": cylindrical_faces > 0,
    "hole_features": hole_features,
    "through_hole_count": len([item for item in hole_features if item["classification"] == "through_hole"]),
    "blind_hole_or_side_hole_count": len([item for item in hole_features if item["classification"] != "through_hole"]),
    "corner_radius_estimates_mm": sorted(circle_radii)[:10],
    "top_down_2p5d_ready": dominant_planar_axis in (None, "Z"),
    "orientation_warning": (
        f"Model's largest planar faces are aligned with {dominant_planar_axis}, not Z. "
        "Reorientation may be required for the current +Z 2.5D workflow."
        if dominant_planar_axis not in (None, "Z")
        else None
    ),
    "recommended_operations": [op for op in ["face", "profile", "pocket", "drilling"] if (
        op == "face" and plane_faces > 0
    ) or (
        op == "profile"
    ) or (
        op == "pocket" and plane_faces >= 2
    ) or (
        op == "drilling" and cylindrical_faces > 0
    )],
}
succeed(
    "CAM feature analysis completed.",
    {
        "doc_name": request["doc_name"],
        "model_name": obj.Name,
        "feature_summary": feature_summary,
    },
)
"""
    return _run_cam_script(freecad, "cam_analyze_model_features", payload, body)


def suggest_setup(freecad: Any, doc_name: str, model_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "model_name": model_name}
    body = """
obj = doc.getObject(request["model_name"])
if obj is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.')

shape = getattr(obj, "Shape", None)
if shape is None:
    fail("invalid_input", f'Object "{obj.Name}" has no Shape and cannot be analyzed for setup.')

bb = shape.BoundBox
top_z = float(bb.ZMax)
z_len = float(bb.ZLength)
planar_axis_areas = {"X": 0.0, "Y": 0.0, "Z": 0.0}
for face in getattr(shape, "Faces", []):
    if "Plane" not in type(getattr(face, "Surface", None)).__name__:
        continue
    try:
        u0, _, v0, _ = face.ParameterRange
        normal = face.normalAt(u0, v0)
        axis_components = {
            "X": abs(float(normal.x)),
            "Y": abs(float(normal.y)),
            "Z": abs(float(normal.z)),
        }
        axis_name = max(axis_components, key=axis_components.get)
        if axis_components[axis_name] >= 0.95:
            planar_axis_areas[axis_name] += float(face.Area)
    except Exception:
        pass
dominant_planar_axis = None
if any(area > 0 for area in planar_axis_areas.values()):
    dominant_planar_axis = max(planar_axis_areas, key=planar_axis_areas.get)
stock_offsets = {
    "ExtXneg": 2.0,
    "ExtXpos": 2.0,
    "ExtYneg": 2.0,
    "ExtYpos": 2.0,
    "ExtZneg": 0.0,
    "ExtZpos": 1.0,
}
succeed(
    "Generated CAM setup suggestion.",
    {
        "doc_name": request["doc_name"],
        "model_name": obj.Name,
        "recommended_setup": {
            "axis_mode": "3-axis",
            "origin_mode": "model_min_corner",
            "origin_point_mm": [float(bb.XMin), float(bb.YMin), float(bb.ZMax)],
            "clearance": max(10.0, z_len + 5.0),
            "safe_height": max(5.0, z_len * 0.5),
            "recommended_stock_mode": "from_model",
            "recommended_stock_offsets": stock_offsets,
            "top_of_stock_z_mm": top_z + stock_offsets["ExtZpos"],
            "dominant_planar_axis": dominant_planar_axis,
            "origin_candidates": [
                {
                    "name": "model_min_corner_top",
                    "origin_point_mm": [float(bb.XMin), float(bb.YMin), float(bb.ZMax)],
                    "reason": "Stable conservative origin for 3-axis stock-aligned machining.",
                },
                {
                    "name": "model_center_top",
                    "origin_point_mm": [float((bb.XMin + bb.XMax) / 2.0), float((bb.YMin + bb.YMax) / 2.0), float(bb.ZMax)],
                    "reason": "Convenient for symmetric parts and centered probing.",
                },
            ],
            "recommended_machining_direction": f"+{dominant_planar_axis}" if dominant_planar_axis else "+Z",
            "requires_reorientation": dominant_planar_axis not in (None, "Z"),
            "reorientation_reason": (
                f"Model appears aligned for +{dominant_planar_axis} access; current phase assumes +Z top-down machining."
                if dominant_planar_axis not in (None, "Z")
                else None
            ),
        },
    },
)
"""
    return _run_cam_script(freecad, "cam_suggest_setup", payload, body)


def reorient_model_to_z(
    freecad: Any,
    doc_name: str,
    model_name: str,
    source_axis: str | None = None,
    output_name: str | None = None,
) -> dict[str, Any]:
    payload = {
        "doc_name": doc_name,
        "model_name": model_name,
        "source_axis": source_axis,
        "output_name": output_name,
    }
    body = """
obj = doc.getObject(request["model_name"])
if obj is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.')

shape = getattr(obj, "Shape", None)
if shape is None:
    fail("invalid_input", f'Object "{obj.Name}" has no Shape and cannot be reoriented for CAM.')

axis_vectors = {
    "X": App.Vector(1, 0, 0),
    "Y": App.Vector(0, 1, 0),
    "Z": App.Vector(0, 0, 1),
}

source_axis = (request.get("source_axis") or "").upper()
if source_axis not in axis_vectors:
    planar_axis_areas = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    for face in getattr(shape, "Faces", []):
        if "Plane" not in type(getattr(face, "Surface", None)).__name__:
            continue
        try:
            u0, _, v0, _ = face.ParameterRange
            normal = face.normalAt(u0, v0)
            axis_components = {
                "X": abs(float(normal.x)),
                "Y": abs(float(normal.y)),
                "Z": abs(float(normal.z)),
            }
            axis_name = max(axis_components, key=axis_components.get)
            if axis_components[axis_name] >= 0.95:
                planar_axis_areas[axis_name] += float(face.Area)
        except Exception:
            pass
    if any(area > 0 for area in planar_axis_areas.values()):
        source_axis = max(planar_axis_areas, key=planar_axis_areas.get)
    else:
        source_axis = "Z"

if source_axis == "Z":
    succeed(
        "Model is already aligned for +Z top-down machining.",
        {
            "doc_name": request["doc_name"],
            "model_name": obj.Name,
            "output_name": obj.Name,
            "source_axis": source_axis,
            "target_axis": "Z",
            "reused_original": True,
        },
    )

rotation = App.Rotation(axis_vectors[source_axis], axis_vectors["Z"])
new_name = request.get("output_name") or f"{obj.Name}_Z"
existing = doc.getObject(new_name)
if existing is None:
    reoriented = doc.addObject("Part::Feature", new_name)
else:
    reoriented = existing

reoriented.Shape = shape.copy()
reoriented.Placement = App.Placement(App.Vector(0, 0, 0), rotation)
doc.recompute()

bb = reoriented.Shape.BoundBox
succeed(
    "Model reoriented for +Z top-down machining.",
    {
        "doc_name": request["doc_name"],
        "model_name": obj.Name,
        "output_name": reoriented.Name,
        "source_axis": source_axis,
        "target_axis": "Z",
        "bbox_mm": {
            "x": float(bb.XLength),
            "y": float(bb.YLength),
            "z": float(bb.ZLength),
        },
    },
)
"""
    return _run_cam_script(freecad, "cam_reorient_model_to_z", payload, body)


def select_tool_preset(
    feature_type: str,
    min_width: float | None,
    depth: float | None,
    material: str | None,
) -> dict[str, Any]:
    material_name = (material or "aluminum").lower()
    feature_name = (feature_type or "profile").lower()
    compatible_type = "drill" if feature_name in {"hole", "drilling", "drill"} else "endmill"
    prefer_closest_fit = compatible_type == "drill"
    target_diameter = float(min_width) if compatible_type == "drill" and min_width is not None else None
    diameter_tolerance = 0.1
    candidates = []
    warnings = []
    for preset in TOOL_PRESETS:
        if preset["tool_type"] != compatible_type:
            continue
        if material_name not in [m.lower() for m in preset.get("materials", [])]:
            continue
        if target_diameter is not None and abs(float(preset["diameter_mm"]) - target_diameter) > diameter_tolerance:
            continue
        # Hard-filter only when tool is clearly too large (> 1.5x min_width)
        if min_width is not None and preset["diameter_mm"] > float(min_width) * 1.5:
            continue
        # Score: prefer tools that fit (diameter <= min_width), penalize oversized
        if min_width is not None:
            if preset["diameter_mm"] <= float(min_width):
                score = float(min_width) - preset["diameter_mm"]
            else:
                score = (preset["diameter_mm"] - float(min_width)) * 10.0
                warnings.append(
                    f"Tool {preset['label']} (D{preset['diameter_mm']}mm) is slightly larger "
                    f"than min_feature_width ({float(min_width):.1f}mm); may not enter narrowest feature."
                )
        elif prefer_closest_fit:
            score = preset["diameter_mm"]
        else:
            score = preset["diameter_mm"]
        if depth is not None:
            score += float(depth) * 0.01
        candidates.append((score, preset))

    if not candidates:
        if target_diameter is not None:
            preset = _dynamic_drill_preset(target_diameter)
            recommended_cutting_values = _recommend_cutting_values(preset, material_name)
            return success_response(
                "Created runtime CAM drill preset for the requested hole diameter.",
                data={
                    "feature_type": feature_name,
                    "material": material_name,
                    "selected_tool_preset": preset,
                    "recommended_cutting_values": recommended_cutting_values,
                    "selection_reason": (
                        f"No static drill preset matched {target_diameter:.3f}mm exactly; "
                        f"created runtime generic drill preset {preset['id']} with matching diameter."
                    ),
                    "warnings": warnings,
                },
            )
        message = "No suitable tool preset was found for the requested feature and material."
        return error_response(
            ERROR_TOOL_PRESET_NOT_FOUND,
            message,
            data={
                "feature_type": feature_name,
                "min_width_mm": min_width,
                "depth_mm": depth,
                "material": material_name,
                "required_diameter_mm": target_diameter,
                "diameter_tolerance_mm": diameter_tolerance if target_diameter is not None else None,
                "available_tools": [
                    {"id": p["id"], "diameter_mm": p["diameter_mm"], "materials": p.get("materials", [])}
                    for p in TOOL_PRESETS
                    if p["tool_type"] == compatible_type
                ],
            },
        )

    candidates.sort(key=lambda item: item[0])
    preset = candidates[0][1]
    recommended_cutting_values = _recommend_cutting_values(preset, material_name)
    return success_response(
        "Selected CAM tool preset.",
        data={
            "feature_type": feature_name,
            "material": material_name,
            "selected_tool_preset": preset,
            "recommended_cutting_values": recommended_cutting_values,
            "selection_reason": (
                f"Matched requested feature type and material. "
                f"Chose {preset['label']} (D{preset['diameter_mm']}mm) — "
                + (
                    f"closest diameter match for {float(min_width):.1f}mm feature."
                    if prefer_closest_fit and min_width is not None
                    else (
                        f"smallest compatible cutter"
                        + (f" that fits within {float(min_width):.1f}mm min feature width." if min_width is not None else ".")
                    )
                )
            ),
        },
        warnings=warnings if warnings else None,
    )


def plan_operations(
    model_name: str,
    feature_summary: dict[str, Any] | None,
    machining_goal: str | None,
) -> dict[str, Any]:
    summary = feature_summary or {}
    goal = (machining_goal or "general").lower()
    bbox = summary.get("bbox_mm") or {}
    depth = summary.get("estimated_depth_mm")
    min_width = summary.get("conservative_feature_width_mm") or summary.get("min_feature_width_mm")
    has_holes = bool(summary.get("has_holes"))
    plane_faces = int(summary.get("plane_face_count") or 0)
    hole_features = summary.get("hole_features") or []

    operations = []
    if plane_faces > 0 and goal in {"general", "finish", "surface", "roughing"}:
        operations.append(
            {
                "operation": "face",
                "feature_type": "face",
                "preferred_tool_type": "endmill",
                "reason": "Model has planar top surfaces that benefit from establishing a clean datum.",
                "params": {"StepOver": 50},
            }
        )
    if has_holes:
        operations.append(
            {
                "operation": "drilling",
                "feature_type": "drilling",
                "preferred_tool_type": "drill",
                "reason": "Cylindrical faces suggest hole-like features.",
                "params": {"PeckDepth": min(2.0, depth or 2.0)},
            }
        )
    if goal in {"general", "roughing", "pocket"}:
        operations.append(
            {
                "operation": "pocket",
                "feature_type": "pocket",
                "preferred_tool_type": "endmill",
                "reason": "Most 2.5D parts benefit from pocket clearing of recessed areas. Verify against model geometry.",
                "params": {"StepDown": min(2.0, depth or 2.0)},
            }
        )
    operations.append(
        {
            "operation": "profile",
            "feature_type": "profile",
            "preferred_tool_type": "endmill",
            "reason": "External perimeter finishing is recommended for most 2.5D parts.",
            "params": {"StepDown": min(2.0, depth or 2.0), "FinalDepth": -(depth or 1.0)},
        }
    )
    if goal in {"rough_finish", "finishing"}:
        operations.append(
            {
                "operation": "profile",
                "feature_type": "profile",
                "preferred_tool_type": "endmill",
                "reason": "Add a second light finishing profile pass for better edge quality.",
                "params": {"StepDown": min(1.0, depth or 1.0), "FinishDepth": -(depth or 1.0)},
            }
        )

    return success_response(
        "Generated CAM operation plan.",
        data={
            "model_name": model_name,
            "machining_goal": goal,
            "recommended_operations": operations,
            "planning_inputs": {
                "min_feature_width_mm": min_width,
                "estimated_depth_mm": depth,
                "has_holes": has_holes,
                "hole_feature_count": len(hole_features),
            },
        },
    )



def resolve_operation_features(
    freecad: Any,
    doc_name: str,
    model_name: str,
    operation_type: str,
    strategy: str = "conservative",
) -> dict[str, Any]:
    op_type = (operation_type or "").lower()
    if op_type not in {"face", "pocket", "profile", "drilling"}:
        return error_response(ERROR_INVALID_INPUT, f'Unsupported operation_type "{operation_type}".')

    payload = {
        "doc_name": doc_name,
        "model_name": model_name,
        "operation_type": op_type,
        "strategy": strategy or "conservative",
    }
    body = """
obj = doc.getObject(request["model_name"])
if obj is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.')

shape = getattr(obj, "Shape", None)
if shape is None:
    fail("invalid_input", f'Object "{obj.Name}" has no Shape and cannot be inspected for CAM features.')

op_type = (request.get("operation_type") or "").lower()
strategy = request.get("strategy") or "conservative"
bb = shape.BoundBox
bbox_x = float(bb.XLength)
bbox_y = float(bb.YLength)
bbox_z = float(bb.ZLength)
tol = max(0.05, min(bbox_x, bbox_y, bbox_z) * 0.002)

face_candidates = []
drilling_locations = []

for idx, face in enumerate(getattr(shape, "Faces", []), start=1):
    surface_name = type(getattr(face, "Surface", None)).__name__
    fb = face.BoundBox
    center_x = float((fb.XMin + fb.XMax) / 2.0)
    center_y = float((fb.YMin + fb.YMax) / 2.0)
    center_z = float((fb.ZMin + fb.ZMax) / 2.0)
    area = float(getattr(face, "Area", 0.0) or 0.0)
    face_ref = {
        "object_name": obj.Name,
        "subelements": [f"Face{idx}"],
    }

    if "Plane" in surface_name:
        try:
            u0, _, v0, _ = face.ParameterRange
            normal = face.normalAt(u0, v0)
            nx = float(normal.x)
            ny = float(normal.y)
            nz = float(normal.z)
        except Exception:
            nx = ny = nz = 0.0

        touches_xmin = abs(float(fb.XMin) - float(bb.XMin)) <= tol and abs(float(fb.XMax) - float(bb.XMin)) <= tol
        touches_xmax = abs(float(fb.XMin) - float(bb.XMax)) <= tol and abs(float(fb.XMax) - float(bb.XMax)) <= tol
        touches_ymin = abs(float(fb.YMin) - float(bb.YMin)) <= tol and abs(float(fb.YMax) - float(bb.YMin)) <= tol
        touches_ymax = abs(float(fb.YMin) - float(bb.YMax)) <= tol and abs(float(fb.YMax) - float(bb.YMax)) <= tol

        candidate = {
            "face_index": idx,
            "object_name": obj.Name,
            "subelements": [f"Face{idx}"],
            "area_mm2": area,
            "center_mm": [center_x, center_y, center_z],
            "normal": {"x": nx, "y": ny, "z": nz},
        }

        if op_type == "face":
            if abs(nz) >= 0.95 and abs(center_z - float(bb.ZMax)) <= tol:
                face_candidates.append(candidate)
        elif op_type == "pocket":
            if abs(nz) >= 0.95 and (float(bb.ZMin) + tol) < center_z < (float(bb.ZMax) - tol):
                face_candidates.append(candidate)
        elif op_type == "profile":
            if abs(nz) <= 0.2:
                if strategy == "all_side_faces":
                    candidate["wall"] = f"side_{idx}"
                    face_candidates.append(candidate)
                elif touches_xmin or touches_xmax or touches_ymin or touches_ymax:
                    if touches_xmin:
                        candidate["wall"] = "xmin"
                    elif touches_xmax:
                        candidate["wall"] = "xmax"
                    elif touches_ymin:
                        candidate["wall"] = "ymin"
                    elif touches_ymax:
                        candidate["wall"] = "ymax"
                    face_candidates.append(candidate)

    elif "Cylinder" in surface_name:
        # Cylindrical faces: profile candidates (vertical walls) or drilling candidates (holes)
        if op_type == "profile":
            try:
                cyl_axis = face.Surface.Axis
                az = float(cyl_axis.z)
                # Vertical cylinder (axis along Z) -> potential profile side face
                if abs(az) >= 0.95:
                    radius = float(face.Surface.Radius)
                    candidate = {
                        "face_index": idx,
                        "object_name": obj.Name,
                        "subelements": [f"Face{idx}"],
                        "area_mm2": area,
                        "center_mm": [center_x, center_y, center_z],
                        "radius_mm": radius,
                        "surface_type": "Cylinder",
                    }
                    touches_boundary = (
                        abs(float(fb.XMin) - float(bb.XMin)) <= tol or
                        abs(float(fb.XMax) - float(bb.XMax)) <= tol or
                        abs(float(fb.YMin) - float(bb.YMin)) <= tol or
                        abs(float(fb.YMax) - float(bb.YMax)) <= tol
                    )
                    if strategy == "all_side_faces":
                        candidate["wall"] = f"cyl_side_{idx}"
                        face_candidates.append(candidate)
                    elif touches_boundary:
                        candidate["wall"] = "outer_cylinder"
                        face_candidates.append(candidate)
            except Exception:
                pass
        elif op_type == "drilling":
            x_len = abs(float(fb.XMax) - float(fb.XMin))
            y_len = abs(float(fb.YMax) - float(fb.YMin))
            z_len = abs(float(fb.ZMax) - float(fb.ZMin))
            dims = {"X": x_len, "Y": y_len, "Z": z_len}
            axis = max(dims, key=dims.get)
            radial_dims = sorted([x_len, y_len, z_len])[:2]
            diameter = sum(radial_dims) / len(radial_dims) if radial_dims else 0.0
            if axis == "Z" and diameter > 0.0 and diameter < min(bbox_x, bbox_y) * 0.95:
                drilling_locations.append(
                {
                    "x": center_x,
                    "y": center_y,
                    "z": float(bb.ZMax),
                    "diameter_mm": diameter,
                    "source_face": f"Face{idx}",
                }
            )

if op_type == "face":
    face_candidates.sort(key=lambda item: (-item["area_mm2"], -item["center_mm"][2], item["face_index"]))
elif op_type == "pocket":
    face_candidates.sort(key=lambda item: (-item["center_mm"][2], -item["area_mm2"], item["face_index"]))
elif op_type == "profile":
    if strategy == "all_side_faces":
        face_candidates.sort(key=lambda item: (-item["area_mm2"], item["face_index"]))
    else:
        wall_order = {"xmin": 0, "xmax": 1, "ymin": 2, "ymax": 3}
        face_candidates.sort(key=lambda item: (wall_order.get(item.get("wall", ""), 99), -item["area_mm2"], item["face_index"]))
        unique = []
        seen_walls = set()
        for item in face_candidates:
            wall = item.get("wall")
            if wall in seen_walls:
                continue
            seen_walls.add(wall)
            unique.append(item)
        face_candidates = unique
elif op_type == "drilling":
    drilling_locations.sort(key=lambda item: (item["x"], item["y"], item["diameter_mm"]))

recommended_base_features = []
if op_type in {"face", "pocket"} and face_candidates:
    recommended_base_features = [
        {
            "object_name": item["object_name"],
            "subelements": item["subelements"],
        }
        for item in face_candidates[:1]
    ]
elif op_type == "profile" and face_candidates:
    recommended_base_features = [
        {
            "object_name": item["object_name"],
            "subelements": item["subelements"],
        }
        for item in face_candidates
    ]

succeed(
    "Resolved CAM operation feature candidates.",
    {
        "doc_name": request["doc_name"],
        "model_name": obj.Name,
        "operation_type": op_type,
        "strategy": strategy,
        "recommended_base_features": recommended_base_features,
        "feature_candidates": face_candidates,
        "recommended_locations": drilling_locations,
        "candidate_count": len(face_candidates) if op_type != "drilling" else len(drilling_locations),
    },
)
"""
    return _run_cam_script(freecad, "cam_resolve_operation_features", payload, body)

def create_job(freecad: Any, doc_name: str, job_name: str, model_name: str, units: str = DEFAULT_UNITS) -> dict[str, Any]:
    if units != DEFAULT_UNITS:
        return error_response(ERROR_INVALID_INPUT, "Only millimeter CAM jobs are supported in this phase.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "model_name": model_name,
        "units": units,
    }
    body = """
target = doc.getObject(request["model_name"])
if target is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.', data={"doc_name": request["doc_name"]})

job_name = request["job_name"]
existing = doc.getObject(job_name)
if existing is not None:
    succeed(
        "CAM job already exists.",
        {
            "doc_name": request["doc_name"],
            "job_name": existing.Name,
            "job_label": existing.Label,
            "model_name": target.Name,
            "reused_existing": True,
        },
    )

job_module = import_module_candidates(["Path.Main.Job"])
if not hasattr(job_module, "Create"):
    fail("cam_api_unavailable", "Path.Main.Job.Create is unavailable in the connected FreeCAD runtime.")

job = job_module.Create(job_name, [target], None)
job.JobType = "2.5D"
doc.recompute()
# Clean up auto-created default TC (spindle=0) that FreeCAD
# generates during Job creation -- prevents residual zombie TC buildup.
for obj in list(doc.Objects):
    if hasattr(obj, 'SpindleSpeed') and (obj.SpindleSpeed is None or obj.SpindleSpeed == 0):
        doc.removeObject(obj.Name)
doc.recompute()
succeed(
    "CAM job created successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "job_label": job.Label,
        "model_name": target.Name,
        "job_type": getattr(job, "JobType", "2.5D"),
    },
)
"""
    return _run_cam_script(freecad, "cam_create_job", payload, body)


def set_stock(freecad: Any, doc_name: str, job_name: str, stock_mode: str, offsets_or_bounds: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "stock_mode": stock_mode,
        "offsets_or_bounds": offsets_or_bounds or {},
    }
    body = """
job = get_job(doc, request["job_name"])
mode = request.get("stock_mode") or "from_model"
options = request.get("offsets_or_bounds") or {}

stock_module = import_module_candidates(["Path.Main.Stock"])
if mode == "from_model":
    neg = None
    pos = None
    if options:
        neg = App.Vector(
            float(options.get("ExtXneg", 1.0)),
            float(options.get("ExtYneg", 1.0)),
            float(options.get("ExtZneg", 1.0)),
        )
        pos = App.Vector(
            float(options.get("ExtXpos", 1.0)),
            float(options.get("ExtYpos", 1.0)),
            float(options.get("ExtZpos", 1.0)),
        )
    stock = stock_module.CreateFromBase(job, neg=neg, pos=pos)
elif mode == "from_boundbox":
    extent = None
    if all(key in options for key in ["Length", "Width", "Height"]):
        extent = App.Vector(
            float(options["Length"]),
            float(options["Width"]),
            float(options["Height"]),
        )
    stock = stock_module.CreateBox(job, extent=extent)
else:
    fail("invalid_input", f'Unsupported stock mode "{mode}".')

for prop in ["ExtXneg", "ExtXpos", "ExtYneg", "ExtYpos", "ExtZneg", "ExtZpos"]:
    if prop in options and hasattr(stock, prop):
        setattr(stock, prop, float(options[prop]))

job.Stock = stock
doc.recompute()
succeed(
    "CAM stock configured successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "stock_name": getattr(stock, "Name", ""),
        "stock_mode": mode,
        "applied_offsets": {k: options[k] for k in options if k in ["ExtXneg", "ExtXpos", "ExtYneg", "ExtYpos", "ExtZneg", "ExtZpos"]},
    },
)
"""
    return _run_cam_script(freecad, "cam_set_stock", payload, body)


def set_wcs(
    freecad: Any,
    doc_name: str,
    job_name: str,
    origin_mode: str,
    origin_params: dict[str, Any] | None,
    clearance: float,
    safe_height: float,
) -> dict[str, Any]:
    if clearance <= 0 or safe_height <= 0:
        return error_response(ERROR_INVALID_HEIGHT, "Clearance and safe height must be positive numbers.")
    if safe_height > clearance:
        return error_response(ERROR_INVALID_HEIGHT, "Safe height cannot be above clearance height.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "origin_mode": origin_mode,
        "origin_params": origin_params or {},
        "clearance": clearance,
        "safe_height": safe_height,
    }
    body = """
job = get_job(doc, request["job_name"])
sheet = getattr(job, "SetupSheet", None)
if sheet is None:
    fail("cam_api_unavailable", "Job has no SetupSheet; CAM WCS cannot be configured.")

params = request.get("origin_params") or {}
fixtures = getattr(job, "Fixtures", ["G54"]) or ["G54"]
job.Fixtures = fixtures

stock = getattr(job, "Stock", None)
stock_top_z = None
stock_bottom_z = None
if stock is not None and hasattr(stock, "Shape"):
    try:
        bb = stock.Shape.BoundBox
        stock_top_z = float(bb.ZMax)
        stock_bottom_z = float(bb.ZMin)
    except Exception:
        pass

model_top_z = None
model_bottom_z = None
model_group = getattr(getattr(job, "Model", None), "Group", []) or []
for model_obj in model_group:
    shape = getattr(model_obj, "Shape", None)
    if shape is None:
        continue
    try:
        z_max = float(shape.BoundBox.ZMax)
        z_min = float(shape.BoundBox.ZMin)
    except Exception:
        continue
    model_top_z = z_max if model_top_z is None else max(model_top_z, z_max)
    model_bottom_z = z_min if model_bottom_z is None else min(model_bottom_z, z_min)

property_candidates = {
    "ClearanceHeight": request["clearance"],
    "SafeHeight": request["safe_height"],
    "StartDepth": params.get("start_depth"),
    "FinalDepth": params.get("final_depth"),
}
for prop, value in property_candidates.items():
    if value is None:
        continue
    if hasattr(sheet, prop):
        setattr(sheet, prop, float(value))

if stock_top_z is not None:
    if hasattr(sheet, "SafeHeightOffset"):
        try:
            sheet.SafeHeightOffset = float(request["safe_height"]) - stock_top_z
        except Exception:
            pass
    if hasattr(sheet, "ClearanceHeightOffset"):
        try:
            sheet.ClearanceHeightOffset = float(request["clearance"]) - stock_top_z
        except Exception:
            pass

# FreeCAD 1.0 CAM commonly drives operation depths from expressions rather than
# direct SetupSheet distances. When explicit offsets are provided, write
# conservative expressions if available.
start_depth = params.get("start_depth")
final_depth = params.get("final_depth")
if start_depth is not None and hasattr(sheet, "StartDepthExpression"):
    try:
        sheet.StartDepthExpression = str(float(start_depth))
    except Exception:
        pass
if final_depth is not None and hasattr(sheet, "FinalDepthExpression"):
    try:
        if float(final_depth) < 0:
            # Interpret negative values as depths below the selected top datum.
            # Prefer model top for model-aligned origins; otherwise fall back to stock top.
            datum_top = model_top_z if model_top_z is not None else stock_top_z
            resolved_final = datum_top + float(final_depth) if datum_top is not None else float(final_depth)
            sheet.FinalDepthExpression = str(float(resolved_final))
        else:
            sheet.FinalDepthExpression = str(float(final_depth))
    except Exception:
        pass

origin_mode = request.get("origin_mode") or "model_min_corner"
origin_params = request.get("origin_params") or {}
wcs_origin_model_mm = None
z_zero_at = "freecad_default"
expected_cutting_z_sign = "unknown"
if origin_mode in ("model_top_center", "model_center_top"):
    z_zero = model_top_z
    x_zero = origin_params.get("x", 0.0)
    y_zero = origin_params.get("y", 0.0)
    if z_zero is not None:
        wcs_origin_model_mm = {"x": float(x_zero), "y": float(y_zero), "z": float(z_zero)}
        z_zero_at = "model_top"
        expected_cutting_z_sign = "negative"
elif origin_mode == "stock_top_center":
    z_zero = stock_top_z
    x_zero = origin_params.get("x", 0.0)
    y_zero = origin_params.get("y", 0.0)
    if z_zero is not None:
        wcs_origin_model_mm = {"x": float(x_zero), "y": float(y_zero), "z": float(z_zero)}
        z_zero_at = "stock_top"
        expected_cutting_z_sign = "negative"
elif origin_mode == "custom":
    if all(k in origin_params for k in ("x", "y", "z")):
        wcs_origin_model_mm = {
            "x": float(origin_params["x"]),
            "y": float(origin_params["y"]),
            "z": float(origin_params["z"]),
        }
        z_zero_at = "custom"
        expected_cutting_z_sign = "negative"
if hasattr(sheet, "WcsOrigin"):
    try:
        sheet.WcsOrigin = origin_mode
    except Exception:
        pass
if wcs_origin_model_mm is not None:
    for prop, value in [
        ("AgentCadWcsOriginX", wcs_origin_model_mm["x"]),
        ("AgentCadWcsOriginY", wcs_origin_model_mm["y"]),
        ("AgentCadWcsOriginZ", wcs_origin_model_mm["z"]),
    ]:
        if not hasattr(job, prop):
            try:
                job.addProperty("App::PropertyFloat", prop, "AgentCAD", "Model-space WCS origin used by MCP postprocessing.")
            except Exception:
                pass
        if hasattr(job, prop):
            try:
                setattr(job, prop, float(value))
            except Exception:
                pass

doc.recompute()
succeed(
    "CAM WCS configured successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "origin_mode": origin_mode,
        "clearance": request["clearance"],
        "safe_height": request["safe_height"],
        "stock_top_z_mm": stock_top_z,
        "stock_bottom_z_mm": stock_bottom_z,
        "model_top_z_mm": model_top_z,
        "model_bottom_z_mm": model_bottom_z,
        "wcs_origin_model_mm": wcs_origin_model_mm,
        "z_zero_at": z_zero_at,
        "expected_cutting_z_sign": expected_cutting_z_sign,
        "fixtures": list(getattr(job, "Fixtures", [])),
    },
)
"""
    return _run_cam_script(freecad, "cam_set_wcs", payload, body)


def add_rect_pocket_operation(
    freecad: Any,
    doc_name: str,
    job_name: str,
    model_name: str,
    boundary: dict[str, Any],
    top_z: float,
    final_z: float,
    tool_controller: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Create a pocket from an explicit XY rectangle instead of inferring from final-model faces."""
    params = dict(params or {})
    if tool_controller == "":
        return error_response(ERROR_INVALID_TOOL, "A tool controller name is required.")
    for key in ("xmin", "xmax", "ymin", "ymax"):
        if key not in boundary:
            return error_response(ERROR_INVALID_INPUT, f'boundary requires "{key}".')
    xmin = float(boundary["xmin"])
    xmax = float(boundary["xmax"])
    ymin = float(boundary["ymin"])
    ymax = float(boundary["ymax"])
    if xmax <= xmin or ymax <= ymin:
        return error_response(ERROR_INVALID_INPUT, "boundary xmax/ymax must be greater than xmin/ymin.")
    if "StartDepth" not in params:
        params["StartDepth"] = float(top_z)
    if "FinalDepth" not in params:
        params["FinalDepth"] = float(final_z)
    if "StepDown" not in params:
        params["StepDown"] = abs(float(top_z) - float(final_z)) or 1.0
    _validate_optional_depths(params)

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "model_name": model_name,
        "boundary": {"xmin": xmin, "xmax": xmax, "ymin": ymin, "ymax": ymax},
        "top_z": float(top_z),
        "final_z": float(final_z),
        "tool_controller": tool_controller,
        "params": params,
    }
    body = """
import FreeCAD as App
import Part

job = get_job(doc, request["job_name"])
tc = doc.getObject(request["tool_controller"])
if tc is None:
    fail("object_not_found", f'Tool controller "{request["tool_controller"]}" was not found.')
model_obj = doc.getObject(request["model_name"])
if model_obj is None:
    fail("object_not_found", f'Model object "{request["model_name"]}" was not found.')

boundary = request["boundary"]
z = float(request["top_z"])
pts = [
    App.Vector(float(boundary["xmin"]), float(boundary["ymin"]), z),
    App.Vector(float(boundary["xmax"]), float(boundary["ymin"]), z),
    App.Vector(float(boundary["xmax"]), float(boundary["ymax"]), z),
    App.Vector(float(boundary["xmin"]), float(boundary["ymax"]), z),
]
edges = [Part.makeLine(pts[i], pts[(i + 1) % 4]) for i in range(4)]
wire = Part.Wire(edges)
face = Part.Face(wire)
boundary_name = f'RectPocketBoundary_{len(getattr(job.Operations, "Group", [])) + 1}'
boundary_obj = doc.addObject("Part::Feature", boundary_name)
boundary_obj.Shape = face
boundary_obj.Label = boundary_name
try:
    boundary_obj.ViewObject.Visibility = False
except Exception:
    pass
doc.recompute()

params = request.get("params") or {}
op_module = import_module_candidates(["Path.Op.PocketShape", "Path.Op.Pocket"])
if not hasattr(op_module, "Create"):
    fail("cam_api_unavailable", "Path.Op.PocketShape/Create() is unavailable.")
op_label = f'Pocket_{len(getattr(job.Operations, "Group", [])) + 1}'
op_obj = doc.addObject("Path::FeaturePython", op_label)
if not hasattr(op_obj, "DoNotSetDefaultValues"):
    op_obj.addProperty("App::PropertyBool", "DoNotSetDefaultValues", "CAM", "Skip FreeCAD default operation setup.")
op_obj.DoNotSetDefaultValues = True
op = op_module.Create(op_label, obj=op_obj, parentJob=job)
op.ToolController = tc
if hasattr(op, "Base"):
    op.Base = [(boundary_obj, ["Face1"])]
if hasattr(op, "UseOutline") and "UseOutline" not in params:
    op.UseOutline = True
if hasattr(op, "StepOver") and "StepOver" not in params:
    op.StepOver = 50
if hasattr(op, "CutMode") and "CutMode" not in params:
    op.CutMode = "Climb"
if hasattr(op, "ZigZagAngle") and "ZigZagAngle" not in params:
    op.ZigZagAngle = 45.0
if hasattr(op, "ClearingPattern") and "ClearingPattern" not in params:
    op.ClearingPattern = "Offset"

expression_keys = {"StartDepth", "FinalDepth", "StepDown", "FinishDepth", "RetractHeight", "ClearanceHeight", "SafeHeight"}
for key, value in params.items():
    if not hasattr(op, key):
        continue
    if key in expression_keys and hasattr(op, "setExpression"):
        try:
            op.setExpression(key, None)
        except Exception:
            pass
    try:
        setattr(op, key, value)
    except Exception:
        try:
            setattr(op, key, float(value))
        except Exception:
            pass

if "StartDepth" in params and hasattr(op, "OpStartDepth"):
    try:
        op.OpStartDepth = float(params["StartDepth"])
    except Exception:
        pass
if "FinalDepth" in params and hasattr(op, "OpFinalDepth"):
    try:
        op.OpFinalDepth = float(params["FinalDepth"])
    except Exception:
        pass

if hasattr(op, "DoNotSetDefaultValues"):
    op.DoNotSetDefaultValues = False
if hasattr(job, "Proxy") and hasattr(job.Proxy, "addOperation"):
    job.Proxy.addOperation(op)
doc.recompute()

succeed(
    "Rectangular pocket operation created successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_name": op.Name,
        "operation_label": op.Label,
        "operation_type": "rect_pocket",
        "boundary_object": boundary_obj.Name,
        "boundary": boundary,
        "top_z": request["top_z"],
        "final_z": request["final_z"],
        "tool_controller": tc.Name,
    },
)
"""
    return _run_cam_script(freecad, "cam_add_rect_pocket_operation", payload, body)


def create_tool_controller(
    freecad: Any,
    doc_name: str,
    job_name: str,
    tool_preset_id: str,
    spindle_rpm: float,
    feed_rate: float,
    plunge_rate: float,
) -> dict[str, Any]:
    preset = _tool_preset_map().get(tool_preset_id)
    if preset is None:
        return error_response(ERROR_TOOL_PRESET_NOT_FOUND, f'Unknown tool preset "{tool_preset_id}".')
    if spindle_rpm <= 0 or feed_rate <= 0 or plunge_rate <= 0:
        return error_response(ERROR_INVALID_TOOL, "Spindle RPM, feed rate, and plunge rate must all be positive.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "tool_preset": preset,
        "spindle_rpm": spindle_rpm,
        "feed_rate": feed_rate,
        "plunge_rate": plunge_rate,
    }
    body = """
job = get_job(doc, request["job_name"])
preset = request["tool_preset"]
feed_rate_mm_min = float(request["feed_rate"])
plunge_rate_mm_min = float(request["plunge_rate"])

# The MCP-facing API and skill talk in mm/min, while the connected FreeCAD
# runtime stores controller feed values in mm/s and converts them during post.
feed_rate_internal = feed_rate_mm_min / 60.0
plunge_rate_internal = plunge_rate_mm_min / 60.0

controller_module = import_module_candidates(["Path.Tool.Controller"])
if not hasattr(controller_module, "Create"):
    fail("cam_api_unavailable", "Path.Tool.Controller.Create is unavailable in the connected FreeCAD runtime.")

tool_number = job.Proxy.nextToolNumber() if hasattr(job, "Proxy") and hasattr(job.Proxy, "nextToolNumber") else 1
tc_name = f'TC_{preset["id"]}'
existing = doc.getObject(tc_name)
if existing is None:
    tc = controller_module.Create(
        name=tc_name,
        tool=None,
        toolNumber=tool_number,
        assignViewProvider=False,
        assignTool=True,
    )
    if hasattr(job, "Proxy") and hasattr(job.Proxy, "addToolController"):
        job.Proxy.addToolController(tc)
else:
    tc = existing
    current_tools = list(getattr(getattr(job, "Tools", None), "Group", []) or [])
    if tc not in current_tools and hasattr(job, "Proxy") and hasattr(job.Proxy, "addToolController"):
        job.Proxy.addToolController(tc)

tc.Label = preset["label"]
tc.ToolNumber = tool_number
tc.SpindleSpeed = float(request["spindle_rpm"])
tc.HorizFeed = feed_rate_internal
tc.VertFeed = plunge_rate_internal
if hasattr(tc, "RampFeed"):
    tc.RampFeed = plunge_rate_internal
if hasattr(tc, "HorizRapid"):
    tc.HorizRapid = feed_rate_internal * 2.0
if hasattr(tc, "VertRapid"):
    tc.VertRapid = plunge_rate_internal * 2.0
if hasattr(tc, "LeadInFeed"):
    tc.LeadInFeed = feed_rate_internal
if hasattr(tc, "LeadOutFeed"):
    tc.LeadOutFeed = feed_rate_internal
if hasattr(tc, "Tool") and tc.Tool:
    if hasattr(tc.Tool, "Diameter"):
        tc.Tool.Diameter = float(preset["diameter_mm"])
    if hasattr(tc.Tool, "Label"):
        tc.Tool.Label = preset["label"]
if hasattr(tc, "SpindleDir"):
    tc.SpindleDir = "Forward"

# FreeCAD 1.0 creates a placeholder default tool controller that confuses
# automatic operation creation when multiple controllers exist and no GUI
# selection context is available. Remove it once an explicit controller exists.
tools_group = list(getattr(getattr(job, "Tools", None), "Group", []) or [])
for existing_tc in tools_group:
    if getattr(existing_tc, "Name", "") != "TC__Default_Tool":
        continue
    try:
        if hasattr(job, "Proxy") and hasattr(job.Proxy, "removeObject"):
            job.Proxy.removeObject(existing_tc)
    except Exception:
        pass
    try:
        doc.removeObject(existing_tc.Name)
    except Exception:
        pass

doc.recompute()
succeed(
    "CAM tool controller created successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "tool_controller_name": tc.Name,
        "tool_controller_label": tc.Label,
        "tool_preset_id": preset["id"],
        "tool_number": tc.ToolNumber,
        "diameter_mm": preset["diameter_mm"],
        "spindle_rpm": float(request["spindle_rpm"]),
        "feed_rate_input_mm_min": feed_rate_mm_min,
        "plunge_rate_input_mm_min": plunge_rate_mm_min,
        "feed_rate_internal_mm_s": feed_rate_internal,
        "plunge_rate_internal_mm_s": plunge_rate_internal,
    },
)
"""
    return _run_cam_script(freecad, "cam_create_tool_controller", payload, body)


def add_operation(
    freecad: Any,
    operation_name: str,
    doc_name: str,
    job_name: str,
    base_features: list[dict[str, Any]] | list[str],
    tool_controller: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    op_type = operation_name.lower()
    if op_type not in SUPPORTED_OPERATIONS:
        return error_response(ERROR_INVALID_INPUT, f'Unsupported CAM operation "{operation_name}".')

    params = params or {}
    _validate_optional_depths(params)
    if tool_controller == "":
        return error_response(ERROR_INVALID_TOOL, "A tool controller name is required.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "base_features": base_features,
        "tool_controller": tool_controller,
        "params": params,
        "operation_name": operation_name,
    }
    body = """
job = get_job(doc, request["job_name"])
tc = doc.getObject(request["tool_controller"])
if tc is None:
    fail("object_not_found", f'Tool controller "{request["tool_controller"]}" was not found.')

feature_refs = normalize_features(doc, request.get("base_features") or [])
if not feature_refs:
    fail("invalid_input", "At least one base feature is required for a CAM operation.")

params = request.get("params") or {}
op_name = request["operation_name"].lower()

def apply_overrides(op, params):
    depth_like = {"StartDepth", "FinalDepth", "StepDown", "FinishDepth", "RetractHeight"}
    height_like = {"ClearanceHeight", "SafeHeight"}
    expression_keys = depth_like | height_like
    for key, value in params.items():
        if not hasattr(op, key):
            continue
        if key in expression_keys and hasattr(op, "setExpression"):
            try:
                op.setExpression(key, None)
            except Exception:
                pass
        try:
            setattr(op, key, value)
        except Exception:
            try:
                setattr(op, key, float(value))
            except Exception:
                pass

def bind_runtime_defaults(op, tc, job, params, op_name=None):
    if hasattr(op, "Active"):
        op.Active = True
    tool = getattr(tc, "Tool", None)
    if hasattr(op, "OpToolDiameter") and tool is not None and hasattr(tool, "Diameter"):
        try:
            op.OpToolDiameter = float(tool.Diameter)
        except Exception:
            pass
    sheet = getattr(job, "SetupSheet", None)
    for prop in ["SafeHeight", "ClearanceHeight"]:
        if hasattr(op, prop):
            explicit_value = params.get(prop)
            if explicit_value is not None:
                try:
                    setattr(op, prop, float(explicit_value))
                    continue
                except Exception:
                    pass
            if sheet is not None and hasattr(op, "setExpression"):
                expr = None
                if prop == "SafeHeight" and hasattr(sheet, "SafeHeightOffset"):
                    expr = "OpStockZMax + SetupSheet.SafeHeightOffset"
                elif prop == "ClearanceHeight" and hasattr(sheet, "ClearanceHeightOffset"):
                    expr = "OpStockZMax + SetupSheet.ClearanceHeightOffset"
                if expr:
                    try:
                        op.setExpression(prop, expr)
                        continue
                    except Exception:
                        pass
            if sheet is not None and hasattr(sheet, prop):
                try:
                    value = getattr(sheet, prop)
                    setattr(op, prop, float(value.Value) if hasattr(value, "Value") else float(value))
                except Exception:
                    pass
    if "StartDepth" in params and hasattr(op, "OpStartDepth"):
        try:
            op.OpStartDepth = float(params["StartDepth"])
        except Exception:
            pass
    if "FinalDepth" in params and hasattr(op, "OpFinalDepth"):
        try:
            op.OpFinalDepth = float(params["FinalDepth"])
        except Exception:
            pass
    if op_name == "profile" and hasattr(op, "UseComp") and "UseComp" not in params:
        try:
            op.UseComp = True
        except Exception:
            pass

module_map = {
    "profile": "Path.Op.Profile",
    "pocket": "Path.Op.Pocket",
    "drilling": "Path.Op.Drilling",
    "face": "Path.Op.MillFace",
}
op_module = import_module_candidates([module_map[op_name]])
if not hasattr(op_module, "Create"):
    fail("cam_api_unavailable", f'Create() is unavailable for operation "{op_name}".')
op_label = f'{op_name.title()}_{len(getattr(job.Operations, "Group", [])) + 1}'
op_obj = doc.addObject("Path::FeaturePython", op_label)
if not hasattr(op_obj, "DoNotSetDefaultValues"):
    op_obj.addProperty(
        "App::PropertyBool",
        "DoNotSetDefaultValues",
        "CAM",
        "Skip FreeCAD default operation setup so MCP can bind tool/depths explicitly.",
    )
op_obj.DoNotSetDefaultValues = True
try:
    op = op_module.Create(op_label, obj=op_obj, parentJob=job)
except Exception as exc:
    if op_name == "drilling" and "NoneType" in str(exc):
        fail(
            "cam_api_unavailable",
            "The connected FreeCAD drilling API requires additional model context and cannot be created through the current headless path.",
            detail=str(exc),
            data={
                "runtime_hint": "FreeCAD 1.0 Path.Op.Drilling.Create() calls findAllHoles() before the structured MCP path can finish binding model/tool context.",
                "suggested_fallback": "Use profile for outer contour first, or patch the drilling adapter to build and populate the operation proxy manually for this runtime.",
            },
        )
    raise
if hasattr(op, "ToolController"):
    op.ToolController = tc
if hasattr(op, "Base"):
    op.Base = feature_refs

# Apply conservative defaults informed by CAM operation source modules.
if op_name == "profile":
    if hasattr(op, "processPerimeter"):
        op.processPerimeter = True
    if hasattr(op, "processHoles"):
        op.processHoles = False
    if hasattr(op, "processCircles"):
        op.processCircles = True
    if hasattr(op, "Side") and not params.get("Side"):
        op.Side = "Outside"
elif op_name == "drilling":
    if hasattr(op, "PeckEnabled") and "PeckEnabled" not in params:
        op.PeckEnabled = True
    if hasattr(op, "PeckDepth") and "PeckDepth" not in params:
        op.PeckDepth = min(2.0, float(params.get("StepDown", 2.0)))
    if hasattr(op, "KeepToolDown") and "KeepToolDown" not in params:
        op.KeepToolDown = False
elif op_name == "face":
    if hasattr(op, "StepOver") and "StepOver" not in params:
        op.StepOver = 50
    if hasattr(op, "ZigZagAngle") and "ZigZagAngle" not in params:
        op.ZigZagAngle = 45.0
    if hasattr(op, "ExcludeRaisedAreas") and "ExcludeRaisedAreas" not in params:
        op.ExcludeRaisedAreas = False
elif op_name == "pocket":
    if hasattr(op, "PocketStepover") and "PocketStepover" not in params:
        op.PocketStepover = 50
    if hasattr(op, "CutMode") and "CutMode" not in params:
        op.CutMode = "Climb"
    if hasattr(op, "ZigZagAngle") and "ZigZagAngle" not in params:
        op.ZigZagAngle = 0.0
    if hasattr(op, "Side") and not params.get("Side"):
        op.Side = "Outside"

apply_overrides(op, params)
bind_runtime_defaults(op, tc, job, params, op_name)
if hasattr(op, "DoNotSetDefaultValues"):
    op.DoNotSetDefaultValues = False

# Auto-compute missing depth parameters from model geometry.
# Without FinalDepth/StepDown, operations generate empty toolpaths.
step_down = params.get("StepDown", params.get("step_down"))
final_depth = params.get("FinalDepth", params.get("final_depth"))
start_depth = params.get("StartDepth", params.get("start_depth"))
tool_diam = getattr(getattr(tc, "Tool", None), "Diameter", None)
tool_diam_val = float(tool_diam.Value) if hasattr(tool_diam, "Value") else (float(tool_diam) if tool_diam else 6.0)
if not step_down:
    step_down = round(tool_diam_val * 0.4, 2)
if (not step_down or not final_depth or not start_depth) and op_name in ("profile", "pocket"):
    stock = job.Stock if hasattr(job, "Stock") else None
    stock_zmin = None
    stock_zmax = None
    if stock is not None and hasattr(stock, "Shape") and hasattr(stock.Shape, "BoundBox"):
        stock_zmin = stock.Shape.BoundBox.ZMin
        stock_zmax = stock.Shape.BoundBox.ZMax
    if not final_depth:
        if op_name == "pocket":
            # Pocket: lowest Z among base feature faces
            lowest_z = stock_zmax  # fallback
            for feat in feature_refs:
                for f in feat:
                    if hasattr(f, "BoundBox"):
                        z = max(f.BoundBox.ZMin, 0)  # clamp above zero for faces
                        if hasattr(f, "CenterOfGravity"):
                            z = f.CenterOfGravity.z
                        if lowest_z is None or z < lowest_z:
                            lowest_z = z
            final_depth = lowest_z
        elif op_name == "profile":
            # Profile: from stock top to model bottom
            if stock_zmin is not None:
                final_depth = stock_zmin
    if not start_depth and stock_zmax is not None:
        start_depth = stock_zmax
    try:
        if hasattr(op, "StepDown") and step_down is not None:
            setattr(op, "StepDown", float(step_down))
    except Exception:
        pass
    try:
        if hasattr(op, "OpFinalDepth") and final_depth is not None:
            op.OpFinalDepth = float(final_depth)
    except Exception:
        pass
    try:
        if hasattr(op, "OpStartDepth") and start_depth is not None:
            op.OpStartDepth = float(start_depth)
    except Exception:
        pass

if hasattr(job, "Proxy") and hasattr(job.Proxy, "addOperation"):
    job.Proxy.addOperation(op)
doc.recompute()

succeed(
    f'{op_name.title()} operation created successfully.',
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_name": op.Name,
        "operation_label": op.Label,
        "operation_type": op_name,
        "tool_controller": tc.Name,
        "feature_count": len(feature_refs),
    },
)
"""
    return _run_cam_script(freecad, f"cam_add_{op_type}_operation", payload, body)


def add_drilling_locations_operation(
    freecad: Any,
    doc_name: str,
    job_name: str,
    locations: list[dict[str, Any]],
    tool_controller: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    params = params or {}
    _validate_optional_depths(params)
    if not locations:
        return error_response(ERROR_INVALID_INPUT, "locations must include at least one drilling target.")
    if tool_controller == "":
        return error_response(ERROR_INVALID_TOOL, "A tool controller name is required.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "locations": locations,
        "tool_controller": tool_controller,
        "params": params,
    }
    body = """
import FreeCAD as App

job = get_job(doc, request["job_name"])
tc = doc.getObject(request["tool_controller"])
if tc is None:
    fail("object_not_found", f'Tool controller "{request["tool_controller"]}" was not found.')

location_vectors = []
for item in request.get("locations") or []:
    if not isinstance(item, dict):
        fail("invalid_input", "Each drilling location must be a dictionary with x/y and optional z.")
    if "x" not in item or "y" not in item:
        fail("invalid_input", "Each drilling location requires x and y values.")
    location_vectors.append(
        App.Vector(float(item["x"]), float(item["y"]), float(item.get("z", 0.0)))
    )

params = request.get("params") or {}

def apply_overrides(op, params):
    expression_keys = {"StartDepth", "FinalDepth", "StepDown", "FinishDepth", "RetractHeight", "ClearanceHeight", "SafeHeight"}
    for key, value in params.items():
        if not hasattr(op, key):
            continue
        if key in expression_keys and hasattr(op, "setExpression"):
            try:
                op.setExpression(key, None)
            except Exception:
                pass
        try:
            setattr(op, key, value)
        except Exception:
            try:
                setattr(op, key, float(value))
            except Exception:
                pass

def bind_runtime_defaults(op, tc, job, params, op_name=None):
    if hasattr(op, "Active"):
        op.Active = True
    tool = getattr(tc, "Tool", None)
    if hasattr(op, "OpToolDiameter") and tool is not None and hasattr(tool, "Diameter"):
        try:
            op.OpToolDiameter = float(tool.Diameter)
        except Exception:
            pass
    sheet = getattr(job, "SetupSheet", None)
    for prop in ["SafeHeight", "ClearanceHeight"]:
        if hasattr(op, prop):
            explicit_value = params.get(prop)
            if explicit_value is not None:
                try:
                    setattr(op, prop, float(explicit_value))
                    continue
                except Exception:
                    pass
            if sheet is not None and hasattr(sheet, prop):
                try:
                    value = getattr(sheet, prop)
                    setattr(op, prop, float(value.Value) if hasattr(value, "Value") else float(value))
                except Exception:
                    pass
    if "StartDepth" in params and hasattr(op, "OpStartDepth"):
        try:
            op.OpStartDepth = float(params["StartDepth"])
        except Exception:
            pass
    if "FinalDepth" in params and hasattr(op, "OpFinalDepth"):
        try:
            op.OpFinalDepth = float(params["FinalDepth"])
        except Exception:
            pass

op_module = import_module_candidates(["Path.Op.Drilling"])
if not hasattr(op_module, "ObjectDrilling"):
    fail("cam_api_unavailable", "Path.Op.Drilling.ObjectDrilling is unavailable in the connected FreeCAD runtime.")

op_label = f'Drilling_{len(getattr(job.Operations, "Group", [])) + 1}'
op = doc.addObject("Path::FeaturePython", op_label)
if not hasattr(op, "DoNotSetDefaultValues"):
    op.addProperty(
        "App::PropertyBool",
        "DoNotSetDefaultValues",
        "CAM",
        "Skip FreeCAD default operation setup so MCP can bind tool/depths explicitly.",
    )
op.DoNotSetDefaultValues = True
proxy = op_module.ObjectDrilling(op, op_label, job)
op.Proxy = proxy
op.Label = op_label
op.ToolController = tc
op.Locations = location_vectors

if hasattr(op, "PeckEnabled") and "PeckEnabled" not in params:
    op.PeckEnabled = True
if hasattr(op, "PeckDepth") and "PeckDepth" not in params:
    op.PeckDepth = 2.0
if hasattr(op, "KeepToolDown") and "KeepToolDown" not in params:
    op.KeepToolDown = False

apply_overrides(op, params)
bind_runtime_defaults(op, tc, job, params, "drilling")
if hasattr(op, "DoNotSetDefaultValues"):
    op.DoNotSetDefaultValues = False

if hasattr(job, "Proxy") and hasattr(job.Proxy, "addOperation"):
    job.Proxy.addOperation(op)
doc.recompute()

path_obj = getattr(op, "Path", None)
commands = list(getattr(path_obj, "Commands", []) if path_obj else [])
cutting_count = 0
for command in commands:
    upper = str(getattr(command, "Name", "") or "").upper()
    if upper.startswith(("G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
        cutting_count += 1

succeed(
    "Drilling operation created from explicit locations.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_name": op.Name,
        "operation_label": op.Label,
        "tool_controller": tc.Name,
        "location_count": len(location_vectors),
        "command_count": len(commands),
        "cutting_command_count": cutting_count,
        "creation_mode": "locations_fallback",
    },
)
"""
    return _run_cam_script(freecad, "cam_add_drilling_locations_operation", payload, body)


def get_job_state(freecad: Any, doc_name: str, job_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "job_name": job_name}
    body = """
job = get_job(doc, request["job_name"])
operations = list(getattr(job.Operations, "Group", [])) if getattr(job, "Operations", None) else []
tool_controllers = list(getattr(getattr(job, "Tools", None), "Group", []) or [])

def quantity_value(value):
    if value is None:
        return None
    if hasattr(value, "Value"):
        return float(value.Value)
    try:
        return float(value)
    except Exception:
        return None

def classify_commands(commands):
    motion = 0
    cutting = 0
    command_names = []
    for command in commands:
        name = str(getattr(command, "Name", "") or "").upper()
        command_names.append(name)
        if name.startswith(("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
            motion += 1
        if name.startswith(("G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
            cutting += 1
    return motion, cutting, command_names

operation_states = []
for op in operations:
    path_obj = getattr(op, "Path", None)
    commands = list(getattr(path_obj, "Commands", []) if path_obj else [])
    motion_count, cutting_count, command_names = classify_commands(commands)
    operation_states.append(
        {
            "name": op.Name,
            "label": op.Label,
            "type_id": getattr(op, "TypeId", None),
            "tool_controller": getattr(getattr(op, "ToolController", None), "Name", None),
            "command_count": len(commands),
            "motion_command_count": motion_count,
            "cutting_command_count": cutting_count,
            "first_commands": command_names[:10],
            "has_base": bool(getattr(op, "Base", []) if hasattr(op, "Base") else False),
            "final_depth_mm": quantity_value(getattr(op, "FinalDepth", None)),
            "start_depth_mm": quantity_value(getattr(op, "StartDepth", None)),
            "state": (
                "empty"
                if len(commands) == 0
                else "non_cutting"
                if cutting_count == 0
                else "ready"
            ),
        }
    )

succeed(
    "Collected CAM job state.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "tool_controller_names": [tc.Name for tc in tool_controllers],
        "operation_states": operation_states,
        "operation_count": len(operation_states),
    },
)
"""
    return _run_cam_script(freecad, "cam_get_job_state", payload, body)


def get_tool_controller_details(
    freecad: Any,
    doc_name: str,
    job_name: str,
    tool_controller_name: str | None = None,
) -> dict[str, Any]:
    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "tool_controller_name": tool_controller_name,
    }
    body = """
job = get_job(doc, request["job_name"])
controllers = list(getattr(getattr(job, "Tools", None), "Group", []) or [])
requested_name = request.get("tool_controller_name")
if requested_name:
    controllers = [tc for tc in controllers if getattr(tc, "Name", "") == requested_name]
    if not controllers:
        fail("object_not_found", f'Tool controller "{requested_name}" was not found in job "{job.Name}".')

def quantity_value(value):
    if value is None:
        return None
    if hasattr(value, "Value"):
        return float(value.Value)
    try:
        return float(value)
    except Exception:
        return None

details = []
for tc in controllers:
    tool = getattr(tc, "Tool", None)
    horiz_feed_internal = quantity_value(getattr(tc, "HorizFeed", None))
    vert_feed_internal = quantity_value(getattr(tc, "VertFeed", None))
    details.append(
        {
            "name": tc.Name,
            "label": tc.Label,
            "tool_number": getattr(tc, "ToolNumber", None),
            "spindle_rpm": quantity_value(getattr(tc, "SpindleSpeed", None)),
            "feed_rate_internal_mm_s": horiz_feed_internal,
            "plunge_rate_internal_mm_s": vert_feed_internal,
            "feed_rate_mm_min": round(horiz_feed_internal * 60.0, 6) if horiz_feed_internal is not None else None,
            "plunge_rate_mm_min": round(vert_feed_internal * 60.0, 6) if vert_feed_internal is not None else None,
            "tool_label": getattr(tool, "Label", None) if tool is not None else None,
            "tool_diameter_mm": quantity_value(getattr(tool, "Diameter", None)) if tool is not None else None,
            "spindle_direction": getattr(tc, "SpindleDir", None),
        }
    )

succeed(
    "Collected tool controller details.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "tool_controller_name": requested_name,
        "tool_controller_details": details,
        "tool_controller_count": len(details),
    },
)
"""
    return _run_cam_script(freecad, "cam_get_tool_controller_details", payload, body)


def get_operation_path_details(freecad: Any, doc_name: str, job_name: str, operation_name: str) -> dict[str, Any]:
    if not operation_name:
        return error_response(ERROR_INVALID_INPUT, "operation_name is required.")

    payload = {"doc_name": doc_name, "job_name": job_name, "operation_name": operation_name}
    body = """
job = get_job(doc, request["job_name"])
op = doc.getObject(request["operation_name"])
if op is None:
    fail("object_not_found", f'Operation "{request["operation_name"]}" was not found.')

path_obj = getattr(op, "Path", None)
commands = list(getattr(path_obj, "Commands", []) if path_obj else [])
command_details = []
motion_command_count = 0
cutting_command_count = 0
for command in commands:
    name = str(getattr(command, "Name", "") or "")
    upper = name.upper()
    params = dict(getattr(command, "Parameters", {}) or {})
    if upper.startswith(("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
        motion_command_count += 1
    if upper.startswith(("G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
        cutting_command_count += 1
    command_details.append({"name": name, "params": params})

succeed(
    "Collected CAM operation path details.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_name": op.Name,
        "operation_label": op.Label,
        "tool_controller": getattr(getattr(op, "ToolController", None), "Name", None),
        "command_count": len(commands),
        "motion_command_count": motion_command_count,
        "cutting_command_count": cutting_command_count,
        "commands": command_details,
    },
)
"""
    return _run_cam_script(freecad, "cam_get_operation_path_details", payload, body)


def get_remote_gcode(freecad: Any, gcode_path: str) -> dict[str, Any]:
    if not gcode_path:
        return error_response(ERROR_INVALID_INPUT, "gcode_path is required to inspect remote G-code.")

    script = dedent(
        f"""
import json
path = {gcode_path!r}
with open(path, "r", encoding="utf-8") as handle:
    content = handle.read()
print(json.dumps({{"path": path, "lines": content.splitlines(), "text": content}}, ensure_ascii=False))
"""
    )
    response = freecad.execute_code(script)
    if not response.get("success"):
        return error_response(
            ERROR_INVALID_INPUT,
            f'Unable to read remote G-code file "{gcode_path}".',
            detail=response.get("error", ""),
            data={"raw_response": response},
        )

    message = response.get("message", "")
    marker = "Output: "
    idx = message.find(marker)
    if idx == -1:
        return error_response(
            ERROR_FREECAD_RESPONSE,
            "Remote FreeCAD response did not include G-code text output.",
            data={"raw_response": response},
        )

    payload_text = message[idx + len(marker):].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return error_response(
            ERROR_FREECAD_RESPONSE,
            "Failed to decode remote G-code text from FreeCAD response.",
            detail=str(exc),
            data={"raw_response": response, "payload_text": payload_text},
        )

    return success_response(
        "Loaded remote G-code text.",
        data={
            "gcode_path": payload.get("path", gcode_path),
            "lines": payload.get("lines", []),
            "text": payload.get("text", ""),
            "line_count": len(payload.get("lines", [])),
        },
    )


def reverse_verify_job_output(freecad: Any, doc_name: str, job_name: str, gcode_path: str) -> dict[str, Any]:
    if not gcode_path:
        return error_response(ERROR_INVALID_INPUT, "gcode_path is required for reverse verification.")

    job_state = get_job_state(freecad, doc_name, job_name)
    if not job_state.get("success"):
        return job_state

    gcode_payload = get_remote_gcode(freecad, gcode_path)
    if not gcode_payload.get("success"):
        return gcode_payload

    lines = [str(line) for line in gcode_payload["data"].get("lines", [])]
    segments: dict[str, dict[str, Any]] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("(Begin operation: "):
            if current_name is not None:
                segments[current_name] = {"lines": current_lines}
            current_name = stripped[len("(Begin operation: ") : -1] if stripped.endswith(")") else stripped
            current_lines = []
            continue
        if stripped.startswith("(Finish operation: "):
            if current_name is not None:
                segments[current_name] = {"lines": current_lines}
                current_name = None
                current_lines = []
            continue
        if current_name is not None:
            current_lines.append(stripped)

    if current_name is not None:
        segments[current_name] = {"lines": current_lines}

    suspicious_operations = []
    operation_reports = []
    for operation in job_state["data"].get("operation_states", []):
        name = operation["name"]
        segment = segments.get(name, {"lines": []})
        segment_lines = segment["lines"]
        motion_count = 0
        cutting_count = 0
        for line in segment_lines:
            upper = line.upper()
            if upper.startswith(("G0 ", "G00 ", "G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 ", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
                motion_count += 1
            if upper.startswith(("G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 ", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
                cutting_count += 1
        report = {
            "operation_name": name,
            "job_state": operation["state"],
            "job_command_count": operation["command_count"],
            "gcode_motion_count": motion_count,
            "gcode_cutting_count": cutting_count,
            "gcode_excerpt": segment_lines[:12],
        }
        if operation["state"] != "ready" or cutting_count == 0:
            suspicious_operations.append(report)
        operation_reports.append(report)

    return success_response(
        "Completed CAM reverse verification against generated G-code.",
        data={
            "doc_name": doc_name,
            "job_name": job_name,
            "gcode_path": gcode_path,
            "operation_reports": operation_reports,
            "suspicious_operations": suspicious_operations,
            "has_suspicious_operations": len(suspicious_operations) > 0,
        },
    )


_GCODE_AXIS_RE = re.compile(r"([XYZFIJQR])\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)


def _split_gcode_operation_segments(lines: list[str]) -> dict[str, list[str]]:
    segments: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("(Begin operation: "):
            if current_name is not None:
                segments[current_name] = current_lines
            current_name = stripped[len("(Begin operation: ") : -1] if stripped.endswith(")") else stripped
            current_lines = []
            continue
        if stripped.startswith("(Finish operation: "):
            if current_name is not None:
                segments[current_name] = current_lines
                current_name = None
                current_lines = []
            continue
        if current_name is not None:
            current_lines.append(stripped)
    if current_name is not None:
        segments[current_name] = current_lines
    return segments


def _gcode_operation_order(lines: list[str]) -> list[str]:
    order: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("(Begin operation: "):
            order.append(stripped[len("(Begin operation: ") : -1] if stripped.endswith(")") else stripped)
    return order


def _parse_axis_words(line: str) -> dict[str, float]:
    return {axis.upper(): float(value) for axis, value in _GCODE_AXIS_RE.findall(line)}


def _segment_extents(segment_lines: list[str]) -> dict[str, Any]:
    modal = {"X": None, "Y": None, "Z": None}
    motion_points: list[dict[str, float]] = []
    cutting_points: list[dict[str, float]] = []
    drill_points: list[dict[str, float]] = []
    cycle_points: list[dict[str, float]] = []

    for raw_line in segment_lines:
        line = raw_line.strip()
        if not line:
            continue
        is_comment = line.startswith("(") or line.startswith(";")
        upper = line.upper()
        words = _parse_axis_words(line)
        if any(axis in words for axis in ("X", "Y", "Z")) and not is_comment:
            for axis in ("X", "Y", "Z"):
                if axis in words:
                    modal[axis] = words[axis]
        is_motion = (not is_comment) and upper.startswith(("G0 ", "G00 ", "G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 "))
        is_cutting = (not is_comment) and upper.startswith(("G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 "))
        if is_motion:
            point = {axis: modal[axis] for axis in ("X", "Y", "Z") if modal[axis] is not None}
            if point:
                motion_points.append(point)
                if is_cutting:
                    cutting_points.append(point)
                    if "Z" in words and words["Z"] < 0 and modal.get("X") is not None and modal.get("Y") is not None:
                        drill_points.append({"X": float(modal["X"]), "Y": float(modal["Y"]), "Z": words["Z"]})
        if "G81" in upper or "G82" in upper or "G83" in upper:
            # Some posts leave cycles as comments; still parse them for verification.
            if "X" in words and "Y" in words:
                point = {"X": words["X"], "Y": words["Y"]}
                if "Z" in words:
                    point["Z"] = words["Z"]
                cycle_points.append(point)

    def extent(points: list[dict[str, float]], axis: str) -> dict[str, float | None]:
        values = [p[axis] for p in points if axis in p]
        return {"min": min(values), "max": max(values)} if values else {"min": None, "max": None}

    return {
        "motion_count": len(motion_points),
        "cutting_count": len(cutting_points),
        "motion_extents": {axis: extent(motion_points, axis) for axis in ("X", "Y", "Z")},
        "cutting_extents": {axis: extent(cutting_points, axis) for axis in ("X", "Y", "Z")},
        "drill_points": cycle_points or drill_points,
        "cycle_points": cycle_points,
        "expanded_peck_points": drill_points,
    }


def _close_enough(value: float | None, expected: float, tolerance: float) -> bool:
    return value is not None and abs(float(value) - float(expected)) <= float(tolerance)


def _range_close(actual: dict[str, float | None], expected: list[float], tolerance: float) -> bool:
    return _close_enough(actual.get("min"), float(expected[0]), tolerance) and _close_enough(actual.get("max"), float(expected[1]), tolerance)


def verify_gcode_against_targets(freecad: Any, gcode_path: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify generated G-code cuts the requested target geometry, not merely any non-empty path."""
    if not gcode_path:
        return error_response(ERROR_INVALID_INPUT, "gcode_path is required for target verification.")
    if not targets:
        return error_response(ERROR_INVALID_INPUT, "targets must include at least one expected operation target.")
    expected_schema = {
        "rect_pocket/profile": {
            "operation": "Pocket_3",
            "kind": "rect_pocket",
            "expected_x": [-50, 50],
            "expected_y": [-20, 20],
            "expected_z": -2,
            "tool_diameter": 5,
            "require_negative_z": True,
        },
        "drilling": {
            "operation": "Drilling_5",
            "kind": "drilling",
            "expected_points": [[-70, -15], [-70, 15], [70, -15], [70, 15]],
            "expected_z": -15,
            "tool_diameter": 6,
        },
    }
    invalid_targets: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            invalid_targets.append({"index": index, "error": "target must be a dictionary"})
            continue
        if not target.get("operation"):
            invalid_targets.append({"index": index, "error": 'missing required key "operation"', "target": target})
        if not target.get("kind"):
            invalid_targets.append({"index": index, "error": 'missing required key "kind"', "target": target})
    if invalid_targets:
        return error_response(
            ERROR_INVALID_INPUT,
            "targets use an invalid schema; each target requires operation and kind.",
            data={"invalid_targets": invalid_targets, "expected_schema": expected_schema},
        )

    gcode_payload = get_remote_gcode(freecad, gcode_path)
    if not gcode_payload.get("success"):
        return gcode_payload

    lines = [str(line) for line in gcode_payload["data"].get("lines", [])]
    segments = _split_gcode_operation_segments(lines)
    operation_order = _gcode_operation_order(lines)
    reports: list[dict[str, Any]] = []
    errors: list[str] = []

    target_order = [str(target.get("operation")) for target in targets if target.get("operation")]
    if target_order and all(bool(target.get("enforce_order", True)) for target in targets):
        positions = [operation_order.index(name) if name in operation_order else None for name in target_order]
        if None not in positions and positions != sorted(positions):
            errors.append(f"G-code operation order {operation_order} does not match expected target order {target_order}.")

    for target in targets:
        operation = str(target.get("operation") or "")
        kind = str(target.get("kind") or "").lower()
        segment_lines = segments.get(operation)
        if segment_lines is None:
            report = {"operation": operation, "kind": kind, "passed": False, "errors": [f'Operation "{operation}" not found in G-code.']}
            reports.append(report)
            errors.extend(report["errors"])
            continue

        extents = _segment_extents(segment_lines)
        tolerance = float(target.get("tolerance_mm", 0.75))
        report_errors: list[str] = []
        report: dict[str, Any] = {
            "operation": operation,
            "kind": kind,
            "passed": True,
            "motion_count": extents["motion_count"],
            "cutting_count": extents["cutting_count"],
            "cutting_extents": extents["cutting_extents"],
        }

        if kind in {"rect_pocket", "pocket", "profile"}:
            tool_diameter = float(target.get("tool_diameter", target.get("tool_diameter_mm", 0.0)) or 0.0)
            radius = tool_diameter / 2.0 if tool_diameter > 0 else 0.0
            expected_x = target.get("expected_x") or target.get("x")
            expected_y = target.get("expected_y") or target.get("y")
            if expected_x is not None:
                expected = [float(expected_x[0]), float(expected_x[1])]
                if kind in {"rect_pocket", "pocket"} and radius:
                    expected = [expected[0] + radius, expected[1] - radius]
                elif kind == "profile" and radius and str(target.get("profile_side", target.get("side", "outside"))).lower() == "outside":
                    expected = [expected[0] - radius, expected[1] + radius]
                elif kind == "profile" and radius and str(target.get("profile_side", target.get("side", ""))).lower() == "inside":
                    expected = [expected[0] + radius, expected[1] - radius]
                report["expected_cutting_x"] = expected
                if not _range_close(extents["cutting_extents"]["X"], expected, tolerance):
                    report_errors.append(
                        f'{operation} X cutting extent {extents["cutting_extents"]["X"]} does not match expected {expected}.'
                    )
            if expected_y is not None:
                expected = [float(expected_y[0]), float(expected_y[1])]
                if kind in {"rect_pocket", "pocket"} and radius:
                    expected = [expected[0] + radius, expected[1] - radius]
                elif kind == "profile" and radius and str(target.get("profile_side", target.get("side", "outside"))).lower() == "outside":
                    expected = [expected[0] - radius, expected[1] + radius]
                elif kind == "profile" and radius and str(target.get("profile_side", target.get("side", ""))).lower() == "inside":
                    expected = [expected[0] + radius, expected[1] - radius]
                report["expected_cutting_y"] = expected
                if not _range_close(extents["cutting_extents"]["Y"], expected, tolerance):
                    report_errors.append(
                        f'{operation} Y cutting extent {extents["cutting_extents"]["Y"]} does not match expected {expected}.'
                    )
            expected_z = target.get("expected_z")
            if expected_z is not None:
                expected_z = float(expected_z)
                report["expected_cutting_z_min"] = expected_z
                actual_z_min = extents["cutting_extents"]["Z"].get("min")
                if not _close_enough(actual_z_min, expected_z, float(target.get("z_tolerance_mm", tolerance))):
                    report_errors.append(f'{operation} min cutting Z {actual_z_min} does not match expected {expected_z}.')
                if bool(target.get("require_negative_z", False)) and actual_z_min is not None and actual_z_min >= 0:
                    report_errors.append(f"{operation} cuts at non-negative Z ({actual_z_min}) but top-zero WCS expects negative cutting Z.")

        elif kind == "drilling":
            expected_points = target.get("expected_points") or []
            expected_z = target.get("expected_z")
            actual_points = extents["drill_points"]
            report["expected_points"] = expected_points
            report["drill_points"] = actual_points
            for point in expected_points:
                px = float(point[0] if isinstance(point, (list, tuple)) else point.get("x"))
                py = float(point[1] if isinstance(point, (list, tuple)) else point.get("y"))
                if not any(_close_enough(p.get("X"), px, tolerance) and _close_enough(p.get("Y"), py, tolerance) for p in actual_points):
                    report_errors.append(f"{operation} missing drilling point ({px}, {py}).")
            if expected_z is not None:
                expected_z = float(expected_z)
                z_values = [float(p["Z"]) for p in actual_points if "Z" in p]
                report["expected_drill_z"] = expected_z
                report["actual_drill_z_min"] = min(z_values) if z_values else None
                if not z_values or not any(abs(z - expected_z) <= float(target.get("z_tolerance_mm", tolerance)) for z in z_values):
                    report_errors.append(f"{operation} does not reach expected drill Z {expected_z}.")

        else:
            report_errors.append(f'Unsupported target kind "{kind}" for {operation}.')

        if report_errors:
            report["passed"] = False
            report["errors"] = report_errors
            errors.extend(report_errors)
        reports.append(report)

    data = {
        "gcode_path": gcode_path,
        "operation_order": operation_order,
        "target_reports": reports,
        "errors": errors,
        "passed": not errors,
    }
    if errors:
        return error_response(ERROR_GCODE_LINT_FAILED, "G-code target verification failed.", detail="; ".join(errors), data=data)
    return success_response("G-code target verification passed.", data=data)


def cleanup_suspicious_operations(
    freecad: Any,
    doc_name: str,
    job_name: str,
    gcode_path: str,
    remove_all: bool = True,
) -> dict[str, Any]:
    reverse_payload = reverse_verify_job_output(freecad, doc_name, job_name, gcode_path)
    if not reverse_payload.get("success"):
        return reverse_payload

    suspicious_operations = list(reverse_payload["data"].get("suspicious_operations", []))
    if not suspicious_operations:
        return success_response(
            "No suspicious CAM operations required cleanup.",
            data={
                "doc_name": doc_name,
                "job_name": job_name,
                "gcode_path": gcode_path,
                "removed_operations": [],
                "skipped_operations": [],
                "has_suspicious_operations": False,
            },
        )

    removed_operations = []
    skipped_operations = []
    removal_errors = []
    for operation in suspicious_operations:
        should_remove = bool(remove_all) or operation.get("job_state") in {"empty", "non_cutting"}
        if not should_remove:
            skipped_operations.append(operation)
            continue
        operation_name = operation.get("operation_name")
        removal = remove_operation(freecad, doc_name, job_name, operation_name)
        if removal.get("success"):
            removed_operations.append(
                {
                    "operation_name": operation_name,
                    "reason": (
                        "Reverse verification reported no usable cutting motion."
                        if operation.get("gcode_cutting_count", 0) == 0
                        else f'Job state was {operation.get("job_state")}.'
                    ),
                }
            )
            continue
        removal_errors.append(
            {
                "operation_name": operation_name,
                "error_code": removal.get("error_code"),
                "error_detail": removal.get("error_detail"),
            }
        )

    if removal_errors:
        return error_response(
            ERROR_FREECAD_EXECUTION,
            "Failed to remove one or more suspicious CAM operations.",
            data={
                "doc_name": doc_name,
                "job_name": job_name,
                "gcode_path": gcode_path,
                "removed_operations": removed_operations,
                "skipped_operations": skipped_operations,
                "removal_errors": removal_errors,
                "suspicious_operations": suspicious_operations,
            },
        )

    recompute_payload = recompute_job(freecad, doc_name, job_name)
    if not recompute_payload.get("success"):
        return recompute_payload

    return success_response(
        "Removed suspicious CAM operations after reverse verification.",
        data={
            "doc_name": doc_name,
            "job_name": job_name,
            "gcode_path": gcode_path,
            "removed_operations": removed_operations,
            "skipped_operations": skipped_operations,
            "suspicious_operations": suspicious_operations,
            "has_suspicious_operations": len(suspicious_operations) > 0,
        },
    )


def remove_operation(freecad: Any, doc_name: str, job_name: str, operation_name: str) -> dict[str, Any]:
    if not operation_name:
        return error_response(ERROR_INVALID_INPUT, "operation_name is required.")

    payload = {"doc_name": doc_name, "job_name": job_name, "operation_name": operation_name}
    body = """
job = get_job(doc, request["job_name"])
op = doc.getObject(request["operation_name"])
if op is None:
    fail("object_not_found", f'Operation "{request["operation_name"]}" was not found.')

removed_from_job = False
if hasattr(job, "Proxy") and hasattr(job.Proxy, "removeObject"):
    try:
        job.Proxy.removeObject(op)
        removed_from_job = True
    except Exception:
        pass

try:
    doc.removeObject(op.Name)
except Exception as exc:
    fail("freecad_execution_error", f'Failed to remove operation "{op.Name}".', detail=str(exc))

doc.recompute()
succeed(
    "CAM operation removed successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_name": request["operation_name"],
        "removed_from_job_proxy": removed_from_job,
    },
)
"""
    return _run_cam_script(freecad, "cam_remove_operation", payload, body)


def probe_runtime_capabilities(freecad: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    body = """
version_parts = []
try:
    version_parts = list(App.Version())
except Exception:
    pass

module_map = {
    "job": "Path.Main.Job",
    "stock": "Path.Main.Stock",
    "tool_controller": "Path.Tool.Controller",
    "profile": "Path.Op.Profile",
    "pocket": "Path.Op.Pocket",
    "drilling": "Path.Op.Drilling",
    "face": "Path.Op.MillFace",
    "post_processor": "Path.Post.Processor",
}

module_status = {}
for key, module_name in module_map.items():
    try:
        module = importlib.import_module(module_name)
        module_status[key] = {
            "module_name": module_name,
            "imported": True,
            "has_create": hasattr(module, "Create"),
        }
    except Exception as exc:
        module_status[key] = {
            "module_name": module_name,
            "imported": False,
            "has_create": False,
            "error": str(exc),
        }

factory_status = {
    "available": False,
    "supported_posts": [],
}
try:
    processor_module = importlib.import_module("Path.Post.Processor")
    factory = getattr(processor_module, "PostProcessorFactory", None)
    if factory is not None:
        factory_status["available"] = True
        factory_status["supported_posts"] = [post for post in ["grbl", "linuxcnc"]]
except Exception:
    pass

runtime_hints = []
drilling_status = module_status.get("drilling", {})
if drilling_status.get("imported") and drilling_status.get("has_create"):
    runtime_hints.append(
        "Drilling module is importable, but FreeCAD 1.0 headless may still fail during Create() before model context is fully bound."
    )

succeed(
    "Probed FreeCAD CAM runtime capabilities.",
    {
        "freecad_version": version_parts,
        "active_document": getattr(getattr(App, "ActiveDocument", None), "Name", None),
        "module_status": module_status,
        "post_processor_status": factory_status,
        "supported_posts": ["grbl", "linuxcnc"],
        "headless_assumption": True,
        "runtime_hints": runtime_hints,
    },
)
"""
    return _run_cam_script(freecad, "cam_probe_runtime_capabilities", payload, body)


def recompute_job(freecad: Any, doc_name: str, job_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "job_name": job_name}
    body = """
job = get_job(doc, request["job_name"])
doc.recompute()
operations = list(getattr(job.Operations, "Group", [])) if getattr(job, "Operations", None) else []
succeed(
    "CAM job recomputed successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_count": len(operations),
        "operations": [op.Name for op in operations],
    },
)
"""
    return _run_cam_script(freecad, "cam_recompute_job", payload, body)


def validate_job(freecad: Any, doc_name: str, job_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "job_name": job_name}
    body = """
job = get_job(doc, request["job_name"])
operations = list(getattr(job.Operations, "Group", [])) if getattr(job, "Operations", None) else []
tool_controllers = list(getattr(job.Tools, "Group", [])) if getattr(job, "Tools", None) else []
warnings = []
errors = []

def quantity_value(value):
    if value is None:
        return None
    if hasattr(value, "Value"):
        return float(value.Value)
    try:
        return float(value)
    except Exception:
        return None

def min_width_for_refs(refs):
    widths = []
    depths = []
    for entry in refs or []:
        obj = entry[0] if isinstance(entry, (tuple, list)) and entry else None
        if obj is None:
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        bb = shape.BoundBox
        widths.extend([float(bb.XLength), float(bb.YLength)])
        depths.append(float(bb.ZLength))
    return (min(widths) if widths else None, max(depths) if depths else None)

if not operations:
    errors.append({"code": "empty_toolpath", "message": "Job has no operations."})
if not tool_controllers:
    errors.append({"code": "invalid_tool", "message": "Job has no tool controllers."})

zombie_tcs = []
for tc in tool_controllers[:]:
    spindle = getattr(tc, "SpindleSpeed", 0)
    horiz = getattr(tc, "HorizFeed", 0)
    vert = getattr(tc, "VertFeed", 0)
    if spindle <= 0 and horiz <= 0 and vert <= 0:
        try:
            doc.removeObject(tc.Name)
            tool_controllers.remove(tc)
            zombie_tcs.append(tc.Name)
        except Exception:
            pass
        continue
    if spindle <= 0:
        errors.append({"code": "invalid_tool", "message": f"Tool controller {tc.Name} has non-positive spindle speed."})
    if horiz <= 0 or vert <= 0:
        errors.append({"code": "invalid_tool", "message": f"Tool controller {tc.Name} has non-positive feed values."})
if zombie_tcs:
    warnings.append(f"Auto-removed zombie tool controller(s): {', '.join(zombie_tcs)} (spindle=0, feed=0).")

sheet = getattr(job, "SetupSheet", None)
if sheet is not None and hasattr(sheet, "SafeHeight") and hasattr(sheet, "ClearanceHeight"):
    try:
        safe_height = quantity_value(getattr(sheet, "SafeHeight", None))
        clearance_height = quantity_value(getattr(sheet, "ClearanceHeight", None))
        if safe_height is not None and safe_height < 1.0:
            errors.append({"code": "safe_height_low", "message": "SafeHeight is too low for a conservative retract."})
        if safe_height is not None and clearance_height is not None and safe_height > clearance_height:
            errors.append({"code": "invalid_height", "message": "SafeHeight exceeds ClearanceHeight."})
    except Exception:
        warnings.append("Could not compare SafeHeight and ClearanceHeight values.")

for op in operations:
    if hasattr(op, "ToolController") and getattr(op, "ToolController", None) is None:
        errors.append({"code": "invalid_tool", "message": f"Operation {op.Name} has no tool controller."})
    path_obj = getattr(op, "Path", None)
    commands = list(getattr(path_obj, "Commands", [])) if path_obj else []
    command_count = len(commands)
    motion_command_count = 0
    cutting_command_count = 0
    for command in commands:
        name = str(getattr(command, "Name", "") or "").upper()
        if name.startswith(("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
            motion_command_count += 1
        if name.startswith(("G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
            cutting_command_count += 1
    if command_count == 0:
        errors.append({"code": "empty_toolpath", "message": f"Operation {op.Name} has no generated path commands."})
    elif cutting_command_count == 0:
        errors.append(
            {
                "code": "empty_toolpath",
                "message": f"Operation {op.Name} contains setup/retract commands only and no cutting motion.",
            }
        )
    if hasattr(op, "Base") and len(getattr(op, "Base", [])) == 0:
        if command_count > 0:
            warnings.append(
                {
                    "code": "base_not_reported",
                    "message": (
                        f"Operation {op.Name} does not expose Base geometry in the connected FreeCAD "
                        "runtime, but it already contains path commands."
                    ),
                }
            )
        else:
            errors.append({"code": "invalid_input", "message": f"Operation {op.Name} has no base geometry."})
    base_refs = getattr(op, "Base", [])
    feature_min_width, feature_depth = min_width_for_refs(base_refs)
    tc = getattr(op, "ToolController", None)
    tool_diameter = None
    if tc is not None:
        tool = getattr(tc, "Tool", None)
        tool_diameter = quantity_value(getattr(tool, "Diameter", None)) if tool is not None else None
    if feature_min_width is not None and tool_diameter is not None and tool_diameter >= feature_min_width:
        errors.append(
            {
                "code": "tool_diameter_too_large",
                "message": f"Operation {op.Name} uses tool diameter {tool_diameter} mm against minimum feature width {feature_min_width} mm.",
            }
        )
    final_depth = quantity_value(getattr(op, "FinalDepth", None))
    start_depth = quantity_value(getattr(op, "StartDepth", None))
    if start_depth is not None and final_depth is not None and final_depth > start_depth:
        errors.append({"code": "invalid_depth", "message": f"Operation {op.Name} has FinalDepth above StartDepth."})
    if feature_depth is not None and final_depth is not None and abs(final_depth) > feature_depth + 2.0:
        errors.append(
            {
                "code": "final_depth_out_of_bounds",
                "message": f"Operation {op.Name} final depth {final_depth} mm exceeds feature depth envelope {feature_depth} mm.",
            }
        )
    if feature_min_width is not None and tool_diameter is not None and feature_min_width - tool_diameter < 0.5:
        warnings.append(
            f"Operation {op.Name} has limited radial clearance; consider a smaller cutter or different entry mode."
        )

if errors:
    fail(
        errors[0]["code"],
        "CAM job validation failed.",
        data={
            "doc_name": request["doc_name"],
            "job_name": job.Name,
            "errors": errors,
            "warnings": warnings,
            "operation_count": len(operations),
            "operation_states": [
                {
                    "name": op.Name,
                    "command_count": len(list(getattr(getattr(op, "Path", None), "Commands", [])) if getattr(op, "Path", None) else []),
                }
                for op in operations
            ],
        },
        warnings=warnings,
    )

succeed(
    "CAM job validation passed.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_count": len(operations),
        "tool_controller_count": len(tool_controllers),
    },
    warnings=warnings,
)
"""
    return _run_cam_script(freecad, "cam_validate_job", payload, body)


def postprocess_job(
    freecad: Any,
    doc_name: str,
    job_name: str,
    post_name: str,
    output_path: str,
) -> dict[str, Any]:
    if post_name not in SUPPORTED_POSTS:
        return error_response(ERROR_UNSUPPORTED_POST, f'Unsupported post processor "{post_name}".', data={"supported_posts": list(SUPPORTED_POSTS)})
    if not output_path:
        return error_response(ERROR_INVALID_INPUT, "An output_path is required for CAM postprocessing.")

    payload = {
        "doc_name": doc_name,
        "job_name": job_name,
        "post_name": post_name,
        "output_path": output_path,
    }
    body = """
import os

job = get_job(doc, request["job_name"])
job.PostProcessor = request["post_name"]
job.PostProcessorOutputFile = request["output_path"]
existing_args = getattr(job, "PostProcessorArgs", "") or ""
if "--no-show-editor" not in existing_args:
    job.PostProcessorArgs = (existing_args + " --no-show-editor").strip()
doc.recompute()

processor_module = import_module_candidates(["Path.Post.Processor"])
factory = getattr(processor_module, "PostProcessorFactory", None)
if factory is None:
    fail("cam_api_unavailable", "Path.Post.Processor.PostProcessorFactory is unavailable.")

processor = factory.get_post_processor(job, request["post_name"])
if processor is None:
    fail("unsupported_post", f'Could not load post processor "{request["post_name"]}".')
if hasattr(processor, "_dialog_handled"):
    processor._dialog_handled = True

sections = processor.export()
if not sections:
    fail("empty_toolpath", "Post processor returned no G-code sections.")

lines = []
for _, section in sections:
    if section:
        lines.append(section)
gcode = "\\n".join(lines).strip()
if not gcode:
    fail("empty_toolpath", "Post processor returned empty G-code output.")

wcs_origin = None
if all(hasattr(job, prop) for prop in ("AgentCadWcsOriginX", "AgentCadWcsOriginY", "AgentCadWcsOriginZ")):
    try:
        wcs_origin = {
            "X": float(job.AgentCadWcsOriginX),
            "Y": float(job.AgentCadWcsOriginY),
            "Z": float(job.AgentCadWcsOriginZ),
        }
    except Exception:
        wcs_origin = None

axis_pattern = re.compile(r"([XYZ])\\s*(-?\\d+(?:\\.\\d+)?)", re.IGNORECASE)
def translate_wcs_line(line):
    if wcs_origin is None:
        return line
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("(", ";")):
        return line
    upper = stripped.upper()
    if not upper.startswith(("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03", "G81", "G82", "G83", "G84", "G85", "G86", "G87", "G88", "G89")):
        return line
    def replace_axis(match):
        axis = match.group(1).upper()
        value = float(match.group(2)) - wcs_origin[axis]
        return f"{axis}{value:.4f}".rstrip("0").rstrip(".")
    return axis_pattern.sub(replace_axis, line)

if wcs_origin is not None:
    gcode = "\\n".join(translate_wcs_line(line) for line in gcode.splitlines()).strip()

motion_lines = []
for line in gcode.splitlines():
    stripped = line.strip().upper()
    if not stripped or stripped.startswith(("(", ";")):
        continue
    if stripped.startswith(("G0 ", "G00 ", "G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 ")):
        motion_lines.append(line)

if not motion_lines:
    fail("empty_toolpath", "Post processor output contains no actual motion commands.")

output_path = request["output_path"]
os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
with open(output_path, "w", encoding="utf-8") as handle:
    handle.write(gcode)

succeed(
    "CAM postprocess completed successfully.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "post_name": request["post_name"],
        "gcode_path": output_path,
        "line_count": len(gcode.splitlines()),
        "motion_line_count": len(motion_lines),
        "wcs_transform_applied": wcs_origin is not None,
        "wcs_origin_model_mm": wcs_origin,
        "gcode_preview": gcode.splitlines()[:20],
    },
)
"""
    return _run_cam_script(freecad, "cam_postprocess_job", payload, body)


def get_gcode_preview(freecad: Any, doc_name: str, job_name: str) -> dict[str, Any]:
    payload = {"doc_name": doc_name, "job_name": job_name}
    body = """
job = get_job(doc, request["job_name"])
operations = list(getattr(job.Operations, "Group", [])) if getattr(job, "Operations", None) else []
preview = []
def quantity_value(value):
    if value is None:
        return None
    if hasattr(value, "Value"):
        return float(value.Value)
    try:
        return float(value)
    except Exception:
        return None

for op in operations:
    path_obj = getattr(op, "Path", None)
    commands = list(getattr(path_obj, "Commands", [])) if path_obj else []
    command_count = len(commands)
    path_length = 0.0
    z_values = []
    last_xyz = None
    for command in commands:
        params = getattr(command, "Parameters", {}) or {}
        current_xyz = list(last_xyz) if last_xyz is not None else [None, None, None]
        for index, axis in enumerate(["X", "Y", "Z"]):
            if axis in params:
                try:
                    current_xyz[index] = float(params[axis])
                except Exception:
                    pass
        if current_xyz[2] is not None:
            z_values.append(current_xyz[2])
        if last_xyz is not None and None not in current_xyz and None not in last_xyz:
            dx = current_xyz[0] - last_xyz[0]
            dy = current_xyz[1] - last_xyz[1]
            dz = current_xyz[2] - last_xyz[2]
            path_length += (dx * dx + dy * dy + dz * dz) ** 0.5
        if None not in current_xyz:
            last_xyz = tuple(current_xyz)

    feed_rate_internal = quantity_value(getattr(getattr(op, "ToolController", None), "HorizFeed", None))
    feed_rate_mm_min = (feed_rate_internal * 60.0) if feed_rate_internal else None
    estimated_time_min = (path_length / feed_rate_mm_min) if path_length and feed_rate_mm_min else None
    preview.append(
        {
            "name": op.Name,
            "label": op.Label,
            "command_count": command_count,
            "tool_controller": getattr(getattr(op, "ToolController", None), "Name", None),
            "feed_rate_mm_min": round(feed_rate_mm_min, 3) if feed_rate_mm_min is not None else None,
            "path_length_mm": round(path_length, 3),
            "min_z_mm": min(z_values) if z_values else None,
            "max_z_mm": max(z_values) if z_values else None,
            "estimated_time_min": round(estimated_time_min, 3) if estimated_time_min is not None else None,
        }
    )

succeed(
    "Generated CAM G-code preview metadata.",
    {
        "doc_name": request["doc_name"],
        "job_name": job.Name,
        "operation_previews": preview,
        "operation_count": len(preview),
    },
)
"""
    return _run_cam_script(freecad, "cam_get_gcode_preview", payload, body)


def lint_gcode(gcode_path: str, post_name: str) -> dict[str, Any]:
    if not gcode_path:
        return error_response(ERROR_INVALID_INPUT, "gcode_path is required for G-code linting.")
    if not os.path.exists(gcode_path):
        return error_response(ERROR_INVALID_INPUT, f'G-code file "{gcode_path}" does not exist.')

    with open(gcode_path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle.readlines()]

    return _lint_gcode_lines(lines, gcode_path, post_name)


def lint_remote_gcode(freecad: Any, gcode_path: str, post_name: str) -> dict[str, Any]:
    if not gcode_path:
        return error_response(ERROR_INVALID_INPUT, "gcode_path is required for G-code linting.")

    script = dedent(
        f"""
import json
with open({gcode_path!r}, "r", encoding="utf-8") as handle:
    content = handle.read()
print(json.dumps({{"lines": content.splitlines()}}))
"""
    )
    response = freecad.execute_code(script)
    if not response.get("success"):
        return error_response(
            ERROR_INVALID_INPUT,
            f'Unable to read remote G-code file "{gcode_path}".',
            detail=response.get("error", ""),
            data={"raw_response": response},
        )

    message = response.get("message", "")
    marker = "Output: "
    idx = message.find(marker)
    if idx == -1:
        return error_response(
            ERROR_FREECAD_RESPONSE,
            "Remote FreeCAD response did not include G-code line output.",
            data={"raw_response": response},
        )
    payload_text = message[idx + len(marker):].strip()
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return error_response(
            ERROR_FREECAD_RESPONSE,
            "Failed to decode remote G-code contents from FreeCAD response.",
            detail=str(exc),
            data={"raw_response": response, "payload_text": payload_text},
        )

    lines = [str(line).strip() for line in payload.get("lines", [])]
    return _lint_gcode_lines(lines, gcode_path, post_name)


def _lint_gcode_lines(lines: list[str], gcode_path: str, post_name: str) -> dict[str, Any]:

    content = "\n".join(lines)
    warnings: list[str] = []
    errors: list[str] = []

    if "G90" not in content:
        errors.append("Missing absolute positioning command G90.")
    if "G21" not in content and "G20" not in content:
        errors.append("Missing explicit unit selection command (G21/G20).")
    if not any(code in content for code in ("G17", "G18", "G19")):
        warnings.append("Missing explicit plane selection command (G17/G18/G19).")
    if not any(code in content for code in ("M3", "M03")):
        warnings.append("Missing spindle start command (M3).")
    if not any(code in content for code in ("M5", "M05")):
        warnings.append("Missing spindle stop command (M5).")
    if not any(code in content for code in ("M8", "M08", "M7", "M07")):
        warnings.append("No coolant or mist command found; verify coolant strategy.")
    if not any(code in content for code in ("M9", "M09")):
        warnings.append("No coolant-off command found at program end.")
    if post_name == "grbl" and "G91" in content:
        warnings.append("Found G91 in GRBL output; verify that incremental moves are intentional.")
    if not lines:
        errors.append("G-code file is empty.")
    motion_lines = []
    for line in lines:
        upper = line.upper()
        if upper.startswith(("(", ";")):
            continue
        if upper.startswith(("G0 ", "G00 ", "G1 ", "G01 ", "G2 ", "G02 ", "G3 ", "G03 ")):
            motion_lines.append(line)
    if not motion_lines:
        errors.append("G-code contains no actual motion commands (G0/G1/G2/G3).")
    prologue = "\n".join(lines[:15])
    epilogue = "\n".join(lines[-20:])
    if "G0 Z" not in prologue and "G00 Z" not in prologue:
        warnings.append("Program prologue does not show an early safe Z retract move.")
    if not any(code in epilogue for code in ("M30", "M2", "M02")):
        warnings.append("Program end does not contain M2/M30.")
    if "G0 Z" not in epilogue and "G00 Z" not in epilogue:
        warnings.append("Program epilogue does not show a final safe Z retract move.")

    data = {
        "gcode_path": gcode_path,
        "post_name": post_name,
        "line_count": len(lines),
        "motion_line_count": len(motion_lines),
        "prologue_checked": lines[:15],
        "epilogue_checked": lines[-20:],
        "warnings": warnings,
        "errors": errors,
    }
    if errors:
        return error_response(ERROR_GCODE_LINT_FAILED, "G-code lint failed.", detail="; ".join(errors), data=data, warnings=warnings)
    return success_response("G-code lint passed.", data=data, warnings=warnings)


def _validate_optional_depths(params: dict[str, Any]) -> None:
    start_depth = params.get("StartDepth")
    final_depth = params.get("FinalDepth")
    if start_depth is not None and final_depth is not None and float(final_depth) > float(start_depth):
        raise ValueError("FinalDepth cannot be above StartDepth.")


def _extract_payload(raw_output: str) -> dict[str, Any] | None:
    pattern = re.escape(CAM_RESULT_START) + r"(.*?)" + re.escape(CAM_RESULT_END)
    match = re.search(pattern, raw_output, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group(1))


def _run_cam_script(freecad: Any, action: str, payload: dict[str, Any], body: str) -> dict[str, Any]:
    script = _build_script(action, payload, body)
    try:
        result = freecad.execute_code(script)
    except Exception as exc:
        return error_response(ERROR_FREECAD_EXECUTION, f"{action} failed while calling FreeCAD.", detail=str(exc))

    if not result.get("success", False):
        return error_response(
            ERROR_FREECAD_EXECUTION,
            f"{action} failed inside FreeCAD.",
            detail=result.get("error") or result.get("message") or "",
            data={"raw_response": result},
        )

    raw_output = "\n".join(
        part for part in [result.get("message", ""), result.get("error", "")] if part
    )
    payload_result = _extract_payload(raw_output)
    if payload_result is None:
        return error_response(
            ERROR_FREECAD_RESPONSE,
            f"{action} did not return a structured CAM payload.",
            detail=raw_output,
            data={"raw_response": result},
        )

    return payload_result


def _build_script(action: str, payload: dict[str, Any], body: str) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    body_block = indent(dedent(body).strip(), "    ")
    script = f"""
import importlib
import json
import re
import traceback

class _CamOutcome(Exception):
    pass

request = json.loads({payload_json!r})

def _emit(payload):
    print({CAM_RESULT_START!r} + json.dumps(payload, ensure_ascii=False) + {CAM_RESULT_END!r})

def succeed(message, data=None, warnings=None):
    _emit({{"success": True, "message": message, "data": data or {{}}, "warnings": warnings or []}})
    raise _CamOutcome()

def fail(code, message, detail=None, data=None, warnings=None):
    _emit({{
        "success": False,
        "message": message,
        "error_code": code,
        "error_detail": detail or "",
        "data": data or {{}},
        "warnings": warnings or [],
    }})
    raise _CamOutcome()

def import_module_candidates(names):
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    fail("cam_api_unavailable", f"Unable to import any FreeCAD CAM module from {{names}}.")

def get_job(doc, job_name):
    job = doc.getObject(job_name)
    if job is None:
        fail("job_not_found", f'CAM job "{{job_name}}" was not found.', data={{"doc_name": request.get("doc_name")}})
    return job

def normalize_features(doc, items):
    refs = []
    for item in items:
        if isinstance(item, str):
            obj = doc.getObject(item)
            if obj is None:
                fail("object_not_found", f'Base feature "{{item}}" was not found.')
            refs.append((obj, []))
            continue
        if not isinstance(item, dict):
            fail("invalid_input", "base_features must contain strings or dictionaries.")
        object_name = item.get("object_name") or item.get("name")
        if not object_name:
            fail("invalid_input", "Each base feature dictionary requires object_name.")
        obj = doc.getObject(object_name)
        if obj is None:
            fail("object_not_found", f'Base feature "{{object_name}}" was not found.')
        refs.append((obj, item.get("subelements", [])))
    return refs

try:
    import FreeCAD as App
    doc_name = request.get("doc_name")
    doc = App.getDocument(doc_name) if doc_name else App.ActiveDocument
    if doc is None:
        fail("document_not_found", f'Document "{{doc_name}}" was not found.')
{body_block}
except _CamOutcome:
    pass
except Exception:
    fail("freecad_execution_error", "{action} failed.", traceback.format_exc())
"""
    return dedent(script)
