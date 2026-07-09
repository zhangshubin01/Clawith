"""B5 triage：ACP 工具 RPC 分层超时（已实现于 tool_bridge）。"""
import sys

sys.path.insert(0, "/Users/shubinzhang/Documents/agent/Clawith/backend")

from app.plugins.clawith_acp.tool_bridge import _timeout_for_acp_method


def test_safe_delete_uses_permission_timeout(monkeypatch):
    monkeypatch.setenv("ACP_PERMISSION_TIMEOUT", "120")
    assert _timeout_for_acp_method("fs/safe_delete", {}) == 120.0


def test_read_uses_fs_timeout(monkeypatch):
    monkeypatch.setenv("ACP_FS_TIMEOUT", "15")
    assert _timeout_for_acp_method("fs/read_text_file", {}) == 15.0
