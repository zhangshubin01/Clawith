"""focus_service 只读路径无副作用测试。"""
import sys
import uuid
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services import focus_service


@pytest.mark.asyncio
async def test_list_focus_items_no_write():
  agent_id = uuid.uuid4()
  mock_db = AsyncMock()

  with patch.object(focus_service, "migrate_legacy_focus_file", new_callable=AsyncMock) as migrate:
    with patch.object(focus_service, "_list_focus_items_impl", new_callable=AsyncMock, return_value=[]):
      result = await focus_service.list_focus_items(agent_id, db=mock_db)
      migrate.assert_not_called()
      assert result == []
