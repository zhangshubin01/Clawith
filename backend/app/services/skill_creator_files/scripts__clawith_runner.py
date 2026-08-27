#!/usr/bin/env python3
"""Clawith-native trigger-eval runner (Python stdlib only).

Replaces the Claude Code runner for platforms without the `claude` CLI.
Calls an OpenAI-compatible chat-completions endpoint, asks the model to call
`read_file` on the skill under test when the query matches, and treats that
tool call as the trigger signal.

Configuration comes exclusively from environment variables (never from code):

- CLAWITH_EVAL_BASE_URL  e.g. http://host.docker.internal:8008/v1
- CLAWITH_EVAL_API_KEY   platform API key
- CLAWITH_EVAL_MODEL     model id served by that endpoint

A missing variable raises RunnerConfigError with an actionable message.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_ENV_BASE_URL = "CLAWITH_EVAL_BASE_URL"
_ENV_API_KEY = "CLAWITH_EVAL_API_KEY"
_ENV_MODEL = "CLAWITH_EVAL_MODEL"

_READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from the agent workspace by its "
            "agent-root-relative path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Agent-root-relative file path.",
                },
            },
            "required": ["file_path"],
        },
    },
}


class RunnerConfigError(RuntimeError):
    """The platform runner is not configured; the message says how to fix it."""


def load_config(env: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve runner config from the environment, failing loudly when absent."""
    source = dict(os.environ if env is None else env)
    missing = [key for key in (_ENV_BASE_URL, _ENV_API_KEY, _ENV_MODEL) if not source.get(key)]
    if missing:
        raise RunnerConfigError(
            "clawith runner is not configured; missing environment "
            f"variable(s): {', '.join(missing)}. Set {_ENV_BASE_URL} "
            "(OpenAI-compatible endpoint), "
            f"{_ENV_API_KEY}, and {_ENV_MODEL} to run trigger evals."
        )
    return {
        "base_url": source[_ENV_BASE_URL].rstrip("/"),
        "api_key": source[_ENV_API_KEY],
        "model": source[_ENV_MODEL],
    }


def build_payload(
    config: dict[str, str],
    query: str,
    skill_dir_name: str,
    skill_name: str,
    skill_description: str,
) -> dict:
    """OpenAI-compatible chat-completions payload for one trigger query."""
    system = (
        f"You are evaluating whether a user query should trigger the skill "
        f"'{skill_name}'. That skill lives at the workspace path "
        f"skills/{skill_dir_name}/SKILL.md and is described as:\n"
        f"{skill_description}\n\n"
        f"If the query genuinely matches the skill's purpose, call the "
        f"read_file tool with exactly that skill path. If it does not match, "
        f"reply with a short answer and no tool calls."
    )
    return {
        "model": config["model"],
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "tools": [_READ_FILE_TOOL],
    }


def post_chat(config: dict[str, str], payload: dict, timeout: float = 30) -> dict:
    """POST the payload and return the parsed response body."""
    url = f"{config['base_url']}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise RunnerConfigError(
            f"eval endpoint returned HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RunnerConfigError(
            f"cannot reach eval endpoint {url}: {exc.reason}"
        ) from exc


def detect_trigger(response: dict | None, skill_dir_name: str) -> bool:
    """True when the model called read_file on the skill under test.

    Mirrors the upstream semantics (Skill/Read tool call whose input names the
    skill) using Clawith's own read_file tool and workspace paths.
    """
    message = (response or {}).get("choices", [{}])[0].get("message", {})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        if function.get("name") != "read_file":
            continue
        raw_arguments = function.get("arguments") or "{}"
        if isinstance(raw_arguments, str):
            try:
                raw_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                raw_arguments = {}
        file_path = str(raw_arguments.get("file_path", ""))
        if f"skills/{skill_dir_name}" in file_path:
            return True
    return False


def run_single_query(
    config: dict[str, str],
    query: str,
    skill_dir_name: str,
    skill_name: str,
    skill_description: str,
    timeout: float = 30,
) -> bool:
    """Run one trigger query through the platform endpoint."""
    payload = build_payload(
        config,
        query,
        skill_dir_name,
        skill_name,
        skill_description,
    )
    response = post_chat(config, payload, timeout=timeout)
    return detect_trigger(response, skill_dir_name)


class LLMClientError(RuntimeError):
    """The platform endpoint returned something unusable."""


class ChatClient:
    """Minimal OpenAI-compatible chat client over the platform endpoint.

    Used by improve_description / run_loop to replace the anthropic SDK.
    """

    def __init__(self, config: dict[str, str]) -> None:
        self._config = config

    def complete(
        self,
        *,
        model: str | None = None,
        messages: list[dict],
        max_tokens: int = 16000,
        temperature: float = 0.7,
        timeout: float = 120,
    ) -> str:
        """Return the assistant text of one completion, or raise LLMClientError."""
        payload = {
            "model": model or self._config["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        response = post_chat(self._config, payload, timeout=timeout)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMClientError(
                "unexpected completion response: "
                + json.dumps(response, ensure_ascii=False)[:300]
            ) from exc
        return content or ""
