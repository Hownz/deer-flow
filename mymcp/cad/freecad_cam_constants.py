"""Constants for the incremental FreeCAD CAM MCP integration."""

from __future__ import annotations

CAM_RESULT_START = "__CAM_RESULT_START__"
CAM_RESULT_END = "__CAM_RESULT_END__"

DEFAULT_UNITS = "mm"
DEFAULT_JOB_TYPE = "2.5D"
DEFAULT_POST = "grbl"
SUPPORTED_POSTS = ("grbl", "linuxcnc")
SUPPORTED_OPERATIONS = ("profile", "pocket", "drilling", "face")

ERROR_INVALID_INPUT = "invalid_input"
ERROR_DOCUMENT_NOT_FOUND = "document_not_found"
ERROR_OBJECT_NOT_FOUND = "object_not_found"
ERROR_JOB_NOT_FOUND = "job_not_found"
ERROR_TOOL_PRESET_NOT_FOUND = "tool_preset_not_found"
ERROR_UNSUPPORTED_POST = "unsupported_post"
ERROR_EMPTY_TOOLPATH = "empty_toolpath"
ERROR_INVALID_HEIGHT = "invalid_height"
ERROR_SAFE_HEIGHT_LOW = "safe_height_low"
ERROR_INVALID_DEPTH = "invalid_depth"
ERROR_FINAL_DEPTH_OUT_OF_BOUNDS = "final_depth_out_of_bounds"
ERROR_INVALID_TOOL = "invalid_tool"
ERROR_TOOL_DIAMETER_TOO_LARGE = "tool_diameter_too_large"
ERROR_FEATURE_NOT_REACHABLE = "feature_not_reachable"
ERROR_GCODE_LINT_FAILED = "gcode_lint_failed"
ERROR_FREECAD_EXECUTION = "freecad_execution_error"
ERROR_FREECAD_RESPONSE = "freecad_response_error"
ERROR_CAM_API_UNAVAILABLE = "cam_api_unavailable"

DEFAULT_CLEARANCE_HEIGHT = 10.0
DEFAULT_SAFE_HEIGHT = 5.0
DEFAULT_FEED_RATE = 300.0
DEFAULT_PLUNGE_RATE = 120.0
DEFAULT_SPINDLE_RPM = 12000.0

# TOOL_PRESETS = [
#     {
#         "id": "em_2mm",
#         "label": "2 mm Endmill",
#         "tool_type": "endmill",
#         "diameter_mm": 2.0,
#         "flutes": 4,
#         "materials": ["aluminum", "plastic", "wood", "steel"],
#         "recommended_rpm_range": [5000, 12000],
#         "recommended_feed_range": [80, 220],
#         "description": "Small 2 mm flat endmill for fine profiling on tighter features, including conservative steel work.",
#     },
#     {
#         "id": "em_3mm",
#         "label": "3 mm Endmill",
#         "tool_type": "endmill",
#         "diameter_mm": 3.0,
#         "flutes": 2,
#         "materials": ["aluminum", "plastic", "wood"],
#         "recommended_rpm_range": [10000, 18000],
#         "recommended_feed_range": [200, 600],
#         "description": "General purpose 3 mm flat endmill for light profiling and pocketing.",
#     },
#     {
#         "id": "em_4mm",
#         "label": "4 mm Endmill",
#         "tool_type": "endmill",
#         "diameter_mm": 4.0,
#         "flutes": 2,
#         "materials": ["aluminum", "plastic", "wood"],
#         "recommended_rpm_range": [9000, 16000],
#         "recommended_feed_range": [180, 700],
#         "description": "4 mm flat endmill for conservative slotting and medium-width pocketing.",
#     },
#     {
#         "id": "em_6mm",
#         "label": "6 mm Endmill",
#         "tool_type": "endmill",
#         "diameter_mm": 6.0,
#         "flutes": 4,
#         "materials": ["aluminum", "plastic", "wood", "steel"],
#         "recommended_rpm_range": [6000, 14000],
#         "recommended_feed_range": [250, 900],
#         "description": "General purpose 6 mm flat endmill for roughing and facing.",
#     },
#     {
#         "id": "drill_3mm",
#         "label": "3 mm Drill",
#         "tool_type": "drill",
#         "diameter_mm": 3.0,
#         "flutes": 2,
#         "materials": ["aluminum", "plastic", "wood", "steel"],
#         "recommended_rpm_range": [3000, 12000],
#         "recommended_feed_range": [80, 240],
#         "description": "3 mm drill for small through holes.",
#     },
#     {
#         "id": "drill_6mm",
#         "label": "6 mm Drill",
#         "tool_type": "drill",
#         "diameter_mm": 6.0,
#         "flutes": 2,
#         "materials": ["aluminum", "plastic", "wood", "steel"],
#         "recommended_rpm_range": [2000, 8000],
#         "recommended_feed_range": [60, 180],
#         "description": "6 mm drill for larger through holes.",
#     },
#     {
#         "id": "drill_8mm",
#         "label": "8 mm Drill",
#         "tool_type": "drill",
#         "diameter_mm": 8.0,
#         "flutes": 2,
#         "materials": ["aluminum", "plastic", "wood", "steel"],
#         "recommended_rpm_range": [1800, 6500],
#         "recommended_feed_range": [50, 150],
#         "description": "8 mm drill for flange bolt holes and other medium through holes.",
#     },
# ]

TOOL_PRESETS = [
    {
        "id": "em_5mm",
        "label": "5 mm Endmill",
        "tool_type": "endmill",
        "diameter_mm": 5.0,
        "flutes": 4,
        "materials": ["aluminum", "plastic", "wood", "steel"],
        "recommended_rpm_range": [6000, 14000],
        "recommended_feed_range": [200, 800],
        "description": "Main 5 mm flat endmill for general profiling and pocketing.",
    },
    {
        "id": "ball_6mm",
        "label": "6 mm Ball End",
        "tool_type": "ball_endmill",
        "diameter_mm": 6.0,
        "flutes": 2,
        "materials": ["aluminum", "plastic", "wood"],
        "recommended_rpm_range": [6000, 14000],
        "recommended_feed_range": [200, 700],
        "description": "6 mm ball endmill for 3D contouring or finishing.",
    },
    {
        "id": "bullnose_6mm",
        "label": "6 mm Bullnose",
        "tool_type": "bullnose_endmill",
        "diameter_mm": 6.0,
        "flutes": 2,
        "materials": ["aluminum", "plastic", "wood"],
        "recommended_rpm_range": [6000, 14000],
        "recommended_feed_range": [200, 700],
        "description": "6 mm bullnose endmill for blending and medium finishing.",
    },
    {
        "id": "drill_5mm",
        "label": "5 mm Drill",
        "tool_type": "drill",
        "diameter_mm": 5.0,
        "flutes": 2,
        "materials": ["aluminum", "plastic", "wood", "steel"],
        "recommended_rpm_range": [3000, 9000],
        "recommended_feed_range": [80, 240],
        "description": "5 mm drill for general through holes.",
    },
    {
        "id": "drill_6mm",
        "label": "6 mm Drill",
        "tool_type": "drill",
        "diameter_mm": 6.0,
        "flutes": 2,
        "materials": ["aluminum", "plastic", "wood", "steel"],
        "recommended_rpm_range": [2000, 8000],
        "recommended_feed_range": [60, 180],
        "description": "6 mm drill for 6 mm through holes and common mounting holes.",
    },
    # 可以视需要再加 chamfer、V-bit、slittingsaw、thread_cutter 等
]
