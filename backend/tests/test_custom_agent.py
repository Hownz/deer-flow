"""Tests for custom agent support."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from deerflow.config.agents_api_config import AgentsApiConfig, get_agents_api_config, set_agents_api_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_user() -> SimpleNamespace:
    """Build a SimpleNamespace mirroring User with system_role=admin."""
    return SimpleNamespace(id="00000000-0000-0000-0000-000000000001", system_role="admin")


def _regular_user() -> SimpleNamespace:
    """Build a SimpleNamespace mirroring User with system_role=user."""
    return SimpleNamespace(id="00000000-0000-0000-0000-000000000002", system_role="user")


class _StampUserMiddleware(BaseHTTPMiddleware):
    """Inject a fake ``request.state.user`` for the duration of a request.

    The production auth path (``AuthMiddleware``) does this from a JWT cookie.
    Tests that don't mount the global middleware need a replacement so the
    inline ``_require_admin`` guard in ``routers/agents.py`` can read the
    role. Modeled on the SimpleNamespace pattern in
    ``tests/test_mcp_config_secrets.py:320-328``.
    """

    def __init__(self, app, user: SimpleNamespace) -> None:
        super().__init__(app)
        self._user = user

    async def dispatch(self, request, call_next):
        request.state.user = self._user
        return await call_next(request)


def _make_paths(base_dir: Path):
    """Return a Paths instance pointing to base_dir."""
    from deerflow.config.paths import Paths

    return Paths(base_dir=base_dir)


def _write_agent(base_dir: Path, name: str, config: dict, soul: str = "You are helpful.") -> None:
    """Write an agent directory with config.yaml and SOUL.md."""
    agent_dir = base_dir / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)

    config_copy = dict(config)
    if "name" not in config_copy:
        config_copy["name"] = name

    with open(agent_dir / "config.yaml", "w") as f:
        yaml.dump(config_copy, f)

    (agent_dir / "SOUL.md").write_text(soul, encoding="utf-8")


# ===========================================================================
# 1. Paths class – agent path methods
# ===========================================================================


class TestPaths:
    def test_agents_dir(self, tmp_path):
        paths = _make_paths(tmp_path)
        assert paths.agents_dir == tmp_path / "agents"

    def test_agent_dir(self, tmp_path):
        paths = _make_paths(tmp_path)
        assert paths.agent_dir("code-reviewer") == tmp_path / "agents" / "code-reviewer"

    def test_agent_memory_file(self, tmp_path):
        paths = _make_paths(tmp_path)
        assert paths.agent_memory_file("code-reviewer") == tmp_path / "agents" / "code-reviewer" / "memory.json"

    def test_user_md_file(self, tmp_path):
        paths = _make_paths(tmp_path)
        assert paths.user_md_file == tmp_path / "USER.md"

    def test_paths_are_different_from_global(self, tmp_path):
        paths = _make_paths(tmp_path)
        assert paths.memory_file != paths.agent_memory_file("my-agent")
        assert paths.memory_file == tmp_path / "memory.json"
        assert paths.agent_memory_file("my-agent") == tmp_path / "agents" / "my-agent" / "memory.json"


# ===========================================================================
# 2. AgentConfig – Pydantic parsing
# ===========================================================================


class TestAgentConfig:
    def test_minimal_config(self):
        from deerflow.config.agents_config import AgentConfig

        cfg = AgentConfig(name="my-agent")
        assert cfg.name == "my-agent"
        assert cfg.description == ""
        assert cfg.model is None
        assert cfg.tool_groups is None

    def test_full_config(self):
        from deerflow.config.agents_config import AgentConfig

        cfg = AgentConfig(
            name="code-reviewer",
            description="Specialized for code review",
            model="deepseek-v3",
            tool_groups=["file:read", "bash"],
        )
        assert cfg.name == "code-reviewer"
        assert cfg.model == "deepseek-v3"
        assert cfg.tool_groups == ["file:read", "bash"]

    def test_config_from_dict(self):
        from deerflow.config.agents_config import AgentConfig

        data = {"name": "test-agent", "description": "A test", "model": "gpt-4"}
        cfg = AgentConfig(**data)
        assert cfg.name == "test-agent"
        assert cfg.model == "gpt-4"
        assert cfg.tool_groups is None


# ===========================================================================
# 3. load_agent_config
# ===========================================================================


class TestLoadAgentConfig:
    def test_load_valid_config(self, tmp_path):
        config_dict = {"name": "code-reviewer", "description": "Code review agent", "model": "deepseek-v3"}
        _write_agent(tmp_path, "code-reviewer", config_dict)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("code-reviewer")

        assert cfg.name == "code-reviewer"
        assert cfg.description == "Code review agent"
        assert cfg.model == "deepseek-v3"

    def test_load_missing_agent_raises(self, tmp_path):
        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            with pytest.raises(FileNotFoundError):
                load_agent_config("nonexistent-agent")

    def test_load_missing_config_yaml_raises(self, tmp_path):
        # Create directory without config.yaml
        (tmp_path / "agents" / "broken-agent").mkdir(parents=True)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            with pytest.raises(FileNotFoundError):
                load_agent_config("broken-agent")

    def test_load_config_infers_name_from_dir(self, tmp_path):
        """Config without 'name' field should use directory name."""
        agent_dir = tmp_path / "agents" / "inferred-name"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text("description: My agent\n")
        (agent_dir / "SOUL.md").write_text("Hello")

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("inferred-name")

        assert cfg.name == "inferred-name"

    def test_load_config_with_tool_groups(self, tmp_path):
        config_dict = {"name": "restricted", "tool_groups": ["file:read", "file:write"]}
        _write_agent(tmp_path, "restricted", config_dict)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("restricted")

        assert cfg.tool_groups == ["file:read", "file:write"]

    def test_load_config_with_skills_empty_list(self, tmp_path):
        config_dict = {"name": "no-skills-agent", "skills": []}
        _write_agent(tmp_path, "no-skills-agent", config_dict)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("no-skills-agent")

        assert cfg.skills == []

    def test_load_config_with_skills_omitted(self, tmp_path):
        config_dict = {"name": "default-skills-agent"}
        _write_agent(tmp_path, "default-skills-agent", config_dict)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("default-skills-agent")

        assert cfg.skills is None

    def test_legacy_prompt_file_field_ignored(self, tmp_path):
        """Unknown fields like the old prompt_file should be silently ignored."""
        agent_dir = tmp_path / "agents" / "legacy-agent"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text("name: legacy-agent\nprompt_file: system.md\n")
        (agent_dir / "SOUL.md").write_text("Soul content")

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import load_agent_config

            cfg = load_agent_config("legacy-agent")

        assert cfg.name == "legacy-agent"


# ===========================================================================
# 4. load_agent_soul
# ===========================================================================


class TestLoadAgentSoul:
    def test_reads_soul_file(self, tmp_path):
        expected_soul = "You are a specialized code review expert."
        _write_agent(tmp_path, "code-reviewer", {"name": "code-reviewer"}, soul=expected_soul)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import AgentConfig, load_agent_soul

            cfg = AgentConfig(name="code-reviewer")
            soul = load_agent_soul(cfg.name)

        assert soul == expected_soul

    def test_missing_soul_file_returns_none(self, tmp_path):
        agent_dir = tmp_path / "agents" / "no-soul"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text("name: no-soul\n")
        # No SOUL.md created

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import AgentConfig, load_agent_soul

            cfg = AgentConfig(name="no-soul")
            soul = load_agent_soul(cfg.name)

        assert soul is None

    def test_empty_soul_file_returns_none(self, tmp_path):
        agent_dir = tmp_path / "agents" / "empty-soul"
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text("name: empty-soul\n")
        (agent_dir / "SOUL.md").write_text("   \n   ")

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import AgentConfig, load_agent_soul

            cfg = AgentConfig(name="empty-soul")
            soul = load_agent_soul(cfg.name)

        assert soul is None


# ===========================================================================
# 5. list_custom_agents
# ===========================================================================


class TestListCustomAgents:
    def test_empty_when_no_agents_dir(self, tmp_path):
        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import list_custom_agents

            agents = list_custom_agents()

        assert agents == []

    def test_discovers_multiple_agents(self, tmp_path):
        _write_agent(tmp_path, "agent-a", {"name": "agent-a"})
        _write_agent(tmp_path, "agent-b", {"name": "agent-b", "description": "B"})

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import list_custom_agents

            agents = list_custom_agents()

        names = [a.name for a in agents]
        assert "agent-a" in names
        assert "agent-b" in names

    def test_skips_dirs_without_config_yaml(self, tmp_path):
        # Valid agent
        _write_agent(tmp_path, "valid-agent", {"name": "valid-agent"})
        # Invalid dir (no config.yaml)
        (tmp_path / "agents" / "invalid-dir").mkdir(parents=True)

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import list_custom_agents

            agents = list_custom_agents()

        assert len(agents) == 1
        assert agents[0].name == "valid-agent"

    def test_skips_non_directory_entries(self, tmp_path):
        # Create the agents dir with a file (not a dir)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "not-a-dir.txt").write_text("hello")
        _write_agent(tmp_path, "real-agent", {"name": "real-agent"})

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import list_custom_agents

            agents = list_custom_agents()

        assert len(agents) == 1
        assert agents[0].name == "real-agent"

    def test_returns_sorted_by_name(self, tmp_path):
        _write_agent(tmp_path, "z-agent", {"name": "z-agent"})
        _write_agent(tmp_path, "a-agent", {"name": "a-agent"})
        _write_agent(tmp_path, "m-agent", {"name": "m-agent"})

        with patch("deerflow.config.agents_config.get_paths", return_value=_make_paths(tmp_path)):
            from deerflow.config.agents_config import list_custom_agents

            agents = list_custom_agents()

        names = [a.name for a in agents]
        assert names == sorted(names)


# ===========================================================================
# 7. Memory isolation: _get_memory_file_path
# ===========================================================================


class TestMemoryFilePath:
    def test_global_memory_path(self, tmp_path):
        """None agent_name should return global memory file."""
        from deerflow.agents.memory.storage import FileMemoryStorage
        from deerflow.config.memory_config import MemoryConfig

        with (
            patch("deerflow.agents.memory.storage.get_paths", return_value=_make_paths(tmp_path)),
            patch("deerflow.agents.memory.storage.get_memory_config", return_value=MemoryConfig(storage_path="")),
        ):
            storage = FileMemoryStorage()
            path = storage._get_memory_file_path(None)
        assert path == tmp_path / "memory.json"

    def test_agent_memory_path(self, tmp_path):
        """Providing agent_name should return per-agent memory file."""
        from deerflow.agents.memory.storage import FileMemoryStorage
        from deerflow.config.memory_config import MemoryConfig

        with (
            patch("deerflow.agents.memory.storage.get_paths", return_value=_make_paths(tmp_path)),
            patch("deerflow.agents.memory.storage.get_memory_config", return_value=MemoryConfig(storage_path="")),
        ):
            storage = FileMemoryStorage()
            path = storage._get_memory_file_path("code-reviewer")
        assert path == tmp_path / "agents" / "code-reviewer" / "memory.json"

    def test_different_paths_for_different_agents(self, tmp_path):
        from deerflow.agents.memory.storage import FileMemoryStorage
        from deerflow.config.memory_config import MemoryConfig

        with (
            patch("deerflow.agents.memory.storage.get_paths", return_value=_make_paths(tmp_path)),
            patch("deerflow.agents.memory.storage.get_memory_config", return_value=MemoryConfig(storage_path="")),
        ):
            storage = FileMemoryStorage()
            path_global = storage._get_memory_file_path(None)
            path_a = storage._get_memory_file_path("agent-a")
            path_b = storage._get_memory_file_path("agent-b")

        assert path_global != path_a
        assert path_global != path_b
        assert path_a != path_b


# ===========================================================================
# 8. Gateway API – Agents endpoints
# ===========================================================================


def _make_test_app(tmp_path: Path, user: SimpleNamespace | None = None):
    """Create a FastAPI app with the agents router, patching paths to tmp_path.

    When ``user`` is provided, a tiny middleware stamps ``request.state.user``
    on every request so the inline ``_require_admin`` guard can read the role.
    This mirrors the production ``AuthMiddleware`` behavior without mounting
    the full auth stack.
    """
    from fastapi import FastAPI

    from app.gateway.routers.agents import router

    app = FastAPI()
    if user is not None:
        app.add_middleware(_StampUserMiddleware, user=user)
    app.include_router(router)
    return app


@pytest.fixture()
def agent_client(tmp_path):
    """TestClient with agents router, using tmp_path as base_dir.

    Stamps an admin user so the ``_require_admin`` guard never blocks
    default-layer writes. Per-user writes by an authed user also pass.
    """
    import app.gateway.routers.agents as agents_router

    paths_instance = _make_paths(tmp_path)
    previous_config = AgentsApiConfig(**get_agents_api_config().model_dump())

    with patch("deerflow.config.agents_config.get_paths", return_value=paths_instance), patch.object(agents_router, "get_paths", return_value=paths_instance):
        set_agents_api_config(AgentsApiConfig(enabled=True))
        try:
            app = _make_test_app(tmp_path, user=_admin_user())
            with TestClient(app) as client:
                client._tmp_path = tmp_path  # type: ignore[attr-defined]
                yield client
        finally:
            set_agents_api_config(previous_config)


@pytest.fixture()
def regular_agent_client(tmp_path):
    """TestClient that stamps a non-admin user.

    Used to verify that ``target=default`` writes are rejected with 403 while
    per-user writes still succeed.
    """
    import app.gateway.routers.agents as agents_router

    paths_instance = _make_paths(tmp_path)
    previous_config = AgentsApiConfig(**get_agents_api_config().model_dump())

    with patch("deerflow.config.agents_config.get_paths", return_value=paths_instance), patch.object(agents_router, "get_paths", return_value=paths_instance):
        set_agents_api_config(AgentsApiConfig(enabled=True))
        try:
            app = _make_test_app(tmp_path, user=_regular_user())
            with TestClient(app) as client:
                client._tmp_path = tmp_path  # type: ignore[attr-defined]
                yield client
        finally:
            set_agents_api_config(previous_config)


@pytest.fixture()
def disabled_agent_client(tmp_path):
    """TestClient with agents router while the management API is disabled."""
    import app.gateway.routers.agents as agents_router

    paths_instance = _make_paths(tmp_path)
    previous_config = AgentsApiConfig(**get_agents_api_config().model_dump())

    with patch("deerflow.config.agents_config.get_paths", return_value=paths_instance), patch.object(agents_router, "get_paths", return_value=paths_instance):
        set_agents_api_config(AgentsApiConfig(enabled=False))
        try:
            app = _make_test_app(tmp_path)
            with TestClient(app) as client:
                yield client
        finally:
            set_agents_api_config(previous_config)


class TestAgentsAPI:
    def test_list_agents_empty(self, agent_client):
        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        data = response.json()
        assert data["agents"] == []

    def test_create_agent(self, agent_client):
        payload = {
            "name": "code-reviewer",
            "description": "Reviews code",
            "soul": "You are a code reviewer.",
        }
        response = agent_client.post("/api/agents", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "code-reviewer"
        assert data["description"] == "Reviews code"
        assert data["soul"] == "You are a code reviewer."

    def test_create_agent_invalid_name(self, agent_client):
        payload = {"name": "Code Reviewer!", "soul": "test"}
        response = agent_client.post("/api/agents", json=payload)
        assert response.status_code == 422

    def test_create_duplicate_agent_409(self, agent_client):
        payload = {"name": "my-agent", "soul": "test"}
        agent_client.post("/api/agents", json=payload)

        # Second create should fail
        response = agent_client.post("/api/agents", json=payload)
        assert response.status_code == 409

    def test_list_agents_after_create(self, agent_client):
        agent_client.post("/api/agents", json={"name": "agent-one", "soul": "p1"})
        agent_client.post("/api/agents", json={"name": "agent-two", "soul": "p2"})

        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        names = [a["name"] for a in response.json()["agents"]]
        assert "agent-one" in names
        assert "agent-two" in names

    def test_list_agents_includes_soul(self, agent_client):
        agent_client.post("/api/agents", json={"name": "soul-agent", "soul": "My soul content"})

        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        agents = response.json()["agents"]
        soul_agent = next(a for a in agents if a["name"] == "soul-agent")
        assert soul_agent["soul"] == "My soul content"

    def test_get_agent(self, agent_client):
        agent_client.post("/api/agents", json={"name": "test-agent", "soul": "Hello world"})

        response = agent_client.get("/api/agents/test-agent")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "test-agent"
        assert data["soul"] == "Hello world"

    def test_get_missing_agent_404(self, agent_client):
        response = agent_client.get("/api/agents/nonexistent")
        assert response.status_code == 404

    def test_update_agent_soul(self, agent_client):
        agent_client.post("/api/agents", json={"name": "update-me", "soul": "original"})

        response = agent_client.put("/api/agents/update-me", json={"soul": "updated"})
        assert response.status_code == 200
        assert response.json()["soul"] == "updated"

    def test_update_agent_description(self, agent_client):
        agent_client.post("/api/agents", json={"name": "desc-agent", "description": "old desc", "soul": "p"})

        response = agent_client.put("/api/agents/desc-agent", json={"description": "new desc"})
        assert response.status_code == 200
        assert response.json()["description"] == "new desc"

    def test_update_missing_agent_404(self, agent_client):
        response = agent_client.put("/api/agents/ghost-agent", json={"soul": "new"})
        assert response.status_code == 404

    def test_delete_agent(self, agent_client):
        agent_client.post("/api/agents", json={"name": "del-me", "soul": "bye"})

        response = agent_client.delete("/api/agents/del-me")
        assert response.status_code == 204

        # Verify it's gone
        response = agent_client.get("/api/agents/del-me")
        assert response.status_code == 404

    def test_delete_missing_agent_404(self, agent_client):
        response = agent_client.delete("/api/agents/does-not-exist")
        assert response.status_code == 404

    def test_create_agent_with_model_and_tool_groups(self, agent_client):
        payload = {
            "name": "specialized",
            "description": "Specialized agent",
            "model": "deepseek-v3",
            "tool_groups": ["file:read", "bash"],
            "soul": "You are specialized.",
        }
        response = agent_client.post("/api/agents", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["model"] == "deepseek-v3"
        assert data["tool_groups"] == ["file:read", "bash"]

    def test_create_persists_files_on_disk(self, agent_client, tmp_path):
        agent_client.post("/api/agents", json={"name": "disk-check", "soul": "disk soul"})

        # tests/conftest.py installs an autouse fixture that sets the
        # contextvar to "test-user-autouse", so the agent is persisted under
        # users/test-user-autouse/agents/ rather than the legacy shared dir.
        agent_dir = tmp_path / "users" / "test-user-autouse" / "agents" / "disk-check"
        assert agent_dir.exists()
        assert (agent_dir / "config.yaml").exists()
        assert (agent_dir / "SOUL.md").exists()
        assert (agent_dir / "SOUL.md").read_text() == "disk soul"

    def test_delete_removes_files_from_disk(self, agent_client, tmp_path):
        agent_client.post("/api/agents", json={"name": "remove-me", "soul": "bye"})
        agent_dir = tmp_path / "users" / "test-user-autouse" / "agents" / "remove-me"
        assert agent_dir.exists()

        agent_client.delete("/api/agents/remove-me")
        assert not agent_dir.exists()

    def test_create_rejects_legacy_name_collision(self, agent_client, tmp_path):
        """An unmigrated legacy agent must still block name collision so that
        running the migration script later won't shadow the legacy entry."""
        legacy_dir = tmp_path / "agents" / "legacy-agent"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "config.yaml").write_text("name: legacy-agent\n", encoding="utf-8")
        (legacy_dir / "SOUL.md").write_text("legacy soul", encoding="utf-8")

        response = agent_client.post("/api/agents", json={"name": "legacy-agent", "soul": "x"})
        assert response.status_code == 409


# ===========================================================================
# 9. Gateway API – User Profile endpoints
# ===========================================================================


class TestUserProfileAPI:
    def test_get_user_profile_empty(self, agent_client):
        response = agent_client.get("/api/user-profile")
        assert response.status_code == 200
        assert response.json()["content"] is None

    def test_put_user_profile(self, agent_client, tmp_path):
        content = "# User Profile\n\nI am a developer."
        response = agent_client.put("/api/user-profile", json={"content": content})
        assert response.status_code == 200
        assert response.json()["content"] == content

        # File should be written to disk
        user_md = tmp_path / "USER.md"
        assert user_md.exists()
        assert user_md.read_text(encoding="utf-8") == content

    def test_get_user_profile_after_put(self, agent_client):
        content = "# Profile\n\nI work on data science."
        agent_client.put("/api/user-profile", json={"content": content})

        response = agent_client.get("/api/user-profile")
        assert response.status_code == 200
        assert response.json()["content"] == content

    def test_put_empty_user_profile_returns_none(self, agent_client):
        response = agent_client.put("/api/user-profile", json={"content": ""})
        assert response.status_code == 200
        assert response.json()["content"] is None


class TestAgentsApiDisabled:
    def test_agents_list_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.get("/api/agents")
        assert response.status_code == 403
        assert "agents_api.enabled=true" in response.json()["detail"]

    def test_agent_get_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.get("/api/agents/example-agent")
        assert response.status_code == 403

    def test_agent_name_check_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.get("/api/agents/check", params={"name": "example-agent"})
        assert response.status_code == 403

    def test_agent_create_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.post("/api/agents", json={"name": "example-agent", "soul": "blocked"})
        assert response.status_code == 403

    def test_agent_update_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.put("/api/agents/example-agent", json={"description": "blocked"})
        assert response.status_code == 403

    def test_agent_delete_returns_403(self, disabled_agent_client):
        response = disabled_agent_client.delete("/api/agents/example-agent")
        assert response.status_code == 403

    def test_user_profile_routes_return_403(self, disabled_agent_client):
        get_response = disabled_agent_client.get("/api/user-profile")
        put_response = disabled_agent_client.put("/api/user-profile", json={"content": "blocked"})

        assert get_response.status_code == 403
        assert put_response.status_code == 403


# ===========================================================================
# 9. System default agents layer (`users/default/agents/`)
# ===========================================================================
#
# These tests cover the third priority tier that the agents scan understands:
# the system-default layer visible to every authenticated user. The admin-only
# write guard on the default target is verified via two fixtures:
# - `agent_client` stamps an admin user (can write to both user and default)
# - `regular_agent_client` stamps a regular user (can write to user only)


def _write_layer_agent(base_dir: Path, layer: str, name: str, *, user_id: str | None = None, config: dict | None = None, soul: str = "You are helpful.") -> Path:
    """Write an agent into a specific on-disk layer.

    Args:
        base_dir: tmp_path base for the test.
        layer: ``"user"``, ``"default"``, or ``"legacy"``.
        name: Agent name.
        user_id: Per-user layer only — the user id to nest under.
        config: Optional config overrides; defaults to ``{"name": name}``.
        soul: SOUL.md content.
    """
    if layer == "user":
        assert user_id is not None, "user layer requires user_id"
        agent_dir = base_dir / "users" / user_id / "agents" / name
    elif layer == "default":
        agent_dir = base_dir / "users" / "default" / "agents" / name
    elif layer == "legacy":
        agent_dir = base_dir / "agents" / name
    else:
        raise ValueError(f"Unknown layer: {layer}")
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"name": name, **(config or {})}
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (agent_dir / "SOUL.md").write_text(soul, encoding="utf-8")
    return agent_dir


class TestDefaultLayerScanning:
    """3-tier agent discovery: user > default > legacy."""

    def test_resolve_agent_dir_three_tier(self, tmp_path, agent_client):
        """resolve_agent_dir returns per-user > default > legacy in that order."""
        from deerflow.config.agents_config import resolve_agent_dir

        _write_layer_agent(tmp_path, "legacy", "greeter")
        _write_layer_agent(tmp_path, "default", "greeter")
        # No per-user entry yet — should hit default.

        resolved = resolve_agent_dir("greeter", user_id="alice")
        assert resolved == (tmp_path / "users" / "default" / "agents" / "greeter").resolve()

        # Now add a per-user entry — per-user should win.
        _write_layer_agent(tmp_path, "user", "greeter", user_id="alice")
        resolved = resolve_agent_dir("greeter", user_id="alice")
        assert resolved == (tmp_path / "users" / "alice" / "agents" / "greeter").resolve()

        # A different user still sees the default-layer copy.
        resolved = resolve_agent_dir("greeter", user_id="bob")
        assert resolved == (tmp_path / "users" / "default" / "agents" / "greeter").resolve()

    def test_list_custom_agents_includes_default_layer(self, agent_client):
        """An agent in users/default/agents/ appears in the list response with is_default=true."""
        _write_layer_agent(agent_client._tmp_path, "default", "greeter", config={"description": "sys default"})
        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        agents = {a["name"]: a for a in response.json()["agents"]}
        assert "greeter" in agents
        assert agents["greeter"]["is_default"] is True
        assert agents["greeter"]["description"] == "sys default"

    def test_list_custom_agents_per_user_shadows_default(self, tmp_path):
        """An agent present in both per-user and default layers is listed once with is_default=false."""
        # Use a fresh regular_agent_client-like setup so we know the effective user_id
        # is the regular user's id. We re-use the agent_client fixture shape.
        _write_layer_agent(tmp_path, "default", "shared", config={"description": "default"})
        _write_layer_agent(tmp_path, "user", "shared", user_id="alice", config={"description": "alice override"})

        # Drive a real GET via a custom client that stamps user_id=alice via get_effective_user_id
        import app.gateway.routers.agents as agents_router
        from deerflow.runtime.user_context import set_current_user, reset_current_user

        paths_instance = _make_paths(tmp_path)
        with patch("deerflow.config.agents_config.get_paths", return_value=paths_instance), patch.object(agents_router, "get_paths", return_value=paths_instance):
            set_agents_api_config(AgentsApiConfig(enabled=True))
            try:
                app = _make_test_app(tmp_path, user=_regular_user())
                with TestClient(app) as client:
                    token = set_current_user(SimpleNamespace(id="alice"))
                    try:
                        response = client.get("/api/agents")
                    finally:
                        reset_current_user(token)
            finally:
                set_agents_api_config(AgentsApiConfig(enabled=False))

        assert response.status_code == 200
        shared = next(a for a in response.json()["agents"] if a["name"] == "shared")
        assert shared["is_default"] is False
        assert shared["description"] == "alice override"

    def test_list_custom_agents_default_shadows_legacy(self, agent_client):
        """An agent present in both default and legacy layers is listed once with is_default=true."""
        base = agent_client._tmp_path
        _write_layer_agent(base, "legacy", "shared-legacy-default", config={"description": "legacy"})
        _write_layer_agent(base, "default", "shared-legacy-default", config={"description": "default"})

        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        shared = next(a for a in response.json()["agents"] if a["name"] == "shared-legacy-default")
        assert shared["is_default"] is True
        assert shared["description"] == "default"


class TestDefaultLayerResponse:
    """is_default flag in GET responses."""

    def test_get_agent_returns_is_default_true_for_default_layer(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "default", "greeter", config={"description": "sys default"}, soul="hi from default")
        response = agent_client.get("/api/agents/greeter")
        assert response.status_code == 200
        data = response.json()
        assert data["is_default"] is True
        assert data["soul"] == "hi from default"

    def test_get_agent_returns_is_default_false_for_legacy(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "legacy", "legacy-bot", config={"description": "legacy only"})
        response = agent_client.get("/api/agents/legacy-bot")
        assert response.status_code == 200
        assert response.json()["is_default"] is False

    def test_check_agent_name_taken_by_default(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "default", "taken")
        response = agent_client.get("/api/agents/check", params={"name": "taken"})
        assert response.status_code == 200
        assert response.json() == {"available": False, "name": "taken"}

    def test_check_agent_name_taken_by_legacy(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "legacy", "legacy-taken")
        response = agent_client.get("/api/agents/check", params={"name": "legacy-taken"})
        assert response.status_code == 200
        assert response.json() == {"available": False, "name": "legacy-taken"}

    def test_list_agent_response_is_default_default_false(self, agent_client):
        """Backward-compat: per-user agents still return is_default=false in the list."""
        base = agent_client._tmp_path
        # The default admin user's per-user id is derived from the auth context.
        # We write to legacy so we don't have to guess the user_id.
        _write_layer_agent(base, "legacy", "regular-legacy", config={"description": "legacy"})
        response = agent_client.get("/api/agents")
        assert response.status_code == 200
        regular = next(a for a in response.json()["agents"] if a["name"] == "regular-legacy")
        assert regular["is_default"] is False


class TestDefaultLayerWriteGuards:
    """target=default writes are admin-only; target=user writes remain available to all authed users."""

    def test_create_agent_target_default_as_admin(self, agent_client):
        payload = {"name": "sys-bot", "description": "sys default", "soul": "hi", "target": "default"}
        response = agent_client.post("/api/agents", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["is_default"] is True
        # File landed in the default layer.
        assert (agent_client._tmp_path / "users" / "default" / "agents" / "sys-bot" / "config.yaml").exists()
        # ... and not in the admin user's per-user dir.
        # (We don't know the exact admin user_id, but we can check that the
        # only per-user path under `users/` that matches is the default one.)

    def test_create_agent_target_default_as_regular_user(self, regular_agent_client):
        payload = {"name": "sys-bot", "description": "sys default", "soul": "hi", "target": "default"}
        response = regular_agent_client.post("/api/agents", json=payload)
        assert response.status_code == 403
        # No file in the default layer.
        assert not (regular_agent_client._tmp_path / "users" / "default" / "agents" / "sys-bot").exists()

    def test_create_agent_default_target_omits_field(self, regular_agent_client):
        """Omitting `target` defaults to 'user' — per-user write still works for non-admin."""
        payload = {"name": "my-bot", "description": "user", "soul": "hi"}
        response = regular_agent_client.post("/api/agents", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["is_default"] is False

    def test_update_agent_default_target_as_admin(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "default", "sys-bot", config={"description": "before"}, soul="hi")
        response = agent_client.put(
            "/api/agents/sys-bot",
            json={"description": "after", "target": "default"},
        )
        assert response.status_code == 200
        assert response.json()["is_default"] is True
        assert (base / "users" / "default" / "agents" / "sys-bot" / "config.yaml").read_text(encoding="utf-8").find("after") != -1

    def test_update_agent_default_target_as_regular_user(self, regular_agent_client):
        base = regular_agent_client._tmp_path
        _write_layer_agent(base, "default", "sys-bot", config={"description": "before"}, soul="hi")
        response = regular_agent_client.put(
            "/api/agents/sys-bot",
            json={"description": "after", "target": "default"},
        )
        assert response.status_code == 403

    def test_delete_agent_default_target_as_admin(self, agent_client):
        base = agent_client._tmp_path
        _write_layer_agent(base, "default", "sys-bot")
        response = agent_client.delete("/api/agents/sys-bot", params={"target": "default"})
        assert response.status_code == 204
        assert not (base / "users" / "default" / "agents" / "sys-bot").exists()

    def test_delete_agent_default_target_as_regular_user(self, regular_agent_client):
        base = regular_agent_client._tmp_path
        _write_layer_agent(base, "default", "sys-bot")
        response = regular_agent_client.delete("/api/agents/sys-bot", params={"target": "default"})
        assert response.status_code == 403
        # The file is still there.
        assert (base / "users" / "default" / "agents" / "sys-bot" / "config.yaml").exists()

    def test_create_agent_409_when_name_exists_in_default(self, regular_agent_client):
        """A per-user create with a name already in the default layer is rejected with 409."""
        base = regular_agent_client._tmp_path
        _write_layer_agent(base, "default", "collide")
        payload = {"name": "collide", "description": "user", "soul": "hi"}
        response = regular_agent_client.post("/api/agents", json=payload)
        assert response.status_code == 409

    def test_update_agent_legacy_guard_does_not_fire_for_default(self, agent_client):
        """PUT target=default on an agent that lives in the default layer is allowed (no 409)."""
        base = agent_client._tmp_path
        _write_layer_agent(base, "default", "sys-bot", config={"description": "before"}, soul="hi")
        response = agent_client.put(
            "/api/agents/sys-bot",
            json={"description": "after", "target": "default"},
        )
        assert response.status_code == 200

