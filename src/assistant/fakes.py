"""Deterministic stand-ins for a real model: used by the tests and by the
offline mode of evals/assistant/run.py. No network."""
from __future__ import annotations

import copy
import json

from assistant.harness import JSONProtocolChat, ModelError, ModelTurn, ToolCall


def call(name: str, **args) -> ModelTurn:
    return ModelTurn(tool_calls=[ToolCall(f"call_{name}", name, args)])


def say(text: str) -> ModelTurn:
    return ModelTurn(text=text)


class ScriptedModel:
    """Returns the scripted turns in order (a callable item gets
    (system, history, specs) and returns a ModelTurn; an Exception item is
    raised). Records every call for assertions."""

    native_tools = True
    tool_mode = "native"
    ident = "scripted"

    def __init__(self, script, loop_last: bool = False):
        self.script = list(script)
        self.loop_last = loop_last
        self.calls: list[dict] = []

    def complete(self, system, history, specs):
        self.calls.append({"system": system, "history": copy.deepcopy(history),
                           "specs": [s["name"] for s in specs]})
        if not self.script:
            return ModelTurn(text="(no more scripted turns)")
        item = self.script[0] if (self.loop_last and len(self.script) == 1) else self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(system, history, specs)
        return copy.deepcopy(item)


class ScriptedText:
    """A text-only backend (no native tools) for exercising JSONProtocolChat:
    returns scripted raw strings."""

    native_tools = False
    ident = "scripted-text"

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls: list[dict] = []

    def complete(self, system, history, specs):
        if specs:
            raise ModelError("text backend got native tools")
        self.calls.append({"system": system, "history": copy.deepcopy(history)})
        return ModelTurn(text=self.texts.pop(0) if self.texts else '{"reply": "(end)"}')


def json_mode(texts) -> JSONProtocolChat:
    """A JSON-protocol model whose raw replies are `texts` (strings or dicts)."""
    raw = [t if isinstance(t, str) else json.dumps(t) for t in texts]
    model = JSONProtocolChat(ScriptedText(raw))
    model.tool_mode = "json"
    return model
