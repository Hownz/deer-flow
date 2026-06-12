"""Unit tests for the ``_require_admin`` guard in ``app.gateway.routers.agents``.

The guard is intentionally a near-copy of ``_require_admin_user`` from
``app/gateway/routers/mcp.py`` (line 75), so these tests mirror the existing
``tests/test_mcp_config_secrets.py:315-339`` template.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException


def _request_with_role(system_role: str | None) -> SimpleNamespace:
    """Build a fake request whose ``state.user`` carries the given role.

    Mirrors the helper at ``tests/test_mcp_config_secrets.py:320-328``.
    """
    user = SimpleNamespace(id="user-1")
    if system_role is not None:
        user.system_role = system_role
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.mark.asyncio
async def test_require_admin_allows_admin_user():
    from app.gateway.routers.agents import _require_admin

    await _require_admin(_request_with_role("admin"))


@pytest.mark.asyncio
async def test_require_admin_rejects_regular_user():
    from app.gateway.routers.agents import _require_admin

    with pytest.raises(HTTPException) as exc_info:
        await _require_admin(_request_with_role("user"))

    assert exc_info.value.status_code == 403
    assert "Admin" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_rejects_user_without_role_attr():
    """A user object that has no ``system_role`` attribute is rejected with 403."""
    from app.gateway.routers.agents import _require_admin

    with pytest.raises(HTTPException) as exc_info:
        await _require_admin(_request_with_role(None))

    assert exc_info.value.status_code == 403
