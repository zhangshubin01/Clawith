"""tool_execution_policy 单元测试。"""
import sys
sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.services.llm.tool_execution_policy import (
    ToolExecutionMode,
    classify_tool_call,
    detect_path_conflict,
    partition_tool_calls,
)


def _tc(name: str, **args):
    return {"id": f"id-{name}", "function": {"name": name, "arguments": __import__("json").dumps(args)}}


def test_six_read_one_parallel_batch():
    calls = [_tc("read_file", path=f"src/{i}.py") for i in range(6)]
    batches = partition_tool_calls(calls)
    assert len(batches) == 1
    assert batches[0].mode == ToolExecutionMode.PARALLEL
    assert len(batches[0].calls) == 6


def test_same_path_two_reads_one_batch():
    calls = [_tc("read_file", path="src/A.kt"), _tc("read_file", path="src/A.kt")]
    batches = partition_tool_calls(calls)
    assert len(batches) == 1
    assert batches[0].mode == ToolExecutionMode.PARALLEL
    assert len(batches[0].calls) == 2


def test_read_then_write_splits():
    calls = [_tc("read_file", path="a"), _tc("edit_file", path="a")]
    batches = partition_tool_calls(calls)
    assert len(batches) == 2
    assert batches[0].mode == ToolExecutionMode.PARALLEL
    assert batches[1].mode == ToolExecutionMode.SERIAL


def test_detect_path_conflict():
    assert detect_path_conflict(_tc("read_file", path="a/b"), _tc("read_file", path="a/b"))
    assert not detect_path_conflict(_tc("read_file", path="a"), _tc("read_file", path="b"))


def test_execute_command_serial():
    assert classify_tool_call(_tc("execute_command", command="ls")) == ToolExecutionMode.SERIAL


def test_sync_files_serial():
    assert classify_tool_call(_tc("sync_files")) == ToolExecutionMode.SERIAL


def test_unknown_tool_serial():
    assert classify_tool_call(_tc("totally_unknown_tool_xyz")) == ToolExecutionMode.SERIAL


def test_duckduckgo_search_parallel():
    assert classify_tool_call(_tc("duckduckgo_search", query="test")) == ToolExecutionMode.PARALLEL
