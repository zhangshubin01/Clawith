"""skill-creator clawith runner：判定纯函数 / 配置错误 / run_eval 分发。"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

FILES_DIR = Path(__file__).resolve().parents[1] / "app" / "services" / "skill_creator_files"


def _load_module(name: str, flat_path: Path, extra: dict[str, types.ModuleType] | None = None):
    for mod_name, mod in (extra or {}).items():
        sys.modules[mod_name] = mod
    spec = importlib.util.spec_from_file_location(name, flat_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    return _load_module("clawith_runner", FILES_DIR / "scripts__clawith_runner.py")


@pytest.fixture(scope="module")
def run_eval_mod(runner):
    scripts_pkg = types.ModuleType("scripts")
    scripts_pkg.__path__ = []  # type: ignore[attr-defined]
    scripts_utils = types.ModuleType("scripts.utils")
    scripts_utils.parse_skill_md = lambda path: ("name", "desc", "content")
    scripts_pkg.utils = scripts_utils
    scripts_pkg.clawith_runner = runner
    sys.modules["scripts"] = scripts_pkg
    sys.modules["scripts.utils"] = scripts_utils
    return _load_module(
        "run_eval_under_test",
        FILES_DIR / "scripts__run_eval.py",
        extra={"scripts": scripts_pkg},
    )


# ── clawith_runner 单元 ──────────────────────────────────────────────────────


def test_load_config_missing_env_raises_actionable_error(runner):
    with pytest.raises(runner.RunnerConfigError) as exc_info:
        runner.load_config(env={})
    message = str(exc_info.value)
    assert "CLAWITH_EVAL_BASE_URL" in message
    assert "CLAWITH_EVAL_API_KEY" in message
    assert "CLAWITH_EVAL_MODEL" in message


def test_load_config_resolves_and_strips_trailing_slash(runner):
    config = runner.load_config(
        env={
            "CLAWITH_EVAL_BASE_URL": "http://platform:8008/v1/",
            "CLAWITH_EVAL_API_KEY": "k",
            "CLAWITH_EVAL_MODEL": "deepseek-v4-flash",
        }
    )
    assert config["base_url"] == "http://platform:8008/v1"
    assert config["model"] == "deepseek-v4-flash"


def test_detect_trigger_true_when_read_file_names_skill(runner):
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps(
                                    {"file_path": "skills/risk-review/SKILL.md"}
                                ),
                            }
                        }
                    ]
                }
            }
        ]
    }
    assert runner.detect_trigger(response, "risk-review") is True


def test_detect_trigger_false_for_other_tool_or_path(runner):
    other_tool = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "x"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    other_path = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read_file",
                                "arguments": '{"file_path": "skills/other/SKILL.md"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    no_tools = {"choices": [{"message": {"content": "no tools here"}}]}
    assert runner.detect_trigger(other_tool, "risk-review") is False
    assert runner.detect_trigger(other_path, "risk-review") is False
    assert runner.detect_trigger(no_tools, "risk-review") is False
    assert runner.detect_trigger(None, "risk-review") is False


def test_post_chat_happy_path_and_http_error(runner, monkeypatch):
    class _Resp:
        def __init__(self, body): self._body = body

        def __enter__(self): return self

        def __exit__(self, *a): return False

        def read(self): return self._body

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Resp(json.dumps({"choices": []}).encode())

    monkeypatch.setattr(runner.urllib.request, "urlopen", fake_urlopen)

    result = runner.post_chat(
        {"base_url": "http://platform/v1", "api_key": "k", "model": "m"},
        {"model": "m", "messages": []},
        timeout=11,
    )
    assert result == {"choices": []}
    assert captured["url"] == "http://platform/v1/chat/completions"
    assert captured["timeout"] == 11

    def raise_http(request, timeout):
        raise urllib.error.HTTPError("u", 401, "Unauthorized", None, None)

    monkeypatch.setattr(runner.urllib.request, "urlopen", raise_http)
    with pytest.raises(runner.RunnerConfigError, match="HTTP 401"):
        runner.post_chat(
            {"base_url": "http://platform/v1", "api_key": "bad", "model": "m"},
            {"model": "m", "messages": []},
        )


# ── run_eval 分发 ─────────────────────────────────────────────────────────────


class _SyncFuture:
    def __init__(self, fn, args):
        self._fn = fn
        self._args = args
        self._ran = False
        self._value = None
        self._exc = None

    def result(self):
        if not self._ran:
            self._ran = True
            try:
                self._value = self._fn(*self._args)
            except Exception as exc:  # noqa: BLE001 - 复现进程池语义
                self._exc = exc
        if self._exc is not None:
            raise self._exc
        return self._value


class _SyncExecutor:
    def __init__(self, *args, **kwargs):
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args):
        future = _SyncFuture(fn, args)
        self.futures.append(future)
        return future


def _install_sync_pool(run_eval_mod, monkeypatch):
    monkeypatch.setattr(run_eval_mod, "ProcessPoolExecutor", _SyncExecutor)
    monkeypatch.setattr(run_eval_mod, "as_completed", lambda futures: iter(futures))


def _eval_set():
    return [
        {"query": "check release risks", "should_trigger": True},
        {"query": "what time is it", "should_trigger": False},
    ]


def test_run_eval_clawith_runner_aborts_on_config_error(run_eval_mod, runner, monkeypatch):
    _install_sync_pool(run_eval_mod, monkeypatch)

    def raise_config(env=None):
        raise runner.RunnerConfigError("missing CLAWITH_EVAL_BASE_URL")

    monkeypatch.setattr(run_eval_mod, "_load_config", raise_config)

    with pytest.raises(run_eval_mod.EvalAbortError, match="missing CLAWITH_EVAL_BASE_URL"):
        run_eval_mod.run_eval(
            eval_set=_eval_set(),
            skill_name="Risk Review",
            description="Check release risks",
            num_workers=2,
            timeout=30,
            project_root=Path("/tmp"),
            runner="clawith",
            skill_dir_name="risk-review",
        )


def test_run_eval_clawith_runner_uses_platform_queries(run_eval_mod, runner, monkeypatch):
    _install_sync_pool(run_eval_mod, monkeypatch)
    seen = []

    def fake_load_config(env=None):
        return {"base_url": "http://x/v1", "api_key": "k", "model": "m"}

    def fake_run_single_query(config, query, skill_dir_name, skill_name, skill_description, timeout=30):
        seen.append((query, skill_dir_name, skill_name))
        return query == "check release risks"

    monkeypatch.setattr(run_eval_mod, "_load_config", fake_load_config)
    monkeypatch.setattr(run_eval_mod, "_runner_single_query", fake_run_single_query)

    output = run_eval_mod.run_eval(
        eval_set=_eval_set(),
        skill_name="Risk Review",
        description="Check release risks",
        num_workers=2,
        timeout=30,
        project_root=Path("/tmp"),
        runner="clawith",
        skill_dir_name="risk-review",
    )

    assert sorted(q for q, _, _ in seen) == ["check release risks", "what time is it"]
    assert output["summary"] == {"total": 2, "passed": 2, "failed": 0}


def test_run_eval_claude_runner_preserves_original_worker(run_eval_mod, monkeypatch):
    _install_sync_pool(run_eval_mod, monkeypatch)
    seen = []

    def fake_run_single_query(query, skill_name, skill_description, timeout, project_root, model):
        seen.append((query, model))
        return True

    monkeypatch.setattr(run_eval_mod, "run_single_query", fake_run_single_query)

    run_eval_mod.run_eval(
        eval_set=_eval_set(),
        skill_name="Risk Review",
        description="desc",
        num_workers=1,
        timeout=30,
        project_root=Path("/tmp"),
        runner="claude",
        model="sonnet",
    )

    assert seen == [("check release risks", "sonnet"), ("what time is it", "sonnet")]


def test_run_eval_unknown_runner_aborts(run_eval_mod):
    with pytest.raises(run_eval_mod.EvalAbortError, match="Unknown runner"):
        run_eval_mod.run_eval(
            eval_set=_eval_set(),
            skill_name="Risk Review",
            description="desc",
            num_workers=1,
            timeout=30,
            project_root=Path("/tmp"),
            runner="bogus",
        )


# ── 种子注册 ─────────────────────────────────────────────────────────────────


def test_skill_creator_seed_includes_clawith_runner():
    from app.services.skill_creator_content import _FILE_MAP, get_skill_creator_files

    assert _FILE_MAP["scripts__clawith_runner.py"] == "scripts/clawith_runner.py"
    files = get_skill_creator_files()
    runner_file = next(f for f in files if f["path"] == "scripts/clawith_runner.py")
    assert "load_config" in runner_file["content"]
    assert "CLAWITH_EVAL_BASE_URL" in runner_file["content"]
