"""CRUD API for custom agents."""

import logging
import re
import shutil
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from deerflow.config.agents_api_config import get_agents_api_config
from deerflow.config.agents_config import (
    AgentConfig,
    list_custom_agents,
    list_custom_agents_with_source,
    load_agent_config,
    load_agent_soul,
)
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["agents"])

AGENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")
_AGENT_TARGET_VALUES: tuple[Literal["user", "default"], ...] = ("user", "default")


class AgentResponse(BaseModel):
    """Response model for a custom agent."""

    name: str = Field(..., description="Agent name (hyphen-case)")
    description: str = Field(default="", description="Agent description")
    model: str | None = Field(default=None, description="Optional model override")
    tool_groups: list[str] | None = Field(default=None, description="Optional tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Optional skill whitelist (None=all, []=none)")
    soul: str | None = Field(default=None, description="SOUL.md content")
    is_default: bool = Field(
        default=False,
        description="True if this agent comes from the system-default layer (users/default/agents/). Visible to all authenticated users; writable only by admins.",
    )


class AgentsListResponse(BaseModel):
    """Response model for listing all custom agents."""

    agents: list[AgentResponse]


class AgentCreateRequest(BaseModel):
    """Request body for creating a custom agent."""

    name: str = Field(..., description="Agent name (must match ^[A-Za-z0-9-]+$, stored as lowercase)")
    description: str = Field(default="", description="Agent description")
    model: str | None = Field(default=None, description="Optional model override")
    tool_groups: list[str] | None = Field(default=None, description="Optional tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Optional skill whitelist (None=all enabled, []=none)")
    soul: str = Field(default="", description="SOUL.md content — agent personality and behavioral guardrails")
    target: Literal["user", "default"] = Field(
        default="user",
        description=(
            "Which on-disk layer to write into. 'user' = caller's per-user dir "
            "(users/{user_id}/agents/, any authed user). 'default' = system-default "
            "dir (users/default/agents/, admin only)."
        ),
    )


class AgentUpdateRequest(BaseModel):
    """Request body for updating a custom agent."""

    description: str | None = Field(default=None, description="Updated description")
    model: str | None = Field(default=None, description="Updated model override")
    tool_groups: list[str] | None = Field(default=None, description="Updated tool group whitelist")
    skills: list[str] | None = Field(default=None, description="Updated skill whitelist (None=all, []=none)")
    soul: str | None = Field(default=None, description="Updated SOUL.md content")
    target: Literal["user", "default"] = Field(
        default="user",
        description=(
            "Which on-disk layer to write into. 'user' = caller's per-user dir "
            "(users/{user_id}/agents/, any authed user). 'default' = system-default "
            "dir (users/default/agents/, admin only)."
        ),
    )


def _validate_agent_name(name: str) -> None:
    """Validate agent name against allowed pattern.

    Args:
        name: The agent name to validate.

    Raises:
        HTTPException: 422 if the name is invalid.
    """
    if not AGENT_NAME_PATTERN.match(name):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid agent name '{name}'. Must match ^[A-Za-z0-9-]+$ (letters, digits, and hyphens only).",
        )


def _normalize_agent_name(name: str) -> str:
    """Normalize agent name to lowercase for filesystem storage."""
    return name.lower()


def _require_agents_api_enabled() -> None:
    """Reject access unless the custom-agent management API is explicitly enabled."""
    if not get_agents_api_config().enabled:
        raise HTTPException(
            status_code=403,
            detail=("Custom-agent management API is disabled. Set agents_api.enabled=true to expose agent and user-profile routes over HTTP."),
        )


async def _require_admin(request: Request) -> None:
    """Require the authenticated caller to have ``system_role == "admin"``.

    Mirrors ``_require_admin_user`` in ``app/gateway/routers/mcp.py``.
    ``AuthMiddleware`` normally stamps ``request.state.user`` before the
    request reaches this router; the fallback to ``get_current_user_from_request``
    keeps this route safe in tests / alternative ASGI compositions that mount
    the router without the global middleware.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        from app.gateway.deps import get_current_user_from_request

        user = await get_current_user_from_request(request)

    if getattr(user, "system_role", None) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to manage system default agents.",
        )


def _agent_config_to_response(
    agent_cfg: AgentConfig,
    include_soul: bool = False,
    *,
    user_id: str | None = None,
    is_default: bool = False,
) -> AgentResponse:
    """Convert AgentConfig to AgentResponse.

    Args:
        agent_cfg: The loaded agent config.
        include_soul: When True, load SOUL.md from disk and include it in the
            response payload.
        user_id: Effective user id used to resolve the SOUL.md path on disk.
        is_default: True when the agent was resolved from the system-default
            layer (``users/default/agents/``). The HTTP layer determines this
            from the source returned by ``list_custom_agents_with_source`` or
            from a per-endpoint filesystem probe (see ``get_agent``).
    """
    soul: str | None = None
    if include_soul:
        soul = load_agent_soul(agent_cfg.name, user_id=user_id) or ""

    return AgentResponse(
        name=agent_cfg.name,
        description=agent_cfg.description,
        model=agent_cfg.model,
        tool_groups=agent_cfg.tool_groups,
        skills=agent_cfg.skills,
        soul=soul,
        is_default=is_default,
    )


@router.get(
    "/agents",
    response_model=AgentsListResponse,
    summary="List Custom Agents",
    description="List all custom agents available in the agents directory, including their soul content.",
)
async def list_agents() -> AgentsListResponse:
    """List all custom agents.

    Returns:
        List of all custom agents with their metadata and soul content.
        Each entry's ``is_default`` flag indicates whether it comes from the
        system-default layer (``users/default/agents/``) — visible to every
        authenticated user and writable only by admins.
    """
    _require_agents_api_enabled()

    user_id = get_effective_user_id()
    try:
        # Use the source-aware variant so we can label default-layer entries.
        results = list_custom_agents_with_source(user_id=user_id)
        return AgentsListResponse(
            agents=[
                _agent_config_to_response(
                    cfg,
                    include_soul=True,
                    user_id=user_id,
                    is_default=(source == "default"),
                )
                for cfg, source in results
            ]
        )
    except Exception as e:
        logger.error(f"Failed to list agents: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list agents: {str(e)}")


@router.get(
    "/agents/check",
    summary="Check Agent Name",
    description="Validate an agent name and check if it is available (case-insensitive).",
)
async def check_agent_name(name: str) -> dict:
    """Check whether an agent name is valid and not yet taken.

    A name is considered taken if it exists in any of the three layers:
    the caller's per-user dir, the system-default dir, or the legacy dir.
    Picking a name that collides with a default-layer entry would shadow
    it for the caller (which is fine, but the caller should be told).

    Args:
        name: The agent name to check.

    Returns:
        ``{"available": true/false, "name": "<normalized>"}``

    Raises:
        HTTPException: 422 if the name is invalid.
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    normalized = _normalize_agent_name(name)
    user_id = get_effective_user_id()
    paths = get_paths()
    available = (
        not paths.user_agent_dir(user_id, normalized).exists()
        and not paths.default_agent_dir(normalized).exists()
        and not paths.agent_dir(normalized).exists()
    )
    return {"available": available, "name": normalized}


@router.get(
    "/agents/{name}",
    response_model=AgentResponse,
    summary="Get Custom Agent",
    description="Retrieve details and SOUL.md content for a specific custom agent.",
)
async def get_agent(name: str) -> AgentResponse:
    """Get a specific custom agent by name.

    Args:
        name: The agent name.

    Returns:
        Agent details including SOUL.md content. The ``is_default`` flag is
        true when the agent was resolved from the system-default layer.

    Raises:
        HTTPException: 404 if agent not found.
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()
    paths = get_paths()

    # Lightweight existence probe to determine the source layer before loading.
    # Mirrors the 3-tier resolution in agents_config.resolve_agent_dir so the
    # reported is_default stays in sync with the file actually read.
    if paths.user_agent_dir(user_id, name).exists():
        is_default = False
    elif paths.default_agent_dir(name).exists():
        is_default = True
    elif paths.agent_dir(name).exists():
        is_default = False
    else:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    try:
        agent_cfg = load_agent_config(name, user_id=user_id)
        return _agent_config_to_response(agent_cfg, include_soul=True, user_id=user_id, is_default=is_default)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
    except Exception as e:
        logger.error(f"Failed to get agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get agent: {str(e)}")


@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=201,
    summary="Create Custom Agent",
    description="Create a new custom agent with its config and SOUL.md.",
)
async def create_agent_endpoint(http_request: Request, request: AgentCreateRequest) -> AgentResponse:
    """Create a new custom agent.

    The ``target`` field on the request picks the on-disk layer:
    - ``"user"`` (default): writes to the caller's per-user dir
      (``users/{user_id}/agents/``); any authenticated user may do this.
    - ``"default"``: writes to the system-default dir
      (``users/default/agents/``); **admin only**.

    Args:
        http_request: FastAPI request (used to read ``request.state.user``
            for the admin guard on default-target writes).
        request: The agent creation request.

    Returns:
        The created agent details, with ``is_default`` reflecting the chosen layer.

    Raises:
        HTTPException: 409 if agent already exists, 422 if name is invalid,
            403 if a non-admin tries ``target=default``.
    """
    _require_agents_api_enabled()
    _validate_agent_name(request.name)
    normalized_name = _normalize_agent_name(request.name)
    user_id = get_effective_user_id()
    paths = get_paths()

    target: Literal["user", "default"] = request.target
    if target == "default":
        await _require_admin(http_request)
        agent_dir = paths.default_agent_dir(normalized_name)
        is_default = True
    else:
        agent_dir = paths.user_agent_dir(user_id, normalized_name)
        is_default = False

    # 409 conflict check now spans all three layers.
    if (
        agent_dir.exists()
        or paths.default_agent_dir(normalized_name).exists()
        or paths.agent_dir(normalized_name).exists()
    ):
        raise HTTPException(status_code=409, detail=f"Agent '{normalized_name}' already exists")

    try:
        agent_dir.mkdir(parents=True, exist_ok=True)

        # Write config.yaml
        config_data: dict = {"name": normalized_name}
        if request.description:
            config_data["description"] = request.description
        if request.model is not None:
            config_data["model"] = request.model
        if request.tool_groups is not None:
            config_data["tool_groups"] = request.tool_groups
        if request.skills is not None:
            config_data["skills"] = request.skills

        config_file = agent_dir / "config.yaml"
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)

        # Write SOUL.md
        soul_file = agent_dir / "SOUL.md"
        soul_file.write_text(request.soul, encoding="utf-8")

        logger.info(f"Created agent '{normalized_name}' at {agent_dir}")

        agent_cfg = load_agent_config(normalized_name, user_id=user_id)
        return _agent_config_to_response(agent_cfg, include_soul=True, user_id=user_id, is_default=is_default)

    except HTTPException:
        raise
    except Exception as e:
        # Clean up on failure
        if agent_dir.exists():
            shutil.rmtree(agent_dir)
        logger.error(f"Failed to create agent '{request.name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")


@router.put(
    "/agents/{name}",
    response_model=AgentResponse,
    summary="Update Custom Agent",
    description="Update an existing custom agent's config and/or SOUL.md.",
)
async def update_agent(http_request: Request, name: str, request: AgentUpdateRequest) -> AgentResponse:
    """Update an existing custom agent.

    The ``target`` field on the request picks the on-disk layer:
    - ``"user"`` (default): writes to the caller's per-user dir; any
      authenticated user may do this.
    - ``"default"``: writes to the system-default dir; **admin only**.

    If the agent only exists in the system-default layer and ``target=user``
    is selected, the read still succeeds (3-tier resolution) but the write
    lands in the caller's per-user dir — this is the read-side shadowing
    pattern applied to write, and is the least surprising interpretation.

    Args:
        http_request: FastAPI request (used to read ``request.state.user``
            for the admin guard on default-target writes).
        name: The agent name.
        request: The update request (all fields optional).

    Returns:
        The updated agent details, with ``is_default`` reflecting the chosen
        write target.

    Raises:
        HTTPException: 404 if agent not found, 403 if a non-admin tries
            ``target=default``, 409 on legacy-migration / per-user-shadow guards.
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()

    target: Literal["user", "default"] = request.target
    paths = get_paths()
    if target == "default":
        await _require_admin(http_request)
        target_dir = paths.default_agent_dir(name)
        is_default = True
    else:
        target_dir = paths.user_agent_dir(user_id, name)
        is_default = False

    try:
        agent_cfg = load_agent_config(name, user_id=user_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    # Legacy-guard only applies to the per-user target.
    if target == "user" and not target_dir.exists() and paths.agent_dir(name).exists():
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{name}' only exists in the legacy shared layout and is not scoped to a user. Run scripts/migrate_user_isolation.py to move legacy agents into the per-user layout before updating."),
        )

    # Prevent an admin from silently mutating a per-user agent by addressing
    # it as target=default. The agent only exists in the caller's per-user
    # dir, so a default-target write would be a no-op or shadowing trick —
    # refuse explicitly.
    if target == "default" and not target_dir.exists() and paths.user_agent_dir(user_id, name).exists():
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{name}' exists in the caller's per-user layer; cannot update as default.",
        )

    try:
        # Update config if any config fields changed
        # Use model_fields_set to distinguish "field omitted" from "explicitly set to null".
        # This is critical for skills where None means "inherit all" (not "don't change").
        fields_set = request.model_fields_set
        config_changed = bool(fields_set & {"description", "model", "tool_groups", "skills"})

        if config_changed:
            updated: dict = {
                "name": agent_cfg.name,
                "description": request.description if "description" in fields_set else agent_cfg.description,
            }
            new_model = request.model if "model" in fields_set else agent_cfg.model
            if new_model is not None:
                updated["model"] = new_model

            new_tool_groups = request.tool_groups if "tool_groups" in fields_set else agent_cfg.tool_groups
            if new_tool_groups is not None:
                updated["tool_groups"] = new_tool_groups

            # skills: None = inherit all, [] = no skills, ["a","b"] = whitelist
            if "skills" in fields_set:
                new_skills = request.skills
            else:
                new_skills = agent_cfg.skills
            if new_skills is not None:
                updated["skills"] = new_skills

            config_file = target_dir / "config.yaml"
            with open(config_file, "w", encoding="utf-8") as f:
                yaml.dump(updated, f, default_flow_style=False, allow_unicode=True)

        # Update SOUL.md if provided
        if request.soul is not None:
            soul_path = target_dir / "SOUL.md"
            soul_path.write_text(request.soul, encoding="utf-8")

        logger.info(f"Updated agent '{name}' at {target_dir}")

        refreshed_cfg = load_agent_config(name, user_id=user_id)
        return _agent_config_to_response(refreshed_cfg, include_soul=True, user_id=user_id, is_default=is_default)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {str(e)}")


class UserProfileResponse(BaseModel):
    """Response model for the global user profile (USER.md)."""

    content: str | None = Field(default=None, description="USER.md content, or null if not yet created")


class UserProfileUpdateRequest(BaseModel):
    """Request body for setting the global user profile."""

    content: str = Field(default="", description="USER.md content — describes the user's background and preferences")


@router.get(
    "/user-profile",
    response_model=UserProfileResponse,
    summary="Get User Profile",
    description="Read the global USER.md file that is injected into all custom agents.",
)
async def get_user_profile() -> UserProfileResponse:
    """Return the current USER.md content.

    Returns:
        UserProfileResponse with content=None if USER.md does not exist yet.
    """
    _require_agents_api_enabled()

    try:
        user_md_path = get_paths().user_md_file
        if not user_md_path.exists():
            return UserProfileResponse(content=None)
        raw = user_md_path.read_text(encoding="utf-8").strip()
        return UserProfileResponse(content=raw or None)
    except Exception as e:
        logger.error(f"Failed to read user profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to read user profile: {str(e)}")


@router.put(
    "/user-profile",
    response_model=UserProfileResponse,
    summary="Update User Profile",
    description="Write the global USER.md file that is injected into all custom agents.",
)
async def update_user_profile(request: UserProfileUpdateRequest) -> UserProfileResponse:
    """Create or overwrite the global USER.md.

    Args:
        request: The update request with the new USER.md content.

    Returns:
        UserProfileResponse with the saved content.
    """
    _require_agents_api_enabled()

    try:
        paths = get_paths()
        paths.base_dir.mkdir(parents=True, exist_ok=True)
        paths.user_md_file.write_text(request.content, encoding="utf-8")
        logger.info(f"Updated USER.md at {paths.user_md_file}")
        return UserProfileResponse(content=request.content or None)
    except Exception as e:
        logger.error(f"Failed to update user profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update user profile: {str(e)}")


@router.delete(
    "/agents/{name}",
    status_code=204,
    summary="Delete Custom Agent",
    description=(
        "Delete a custom agent and all its files (config, SOUL.md, memory). "
        "Use the ``target`` query parameter to choose the layer: ``user`` "
        "(default) deletes from the caller's per-user dir; ``default`` "
        "deletes from the system-default dir and is **admin only**."
    ),
)
async def delete_agent(
    http_request: Request,
    name: str,
    target: Literal["user", "default"] = Query(default="user"),
) -> None:
    """Delete a custom agent.

    Args:
        http_request: FastAPI request (used for the admin guard on
            ``target=default`` deletes).
        name: The agent name.
        target: ``"user"`` (default) deletes the caller's per-user copy;
            ``"default"`` deletes the system-default copy (admin only).

    Raises:
        HTTPException: 404 if the targeted copy does not exist, 409 if a
            per-user delete collides with the legacy layout, 403 if a
            non-admin tries ``target=default``.
    """
    _require_agents_api_enabled()
    _validate_agent_name(name)
    name = _normalize_agent_name(name)
    user_id = get_effective_user_id()
    paths = get_paths()

    if target == "default":
        await _require_admin(http_request)
        target_dir = paths.default_agent_dir(name)
    else:
        target_dir = paths.user_agent_dir(user_id, name)

    if not target_dir.exists():
        if target == "user" and paths.agent_dir(name).exists():
            raise HTTPException(
                status_code=409,
                detail=(f"Agent '{name}' only exists in the legacy shared layout and is not scoped to a user. Run scripts/migrate_user_isolation.py to move legacy agents into the per-user layout before deleting."),
            )
        raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

    try:
        shutil.rmtree(target_dir)
        logger.info(f"Deleted agent '{name}' from {target_dir}")
    except Exception as e:
        logger.error(f"Failed to delete agent '{name}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to delete agent: {str(e)}")
