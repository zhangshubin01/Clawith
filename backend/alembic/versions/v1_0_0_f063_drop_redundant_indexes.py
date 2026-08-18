"""Drop redundant single-column indexes that are covered by composite/unqiue indexes.

Background:
    A database health audit (duplicate-index check) found 15 public-schema
    indexes whose leading column is fully covered by another index on the same
    table (unique constraints or composite indexes):

    - ix_agent_focus_items_agent_id          covered by uq_agent_focus_items_agent_key (agent_id, key)
    - ix_agent_triggers_agent_id             covered by uq_agent_trigger_name (agent_id, name)
    - ix_channel_configs_agent_id            covered by uq_channel_configs_agent_channel (agent_id, channel_type)
    - ix_chat_messages_conversation_id       covered by ix_chat_messages_conversation_created_id (conversation_id, created_at, id)
    - ix_chat_sessions_agent_id              covered by uq_chat_sessions_agent_ext_conv (agent_id, external_conv_id)
    - ix_chat_sessions_tenant_id             covered by uq_chat_sessions_tenant_id_id (tenant_id, id)
    - ix_company_reports_tenant_id           covered by uq_company_report_period (tenant_id, report_type, period_start, period_end)
    - ix_daily_token_usage_agent_id          covered by uq_daily_token_usage_agent_date (agent_id, date)
    - ix_enterprise_info_tenant_id           covered by uq_enterprise_info_tenant_type (tenant_id, info_type)
    - idx_invitation_codes_code              duplicate of the unique ix_invitation_codes_code
    - ix_member_daily_reports_tenant_id      covered by uq_member_daily_report (tenant_id, member_type, member_id, report_date)
    - ix_org_members_tenant_id               covered by ix_org_members_tenant_user (tenant_id, user_id)
    - ix_published_pages_short_id            duplicate of the unique published_pages_short_id_key
    - ix_trigger_executions_trigger_id       covered by uq_trigger_execution_idempotency (trigger_id, idempotency_key)
    - ix_user_tenant_onboardings_user_id     covered by uq_user_tenant_onboarding (user_id, tenant_id)

Scope:
    - DROP INDEX IF EXISTS only; pure DDL, no row reads or data backfill.
    - Deliberately NOT touching the langgraph_checkpoint schema indexes
      (checkpoint_*_thread_id_idx): that schema is created and managed by the
      LangGraph library itself on startup, not by our migrations.

Downgrade:
    Recreates the dropped btree indexes.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f063_drop_redundant_indexes"
down_revision: Union[str, Sequence[str], None] = "f062_add_skill_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index_name, table_name, column) — one redundant index per entry.
_REDUNDANT_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_agent_focus_items_agent_id", "agent_focus_items", "agent_id"),
    ("ix_agent_triggers_agent_id", "agent_triggers", "agent_id"),
    ("ix_channel_configs_agent_id", "channel_configs", "agent_id"),
    ("ix_chat_messages_conversation_id", "chat_messages", "conversation_id"),
    ("ix_chat_sessions_agent_id", "chat_sessions", "agent_id"),
    ("ix_chat_sessions_tenant_id", "chat_sessions", "tenant_id"),
    ("ix_company_reports_tenant_id", "company_reports", "tenant_id"),
    ("ix_daily_token_usage_agent_id", "daily_token_usage", "agent_id"),
    ("ix_enterprise_info_tenant_id", "enterprise_info", "tenant_id"),
    ("idx_invitation_codes_code", "invitation_codes", "code"),
    ("ix_member_daily_reports_tenant_id", "member_daily_reports", "tenant_id"),
    ("ix_org_members_tenant_id", "org_members", "tenant_id"),
    ("ix_published_pages_short_id", "published_pages", "short_id"),
    ("ix_trigger_executions_trigger_id", "trigger_executions", "trigger_id"),
    ("ix_user_tenant_onboardings_user_id", "user_tenant_onboardings", "user_id"),
)


def upgrade() -> None:
    for index_name, table_name, _ in _REDUNDANT_INDEXES:
        op.drop_index(index_name, table_name=table_name, if_exists=True)


def downgrade() -> None:
    for index_name, table_name, column in _REDUNDANT_INDEXES:
        op.create_index(index_name, table_name, [column], unique=False, if_not_exists=True)
