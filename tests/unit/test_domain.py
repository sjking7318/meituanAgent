from __future__ import annotations

import pytest

from sales_assistant.domain import (
    AgentRun,
    AuthContext,
    Conversation,
    InvalidStateTransitionError,
    ResourceForbiddenError,
    ResourceNotFoundError,
    RunStatus,
)
from sales_assistant.domain.entities import new_id


def test_uuid7_is_time_ordered() -> None:
    ids = [new_id() for _ in range(50)]
    assert ids == sorted(ids), "UUIDv7 should be monotonically increasing"


def test_conversation_access_wrong_tenant() -> None:
    owner = new_id()
    conv = Conversation(tenant_id=new_id(), owner_id=owner)
    context = AuthContext(tenant_id=new_id(), user_id=owner)
    with pytest.raises(ResourceNotFoundError):
        conv.assert_access(context)


def test_conversation_access_wrong_owner() -> None:
    tenant = new_id()
    conv = Conversation(tenant_id=tenant, owner_id=new_id())
    context = AuthContext(tenant_id=tenant, user_id=new_id())
    with pytest.raises(ResourceForbiddenError):
        conv.assert_access(context)


def test_conversation_access_read_any_permission() -> None:
    tenant = new_id()
    conv = Conversation(tenant_id=tenant, owner_id=new_id())
    context = AuthContext(
        tenant_id=tenant,
        user_id=new_id(),
        permissions=frozenset({"conversation:read:any"}),
    )
    conv.assert_access(context)  # no raise


def _run() -> AgentRun:
    return AgentRun(
        tenant_id=new_id(),
        conversation_id=new_id(),
        user_id=new_id(),
        idempotency_key="key-12345678",
        request_fingerprint="fp",
        expected_conversation_version=0,
    )


def test_run_valid_transition() -> None:
    run = _run()
    run.transition_to(RunStatus.RUNNING)
    run.transition_to(RunStatus.SUCCEEDED)
    assert run.status is RunStatus.SUCCEEDED


def test_run_invalid_transition() -> None:
    run = _run()
    with pytest.raises(InvalidStateTransitionError):
        run.transition_to(RunStatus.SUCCEEDED)


def test_run_terminal_is_final() -> None:
    run = _run()
    run.transition_to(RunStatus.CANCELLED)
    with pytest.raises(InvalidStateTransitionError):
        run.transition_to(RunStatus.RUNNING)
