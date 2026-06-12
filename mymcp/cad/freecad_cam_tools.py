"""MCP tool registration for structured FreeCAD CAM operations."""

from __future__ import annotations

from typing import Any

from freecad_cam_adapter import (
    add_drilling_locations_operation,
    add_rect_pocket_operation,
    analyze_model_features,
    add_operation,
    cleanup_suspicious_operations,
    create_job,
    create_tool_controller,
    get_job_state,
    get_operation_path_details,
    get_gcode_preview,
    get_remote_gcode,
    get_tool_controller_details,
    import_cad,
    import_step,
    lint_gcode,
    lint_remote_gcode,
    list_tool_presets,
    plan_operations,
    postprocess_job,
    probe_runtime_capabilities,
    remove_operation,
    reorient_model_to_z,
    recompute_job,
    reverse_verify_job_output,
    resolve_operation_features,
    select_tool_preset,
    set_stock,
    suggest_setup,
    set_wcs,
    validate_job,
    verify_gcode_against_targets,
)
from freecad_cam_constants import (
    DEFAULT_CLEARANCE_HEIGHT,
    DEFAULT_FEED_RATE,
    DEFAULT_PLUNGE_RATE,
    DEFAULT_POST,
    DEFAULT_SAFE_HEIGHT,
    DEFAULT_SPINDLE_RPM,
    DEFAULT_UNITS,
)
from freecad_cam_response import dump_response, error_response
from freecad_mcp_observability import observe_mcp_tool


def register_cam_tools(mcp: Any, get_freecad_connection: Any) -> None:
    @mcp.tool()
    @observe_mcp_tool("cam_probe_runtime_capabilities")
    def cam_probe_runtime_capabilities() -> str:
        """Probe available FreeCAD CAM modules, posts, and likely headless runtime limits."""
        return dump_response(probe_runtime_capabilities(get_freecad_connection()))

    @mcp.tool()
    @observe_mcp_tool("cam_import_step")
    def cam_import_step(step_path: str) -> str:
        """Import a STEP/STP file on the connected FreeCAD host and return the imported document/object names."""
        return dump_response(import_step(get_freecad_connection(), step_path))

    @mcp.tool()
    @observe_mcp_tool("cam_import_cad")
    def cam_import_cad(file_path: str) -> str:
        """Import a STEP/STP/IGS/IGES file on the connected FreeCAD host and return imported document/object names."""
        return dump_response(import_cad(get_freecad_connection(), file_path))

    @mcp.tool()
    @observe_mcp_tool("cam_analyze_model_features")
    def cam_analyze_model_features(doc_name: str, model_name: str) -> str:
        """Analyze a model's basic geometric features to support CAM planning."""
        return dump_response(analyze_model_features(get_freecad_connection(), doc_name, model_name))

    @mcp.tool()
    @observe_mcp_tool("cam_suggest_setup")
    def cam_suggest_setup(doc_name: str, model_name: str) -> str:
        """Suggest a conservative 3-axis setup and stock strategy for an existing model."""
        return dump_response(suggest_setup(get_freecad_connection(), doc_name, model_name))

    @mcp.tool()
    @observe_mcp_tool("cam_reorient_model_to_z")
    def cam_reorient_model_to_z(
        doc_name: str,
        model_name: str,
        source_axis: str | None = None,
        output_name: str | None = None,
    ) -> str:
        """Create or update a rotated copy of a model so its dominant machining direction aligns with +Z."""
        return dump_response(reorient_model_to_z(get_freecad_connection(), doc_name, model_name, source_axis, output_name))

    @mcp.tool()
    @observe_mcp_tool("cam_select_tool_preset")
    def cam_select_tool_preset(
        feature_type: str,
        min_width: float | None = None,
        depth: float | None = None,
        material: str | None = None,
    ) -> str:
        """Select a compatible tool preset using feature type, width, depth, and material."""
        return dump_response(select_tool_preset(feature_type, min_width, depth, material))

    @mcp.tool()
    @observe_mcp_tool("cam_plan_operations")
    def cam_plan_operations(
        model_name: str,
        feature_summary: dict[str, Any] | None = None,
        machining_goal: str | None = None,
        doc_name: str | None = None,
    ) -> str:
        """Generate a conservative suggested operation chain without executing it.

        If doc_name is provided and feature_summary is None/empty, auto-populates
        feature_summary by calling analyze_model_features internally.
        """
        if doc_name and not feature_summary:
            result = analyze_model_features(get_freecad_connection(), doc_name, model_name)
            feature_summary = (result.get("data") or {}).get("feature_summary")
        return dump_response(plan_operations(model_name, feature_summary, machining_goal))

    @mcp.tool()
    @observe_mcp_tool("cam_resolve_operation_features")
    def cam_resolve_operation_features(
        doc_name: str,
        model_name: str,
        operation_type: str,
        strategy: str = "conservative",
    ) -> str:
        """Resolve conservative base-feature or location candidates for a CAM operation on an existing model."""
        return dump_response(
            resolve_operation_features(
                get_freecad_connection(),
                doc_name,
                model_name,
                operation_type,
                strategy,
            )
        )

    @mcp.tool()
    @observe_mcp_tool("cam_create_job")
    def cam_create_job(doc_name: str, job_name: str, model_name: str, units: str = DEFAULT_UNITS) -> str:
        """Create a structured CAM job for an existing model object."""
        return dump_response(create_job(get_freecad_connection(), doc_name, job_name, model_name, units))

    @mcp.tool()
    @observe_mcp_tool("cam_set_stock")
    def cam_set_stock(doc_name: str, job_name: str, stock_mode: str, offsets_or_bounds: dict[str, Any]) -> str:
        """Configure the stock for a CAM job using a structured stock mode and offsets."""
        return dump_response(set_stock(get_freecad_connection(), doc_name, job_name, stock_mode, offsets_or_bounds))

    @mcp.tool()
    @observe_mcp_tool("cam_set_wcs")
    def cam_set_wcs(
        doc_name: str,
        job_name: str,
        origin_mode: str,
        origin_params: dict[str, Any] | None = None,
        clearance: float = DEFAULT_CLEARANCE_HEIGHT,
        safe_height: float = DEFAULT_SAFE_HEIGHT,
    ) -> str:
        """Configure work coordinate system and setup heights for a CAM job."""
        return dump_response(
            set_wcs(get_freecad_connection(), doc_name, job_name, origin_mode, origin_params, clearance, safe_height)
        )

    @mcp.tool()
    @observe_mcp_tool("cam_list_tool_presets")
    def cam_list_tool_presets() -> str:
        """List supported CAM tool presets and basic post processors."""
        return dump_response(list_tool_presets())

    @mcp.tool()
    @observe_mcp_tool("cam_create_tool_controller")
    def cam_create_tool_controller(
        doc_name: str,
        job_name: str,
        tool_preset_id: str,
        spindle_rpm: float = DEFAULT_SPINDLE_RPM,
        feed_rate: float = DEFAULT_FEED_RATE,
        plunge_rate: float = DEFAULT_PLUNGE_RATE,
    ) -> str:
        """Create a CAM tool controller from a curated tool preset."""
        return dump_response(
            create_tool_controller(
                get_freecad_connection(),
                doc_name,
                job_name,
                tool_preset_id,
                spindle_rpm,
                feed_rate,
                plunge_rate,
            )
        )

    @mcp.tool()
    @observe_mcp_tool("cam_add_profile_operation")
    def cam_add_profile_operation(
        doc_name: str,
        job_name: str,
        base_features: list[dict[str, Any]] | list[str],
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a profile operation to a CAM job."""
        try:
            return dump_response(
                add_operation(get_freecad_connection(), "profile", doc_name, job_name, base_features, tool_controller, params)
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_add_pocket_operation")
    def cam_add_pocket_operation(
        doc_name: str,
        job_name: str,
        base_features: list[dict[str, Any]] | list[str],
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a pocket operation to a CAM job."""
        try:
            return dump_response(
                add_operation(get_freecad_connection(), "pocket", doc_name, job_name, base_features, tool_controller, params)
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_add_rect_pocket_operation")
    def cam_add_rect_pocket_operation(
        doc_name: str,
        job_name: str,
        model_name: str,
        boundary: dict[str, Any],
        top_z: float,
        final_z: float,
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a pocket operation from an explicit XY rectangle boundary."""
        try:
            return dump_response(
                add_rect_pocket_operation(
                    get_freecad_connection(),
                    doc_name,
                    job_name,
                    model_name,
                    boundary,
                    top_z,
                    final_z,
                    tool_controller,
                    params,
                )
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_add_drilling_operation")
    def cam_add_drilling_operation(
        doc_name: str,
        job_name: str,
        base_features: list[dict[str, Any]] | list[str],
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a drilling operation to a CAM job."""
        try:
            return dump_response(
                add_operation(get_freecad_connection(), "drilling", doc_name, job_name, base_features, tool_controller, params)
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_add_drilling_locations_operation")
    def cam_add_drilling_locations_operation(
        doc_name: str,
        job_name: str,
        locations: list[dict[str, Any]],
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a drilling operation from explicit XY/Z locations when base-hole detection is unreliable."""
        try:
            return dump_response(
                add_drilling_locations_operation(
                    get_freecad_connection(),
                    doc_name,
                    job_name,
                    locations,
                    tool_controller,
                    params,
                )
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_add_face_operation")
    def cam_add_face_operation(
        doc_name: str,
        job_name: str,
        base_features: list[dict[str, Any]] | list[str],
        tool_controller: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Add a facing operation to a CAM job."""
        try:
            return dump_response(
                add_operation(get_freecad_connection(), "face", doc_name, job_name, base_features, tool_controller, params)
            )
        except ValueError as exc:
            return dump_response(error_response("invalid_depth", str(exc)))

    @mcp.tool()
    @observe_mcp_tool("cam_recompute_job")
    def cam_recompute_job(doc_name: str, job_name: str) -> str:
        """Recompute an existing CAM job after changing operations or setup data."""
        return dump_response(recompute_job(get_freecad_connection(), doc_name, job_name))

    @mcp.tool()
    @observe_mcp_tool("cam_get_job_state")
    def cam_get_job_state(doc_name: str, job_name: str) -> str:
        """Inspect a CAM job's operations, command counts, and readiness state."""
        return dump_response(get_job_state(get_freecad_connection(), doc_name, job_name))

    @mcp.tool()
    @observe_mcp_tool("cam_get_tool_controller_details")
    def cam_get_tool_controller_details(
        doc_name: str,
        job_name: str,
        tool_controller_name: str | None = None,
    ) -> str:
        """Inspect actual tool controller spindle and feed values stored in FreeCAD."""
        return dump_response(
            get_tool_controller_details(get_freecad_connection(), doc_name, job_name, tool_controller_name)
        )

    @mcp.tool()
    @observe_mcp_tool("cam_get_operation_path_details")
    def cam_get_operation_path_details(doc_name: str, job_name: str, operation_name: str) -> str:
        """Inspect a single CAM operation's raw path commands for reverse verification."""
        return dump_response(get_operation_path_details(get_freecad_connection(), doc_name, job_name, operation_name))

    @mcp.tool()
    @observe_mcp_tool("cam_remove_operation")
    def cam_remove_operation(doc_name: str, job_name: str, operation_name: str) -> str:
        """Remove a CAM operation from the document and job, typically after a failed generation attempt."""
        return dump_response(remove_operation(get_freecad_connection(), doc_name, job_name, operation_name))

    @mcp.tool()
    @observe_mcp_tool("cam_validate_job")
    def cam_validate_job(doc_name: str, job_name: str) -> str:
        """Run structured validation checks against an existing CAM job."""
        return dump_response(validate_job(get_freecad_connection(), doc_name, job_name))

    @mcp.tool()
    @observe_mcp_tool("cam_postprocess_job")
    def cam_postprocess_job(doc_name: str, job_name: str, post_name: str = DEFAULT_POST, output_path: str = "") -> str:
        """Postprocess a CAM job into G-code and save it to output_path."""
        return dump_response(postprocess_job(get_freecad_connection(), doc_name, job_name, post_name, output_path))

    @mcp.tool()
    @observe_mcp_tool("cam_get_gcode_preview")
    def cam_get_gcode_preview(doc_name: str, job_name: str) -> str:
        """Return lightweight metadata preview for a CAM job's generated operations."""
        return dump_response(get_gcode_preview(get_freecad_connection(), doc_name, job_name))

    @mcp.tool()
    @observe_mcp_tool("cam_lint_gcode")
    def cam_lint_gcode(gcode_path: str, post_name: str = DEFAULT_POST) -> str:
        """Lint generated G-code for basic safety and compatibility issues."""
        return dump_response(lint_gcode(gcode_path, post_name))

    @mcp.tool()
    @observe_mcp_tool("cam_lint_remote_gcode")
    def cam_lint_remote_gcode(gcode_path: str, post_name: str = DEFAULT_POST) -> str:
        """Lint a G-code file that is only accessible from the connected FreeCAD host."""
        return dump_response(lint_remote_gcode(get_freecad_connection(), gcode_path, post_name))

    @mcp.tool()
    @observe_mcp_tool("cam_get_remote_gcode")
    def cam_get_remote_gcode(gcode_path: str) -> str:
        """Read remote G-code text from the connected FreeCAD host for reverse verification."""
        return dump_response(get_remote_gcode(get_freecad_connection(), gcode_path))

    @mcp.tool()
    @observe_mcp_tool("cam_reverse_verify_job_output")
    def cam_reverse_verify_job_output(doc_name: str, job_name: str, gcode_path: str) -> str:
        """Cross-check generated G-code against job operations and flag suspicious non-cutting output."""
        return dump_response(reverse_verify_job_output(get_freecad_connection(), doc_name, job_name, gcode_path))

    @mcp.tool()
    @observe_mcp_tool("cam_verify_gcode_against_targets")
    def cam_verify_gcode_against_targets(gcode_path: str, targets: list[dict[str, Any]]) -> str:
        """Verify generated G-code operation extents against user-requested machining targets."""
        return dump_response(verify_gcode_against_targets(get_freecad_connection(), gcode_path, targets))

    @mcp.tool()
    @observe_mcp_tool("cam_cleanup_suspicious_operations")
    def cam_cleanup_suspicious_operations(
        doc_name: str,
        job_name: str,
        gcode_path: str,
        remove_all: bool = True,
    ) -> str:
        """Remove suspicious operations detected by reverse verification before retrying CAM generation."""
        return dump_response(
            cleanup_suspicious_operations(get_freecad_connection(), doc_name, job_name, gcode_path, remove_all)
        )
