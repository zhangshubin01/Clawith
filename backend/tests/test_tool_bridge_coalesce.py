"""tool_bridge coalesce 单元测试。"""
import asyncio
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

import pytest

from app.plugins.clawith_acp import tool_bridge


@pytest.mark.asyncio
async def test_coalesce_search_text_diff_query_not_merged():
  """search_text 已移出 coalesce，不同 query 必须独立执行。"""
  calls = []

  async def exec_a():
    calls.append("a")
    await asyncio.sleep(0.05)
    return "result-a"

  async def exec_b():
    calls.append("b")
    await asyncio.sleep(0.05)
    return "result-b"

  r1, r2 = await asyncio.gather(
    tool_bridge.coalesce_or_execute("fs/search_text", "/proj", "sess1", exec_a, args={"query": "foo"}),
    tool_bridge.coalesce_or_execute("fs/search_text", "/proj", "sess1", exec_b, args={"query": "bar"}),
  )
  assert r1 == "result-a"
  assert r2 == "result-b"
  assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_coalesce_read_text_file_same_path_merged():
  calls = []

  async def exec_once():
    calls.append(1)
    await asyncio.sleep(0.05)
    return "ok"

  r1, r2 = await asyncio.gather(
    tool_bridge.coalesce_or_execute("fs/read_text_file", "/a.kt", "sess2", exec_once),
    tool_bridge.coalesce_or_execute("fs/read_text_file", "/a.kt", "sess2", exec_once),
  )
  assert r1 == "ok"
  assert r2 == "ok"
  assert len(calls) == 1


@pytest.mark.asyncio
async def test_coalesce_read_with_line_not_merged():
  calls = []

  async def exec_a():
    calls.append("a")
    return "a"

  async def exec_b():
    calls.append("b")
    return "b"

  await asyncio.gather(
    tool_bridge.coalesce_or_execute(
      "fs/read_text_file", "/a.kt", "sess3", exec_a, args={"line": 1},
    ),
    tool_bridge.coalesce_or_execute(
      "fs/read_text_file", "/a.kt", "sess3", exec_b, args={"line": 10},
    ),
  )
  assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_coalesce_search_text_same_query_merged():
    calls = []

    async def exec_once():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "ok"

    args = {"query": "same", "filePattern": "*.kt", "regex": True, "caseSensitive": True}
    r1, r2 = await asyncio.gather(
        tool_bridge.coalesce_or_execute("fs/search_text", "", "sess4", exec_once, args=args),
        tool_bridge.coalesce_or_execute("fs/search_text", "", "sess4", exec_once, args=args),
    )
    assert r1 == "ok"
    assert r2 == "ok"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_coalesce_find_file_same_query_merged():
    calls = []

    async def exec_once():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "ok"

    args = {"query": "Foo.kt", "scope": "project_files", "pageSize": 25}
    r1, r2 = await asyncio.gather(
        tool_bridge.coalesce_or_execute("fs/find_file", "", "sess5", exec_once, args=args),
        tool_bridge.coalesce_or_execute("fs/find_file", "", "sess5", exec_once, args=args),
    )
    assert r1 == "ok"
    assert r2 == "ok"
    assert len(calls) == 1
