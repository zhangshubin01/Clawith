"""skill-creator 描述优化：去 anthropic 后走 OpenAI 兼容 client。"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

FILES_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "skill_creator_files"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location(
        "clawith_runner", FILES_DIR / "scripts__clawith_runner.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["clawith_runner"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def improve_mod(runner):
    scripts_pkg = types.ModuleType("scripts")
    scripts_pkg.__path__ = []  # type: ignore[attr-defined]
    scripts_utils = types.ModuleType("scripts.utils")
    scripts_utils.parse_skill_md = lambda path: ("name", "desc", "content")
    scripts_pkg.utils = scripts_utils
    scripts_pkg.clawith_runner = runner
    sys.modules["scripts"] = scripts_pkg
    sys.modules["scripts.utils"] = scripts_utils
    spec = importlib.util.spec_from_file_location(
        "improve_under_test", FILES_DIR / "scripts__improve_description.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["improve_under_test"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def complete(self, *, model, messages, max_tokens, temperature=None, timeout=None):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
        )
        return self._replies.pop(0)


def _eval_results() -> dict:
    return {
        "results": [
            {"query": "check release risks", "should_trigger": True, "pass": False, "triggers": 0, "runs": 3},
            {"query": "unrelated", "should_trigger": False, "pass": True, "triggers": 0, "runs": 3},
        ],
        "summary": {"passed": 1, "total": 2, "failed": 1},
    }


def test_improve_description_uses_openai_compatible_complete_and_parses_tags(improve_mod):
    client = _FakeClient(["<new_description>Use this skill to review release risk.</new_description>"])
    description = improve_mod.improve_description(
        client=client,
        skill_name="Risk Review",
        skill_content="# Skill body",
        current_description="old description",
        eval_results=_eval_results(),
        history=[],
        model="deepseek-v4-flash",
    )
    assert description == "Use this skill to review release risk."
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["messages"][0]["role"] == "system"
    assert "<current_description>" in call["messages"][1]["content"]
    assert "FAILED TO TRIGGER" in call["messages"][1]["content"]


def test_improve_description_shortens_when_over_limit(improve_mod):
    long_description = "<new_description>" + "x" * 1100 + "</new_description>"
    client = _FakeClient([long_description, "<new_description>short and useful</new_description>"])
    description = improve_mod.improve_description(
        client=client,
        skill_name="Risk Review",
        skill_content="# Skill body",
        current_description="old",
        eval_results=_eval_results(),
        history=[],
        model="m",
    )
    assert description == "short and useful"
    assert len(client.calls) == 2
    assert "1024" in client.calls[1]["messages"][-1]["content"]


def test_improve_description_no_tags_falls_back_to_full_text(improve_mod):
    client = _FakeClient(["plain description text without tags"])
    description = improve_mod.improve_description(
        client=client,
        skill_name="Risk Review",
        skill_content="# Skill body",
        current_description="old",
        eval_results=_eval_results(),
        history=[],
        model="m",
    )
    assert description == "plain description text without tags"


def test_chat_client_complete_parses_content(runner, monkeypatch):
    import json

    captured = {}

    class _Resp:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "hi there"}}]}).encode()

    monkeypatch.setattr(
        runner.urllib.request,
        "urlopen",
        lambda request, timeout: captured.update(timeout=timeout) or _Resp(),
    )

    client = runner.ChatClient({"base_url": "http://x/v1", "api_key": "k", "model": "default"})
    text = client.complete(messages=[{"role": "user", "content": "hello"}])
    assert text == "hi there"
    assert captured["timeout"] == 120


def test_chat_client_complete_malformed_response_raises(runner, monkeypatch):
    import json

    class _Resp:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self):
            return json.dumps({"unexpected": True}).encode()

    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda request, timeout: _Resp())

    client = runner.ChatClient({"base_url": "http://x/v1", "api_key": "k", "model": "m"})
    with pytest.raises(runner.LLMClientError, match="unexpected completion response"):
        client.complete(messages=[])


def test_chat_client_complete_empty_content_raises(runner, monkeypatch):
    import json

    class _Resp:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "  "}}]}).encode()

    monkeypatch.setattr(runner.urllib.request, "urlopen", lambda request, timeout: _Resp())

    client = runner.ChatClient({"base_url": "http://x/v1", "api_key": "k", "model": "m"})
    with pytest.raises(runner.LLMClientError, match="empty content"):
        client.complete(messages=[])
