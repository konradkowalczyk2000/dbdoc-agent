"""LLM providers with a common tool-calling loop.

Each provider keeps the conversation in its own native format and exposes:
  run_tools(system, user, tools, handle, final_tool, max_steps) -> dict | None
  complete(system, user) -> str

`tools` is a neutral list of {name, description, parameters (JSON Schema)}.
`handle(name, args) -> (result_text, is_final)` executes a tool call.
"""
from __future__ import annotations

import json
import os

from . import log
from typing import Callable

Handler = Callable[[str, dict], tuple[str, bool]]

NUDGE = "Continue using the tools. When you are done, call {final}."


class BaseLLM:
    label = "none"

    def run_tools(self, system: str, user: str, tools: list[dict], handle: Handler,
                  final_tool: str, max_steps: int) -> dict | None:
        raise NotImplementedError

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Anthropic (Claude)
# --------------------------------------------------------------------------- #
class AnthropicLLM(BaseLLM):
    def __init__(self, model: str | None = None, max_tokens: int = 8000):
        import anthropic  # pip install anthropic

        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.max_tokens = max_tokens
        self.label = f"anthropic:{self.model}"

    def run_tools(self, system, user, tools, handle, final_tool, max_steps):
        atools = [{"name": t["name"], "description": t["description"],
                   "input_schema": t["parameters"]} for t in tools]
        messages: list = [{"role": "user", "content": user}]

        for step in range(max_steps):
            log.detail(f"Waiting for the model (step {step + 1}/{max_steps})", 2)
            extra = {}
            if step == max_steps - 1:  # out of budget: force the final answer
                extra["tool_choice"] = {"type": "tool", "name": final_tool}
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=system,
                tools=atools, messages=messages, **extra,
            )
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": NUDGE.format(final=final_tool)})
                continue

            results, final = [], None
            for call in calls:
                text, is_final = handle(call.name, dict(call.input))
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": text})
                if is_final:
                    final = dict(call.input)
            if final is not None:
                return final
            messages.append({"role": "user", "content": results})
        return None

    def complete(self, system, user):
        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()


# --------------------------------------------------------------------------- #
# OpenAI-compatible (Azure OpenAI / OpenAI)
# --------------------------------------------------------------------------- #
class OpenAICompatibleLLM(BaseLLM):
    def __init__(self, client, model: str, label: str):
        self.client = client
        self.model = model
        self.label = label

    def _create(self, messages, **kwargs):
        return self.client.chat.completions.create(model=self.model, messages=messages, **kwargs)

    def run_tools(self, system, user, tools, handle, final_tool, max_steps):
        otools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
            for t in tools]
        messages: list = [{"role": "system", "content": system},
                          {"role": "user", "content": user}]

        for step in range(max_steps):
            log.detail(f"Waiting for the model (step {step + 1}/{max_steps})", 2)
            extra = {}
            if step == max_steps - 1:
                extra["tool_choice"] = {"type": "function", "function": {"name": final_tool}}
            msg = self._create(messages, tools=otools, **extra).choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                messages.append({"role": "user", "content": NUDGE.format(final=final_tool)})
                continue

            final = None
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    text, is_final = handle(call.function.name, args)
                except json.JSONDecodeError:
                    text, is_final = "ERROR: arguments were not valid JSON.", False
                messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
                if is_final:
                    final = args
            if final is not None:
                return final
        return None

    def complete(self, system, user):
        msg = self._create([{"role": "system", "content": system},
                            {"role": "user", "content": user}]).choices[0].message
        return (msg.content or "").strip()


def azure_openai(deployment: str | None = None) -> OpenAICompatibleLLM:
    from openai import AzureOpenAI  # pip install openai

    deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
    kwargs = dict(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    if os.getenv("AZURE_OPENAI_API_KEY"):
        kwargs["api_key"] = os.environ["AZURE_OPENAI_API_KEY"]
    else:  # Entra ID (az login / managed identity) instead of an API key
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return OpenAICompatibleLLM(AzureOpenAI(**kwargs), deployment, f"azure_openai:{deployment}")


def openai(model: str | None = None) -> OpenAICompatibleLLM:
    from openai import OpenAI

    model = model or os.getenv("OPENAI_MODEL", "gpt-4.1")
    return OpenAICompatibleLLM(OpenAI(), model, f"openai:{model}")


def get_llm(provider: str, model: str | None = None) -> BaseLLM | None:
    if provider == "none":
        return None
    if provider == "anthropic":
        return AnthropicLLM(model)
    if provider == "azure_openai":
        return azure_openai(model)
    if provider == "openai":
        return openai(model)
    raise ValueError(f"Unknown provider: {provider}")
