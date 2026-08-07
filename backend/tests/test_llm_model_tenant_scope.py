import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.enterprise import _llm_management_tenant_id, _llm_model_scope
from app.models.llm import LLMModel


def _user(tenant_id: uuid.UUID, role: str = "org_admin") -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, role=role)


def test_org_admin_cannot_select_another_tenant_for_llm_models() -> None:
    user = _user(uuid.uuid4())

    with pytest.raises(HTTPException, match="Cannot manage another tenant's models") as error:
        _llm_management_tenant_id(user, str(uuid.uuid4()))

    assert error.value.status_code == 403


def test_platform_admin_can_select_another_tenant_for_llm_models() -> None:
    target_tenant_id = uuid.uuid4()

    assert _llm_management_tenant_id(_user(uuid.uuid4(), "platform_admin"), str(target_tenant_id)) == target_tenant_id


def test_org_admin_model_mutation_query_is_tenant_scoped() -> None:
    tenant_id = uuid.uuid4()
    statement = _llm_model_scope(uuid.uuid4(), _user(tenant_id))
    where_clause = " ".join(str(criteria) for criteria in statement._where_criteria)

    assert "llm_models.tenant_id" in where_clause
    assert tenant_id.hex in str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_platform_admin_model_mutation_query_is_not_tenant_scoped() -> None:
    statement = _llm_model_scope(uuid.uuid4(), _user(uuid.uuid4(), "platform_admin"))
    where_clause = " ".join(str(criteria) for criteria in statement._where_criteria)

    assert "llm_models.tenant_id" not in where_clause


def test_list_llm_models_query_accepts_uuid_tenant_id() -> None:
    """Regression: _llm_management_tenant_id returns a uuid.UUID; re-wrapping it
    with uuid.UUID(tid) raised AttributeError ('UUID' object has no attribute
    'replace'), 500-ing GET /api/enterprise/llm-models on every enterprise page
    load. The tenant filter must accept the UUID directly."""
    tid = _llm_management_tenant_id(
        _user(uuid.uuid4(), "platform_admin"),
        str(uuid.uuid4()),
    )
    assert isinstance(tid, uuid.UUID)

    # 旧实现 uuid.UUID(tid) 会抛 AttributeError；新实现直接使用 UUID
    query = select(LLMModel).where(LLMModel.deleted_at.is_(None))
    query = query.where(LLMModel.tenant_id == tid)

    where_clause = " ".join(str(criteria) for criteria in query._where_criteria)
    assert "llm_models.tenant_id" in where_clause
    assert tid.hex in str(query.compile(compile_kwargs={"literal_binds": True}))
