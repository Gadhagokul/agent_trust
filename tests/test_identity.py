from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.security.identity import Identity, resolve_identity


def _req(headers):
    return MagicMock(headers=headers)


def _db():
    return MagicMock()


def _agent() -> dict:
    return {"id": 47, "is_active": True, "approval_status": "approved"}


class TestIdentityResolution:
    def test_admin_role_resolved_from_db(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["admin"]
            identity = resolve_identity(_req({"x-user-id": "25"}), _db())

        assert identity == Identity(user_id=25, role="admin", agent_id=None)

    def test_agent_role_with_matching_agent_id(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = _agent()
            identity = resolve_identity(_req({"x-user-id": "25", "x-agent-id": "47"}), _db())

        assert identity == Identity(user_id=25, role="agent", agent_id=47)

    def test_agent_role_without_agent_id_header(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = _agent()
            identity = resolve_identity(_req({"x-user-id": "25"}), _db())

        assert identity.agent_id == 47

    def test_admin_beats_agent_when_both_present(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent", "admin"]
            identity = resolve_identity(_req({"x-user-id": "25"}), _db())

        assert identity.role == "admin"

    def test_missing_user_id_returns_401(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_identity(_req({}), _db())
        assert exc_info.value.status_code == 401

    def test_non_numeric_user_id_returns_401(self):
        with pytest.raises(HTTPException) as exc_info:
            resolve_identity(_req({"x-user-id": "abc"}), _db())
        assert exc_info.value.status_code == 401

    def test_unknown_role_returns_403(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["customer"]
            with pytest.raises(HTTPException) as exc_info:
                resolve_identity(_req({"x-user-id": "25"}), _db())

        assert exc_info.value.status_code == 403

    def test_agent_without_agent_row_returns_403(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = None
            with pytest.raises(HTTPException) as exc_info:
                resolve_identity(_req({"x-user-id": "25"}), _db())

        assert exc_info.value.status_code == 403

    def test_inactive_agent_returns_403(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = {
                "id": 47,
                "is_active": False,
                "approval_status": "approved",
            }
            with pytest.raises(HTTPException) as exc_info:
                resolve_identity(_req({"x-user-id": "25"}), _db())

        assert exc_info.value.status_code == 403

    def test_mismatched_agent_id_returns_403(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = _agent()
            with pytest.raises(HTTPException) as exc_info:
                resolve_identity(_req({"x-user-id": "25", "x-agent-id": "99"}), _db())

        assert exc_info.value.status_code == 403

    def test_non_numeric_agent_id_returns_403(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.return_value = ["agent"]
            inst.get_agent_for_user.return_value = _agent()
            with pytest.raises(HTTPException) as exc_info:
                resolve_identity(_req({"x-user-id": "25", "x-agent-id": "junk"}), _db())

        assert exc_info.value.status_code == 403

    def test_prod_role_query_failure_is_hard_error(self):
        fake_settings = SimpleNamespace(
            app_env="production", identity_model_type="App\\Models\\User"
        )
        with patch("app.security.identity.get_settings", return_value=fake_settings):
            with patch("app.security.identity.AgentRepository") as mock_cls:
                inst = mock_cls.return_value
                inst.get_user_role.side_effect = RuntimeError("tables missing")
                with pytest.raises(RuntimeError):
                    resolve_identity(_req({"x-user-id": "25"}), _db())


class TestIdentityDevFallback:
    def test_dev_fallback_defaults_to_agent(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.side_effect = RuntimeError("no role tables in seed db")
            inst.get_agent_for_user.return_value = _agent()
            identity = resolve_identity(_req({"x-user-id": "25"}), _db())

        assert identity == Identity(user_id=25, role="agent", agent_id=47)

    def test_dev_fallback_respects_role_header(self):
        with patch("app.security.identity.AgentRepository") as mock_cls:
            inst = mock_cls.return_value
            inst.get_user_role.side_effect = RuntimeError("no role tables in seed db")
            identity = resolve_identity(_req({"x-user-id": "1", "x-user-role": "admin"}), _db())

        assert identity.role == "admin"
