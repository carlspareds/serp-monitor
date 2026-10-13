"""Provider-agnostic tool-calling loop for the in-app assistant.

    run_turn(model, ctx, messages, session_key=...) -> TurnResult

One user turn = up to MAX_STEPS model calls. Each call either answers
(text) or asks for tools; read tools run and their results go back to the
model, navigate tools become UI intents, action tools become PENDING
confirmations (pending.STORE) and are never executed here.

Model adapters (all return ModelTurn(text, tool_calls)):
  OpenAIChat   -- OpenAI-compatible chat completions, `tools`/`tool_calls`.
  GeminiChat   -- Gemini generateContent, functionDeclarations/functionCall.
  JSONProtocolChat -- wraps either one WITHOUT native tools: the model
                  replies {"tool": .., "args": ..} or {"reply": ..}, parsed
                  defensively. Used when the provider/model rejects tools
                  (auto-detected, then remembered per provider+model) or
                  when ASSISTANT_TOOL_MODE=json.

Provider config comes from processing/llm.py (same env vars as the rest of
the app). Every string leaving this module goes through scrub_secrets.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests

from assistant import cards, prompts, tools
from assistant.pending import STORE, PendingStore
from common.redact import redact
from processing import ai_status, llm

log = logging.getLogger("assistant")

MAX_STEPS = 6
MAX_CALLS_PER_STEP = 4
MAX_PROPOSALS_PER_TURN = 3
REQUEST_TIMEOUT = 45
TURN_BUDGET_S = 100
# Reasoning-safe (see llm.LONG_OUTPUT_TOKENS): reasoning models spend part of
# it thinking; an empty finish_reason=length answer is retried once doubled.
MAX_OUTPUT_TOKENS = llm.LONG_OUTPUT_TOKENS
MAX_TOOL_RESULT_CHARS = 6000
MAX_HISTORY_MESSAGES = 20
MAX_MESSAGE_CHARS = 4000
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASS|PASSWORD|WEBHOOK|DSN|CREDENTIAL)", re.IGNORECASE)


# --------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------- #
def secret_values() -> list[str]:
    """Values of env vars whose NAME looks secret (>= 6 chars), plus any
    password embedded in OPENAI_BASE_URL. Longest first."""
    vals = {v.strip() for k, v in os.environ.items()
            if _SECRET_NAME.search(k) and v and len(v.strip()) >= 6}
    vals.update(s for s in llm._secrets() if s and len(s) >= 6)
    return sorted(vals, key=len, reverse=True)


def scrub_secrets(text: str, secrets: list[str] | None = None) -> str:
    if not text:
        return text
    for s in secrets if secrets is not None else secret_values():
        if s in text:
            text = text.replace(s, "<secret>")
    # Key-like shapes (sk-..., apify_api_..., bot tokens) even when they are
    # not the value of a configured variable -- see common.redact.
    return redact(text, [])


def scrub_obj(obj, secrets: list[str] | None = None):
    secrets = secret_values() if secrets is None else secrets
    if isinstance(obj, str):
        return scrub_secrets(obj, secrets)
    if isinstance(obj, list):
        return [scrub_obj(v, secrets) for v in obj]
    if isinstance(obj, dict):
        return {k: scrub_obj(v, secrets) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------- #
# Model plumbing
# --------------------------------------------------------------------- #
class ModelError(Exception):
    """Plain, secret-free message about a failed model call."""


class ToolsUnsupported(ModelError):
    """The provider/model rejected native tool calling."""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (TypeError, ValueError):
        found = _first_json_object(str(raw))
        return found if isinstance(found, dict) else {}


def _http_error(status: int, body: str, with_tools: bool, url: str = "") -> ModelError:
    low = (body or "").lower()
    if with_tools and status in (400, 404, 405, 422, 500, 501) and (
            "tool" in low or "function" in low):
        return ToolsUnsupported(f"HTTP {status}: tools not supported")
    # Plain message only: the provider's raw JSON body never reaches the user.
    return ModelError(scrub_secrets(llm.describe_http_error(status, body or "", url)))


def _post(url: str, payload: dict, headers: dict, with_tools: bool, timeout: int,
          send=None) -> dict:
    try:
        if send is not None:
            resp = send(url, payload, headers, timeout)
        else:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        msg = llm.describe_request_error(e, url, "Check Settings → AI provider.")
        raise ModelError(scrub_secrets(msg or "Couldn't reach the AI provider.")) from None
    if resp.status_code >= 400:
        raise _http_error(resp.status_code, resp.text or "", with_tools, url)
    try:
        return resp.json()
    except ValueError:
        raise ModelError("The AI provider returned a response that isn't JSON.") from None


class OpenAIChat:
    native_tools = True

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: int = REQUEST_TIMEOUT, max_tokens: int = MAX_OUTPUT_TOKENS):
        self.base_url, self.model, self.api_key = base_url.rstrip("/"), model, api_key
        self.timeout, self.max_tokens = timeout, max_tokens
        self.ident = f"openai:{model}@{self.base_url}"

    def _messages(self, system: str, history: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for h in history:
            if h["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": h["tool_call_id"], "content": h["content"]})
            elif h["role"] == "assistant" and h.get("tool_calls"):
                out.append({"role": "assistant", "content": h.get("content") or None,
                            "tool_calls": [{"id": c.id, "type": "function",
                                            "function": {"name": c.name,
                                                         "arguments": json.dumps(c.args)}}
                                           for c in h["tool_calls"]]})
            else:
                out.append({"role": h["role"], "content": h.get("content") or ""})
        return out

    def _send(self, url: str, payload: dict, headers: dict, timeout: int):
        # max_tokens/max_completion_tokens + reasoning_effort adaptation,
        # shared with processing/llm.call_openai.
        return llm.post_chat_completion(url, payload, headers, timeout, base_url=self.base_url,
                                        model_name=self.model, tools_sent="tools" in payload)

    def complete(self, system: str, history: list[dict], specs: list[dict]) -> ModelTurn:
        headers = {"Content-Type": "application/json", **llm.routing_headers(self.base_url)}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        budget, retried, first_text = self.max_tokens, False, ""
        while True:
            payload = {"model": self.model, "messages": self._messages(system, history),
                       "max_tokens": budget, "temperature": 0.2}
            if specs:
                payload["tools"] = [{"type": "function", "function": s} for s in specs]
                payload["tool_choice"] = "auto"
            data = _post(f"{self.base_url}/chat/completions", payload, headers, bool(specs),
                         self.timeout, send=self._send)
            try:
                choice = data["choices"][0]
                msg = choice["message"]
            except (KeyError, IndexError, TypeError):
                raise ModelError("The AI provider returned an empty answer.") from None
            calls = []
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                if fn.get("name"):
                    calls.append(ToolCall(tc.get("id") or f"call_{i}", fn["name"],
                                          _parse_args(fn.get("arguments"))))
            text = llm.message_text(msg)
            if not calls and choice.get("finish_reason") == "length" and not retried:
                bigger = llm.retry_budget(budget)
                if bigger:
                    budget, retried, first_text = bigger, True, text
                    continue
            text = text or first_text
            if not calls and not text:
                raise ModelError(llm.EMPTY_ANSWER)
            return ModelTurn(text=text, tool_calls=calls)


def gemini_schema(schema: dict) -> dict:
    """JSON schema -> Gemini's OpenAPI subset (no additionalProperties etc.)."""
    out = {}
    for key in ("type", "description", "enum", "format"):
        if key in schema:
            out[key] = schema[key]
    if "items" in schema:
        out["items"] = gemini_schema(schema["items"])
    if schema.get("properties"):
        out["properties"] = {k: gemini_schema(v) for k, v in schema["properties"].items()}
        if schema.get("required"):
            out["required"] = list(schema["required"])
    return out


class GeminiChat:
    native_tools = True

    def __init__(self, model: str, api_key: str, timeout: int = REQUEST_TIMEOUT,
                 max_tokens: int = MAX_OUTPUT_TOKENS, base: str = GEMINI_BASE):
        self.model, self.api_key, self.timeout, self.max_tokens = model, api_key, timeout, max_tokens
        self.base = base
        self.ident = f"gemini:{model}"

    @staticmethod
    def _contents(history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for h in history:
            if h["role"] == "tool":
                try:
                    resp = json.loads(h["content"])
                except (TypeError, ValueError):
                    resp = None
                if not isinstance(resp, dict):
                    resp = {"result": h["content"]}
                part = {"functionResponse": {"name": h["name"], "response": resp}}
                if out and out[-1]["role"] == "user" and "functionResponse" in out[-1]["parts"][0]:
                    out[-1]["parts"].append(part)
                else:
                    out.append({"role": "user", "parts": [part]})
            elif h["role"] == "assistant":
                parts = [{"text": h["content"]}] if h.get("content") else []
                parts += [{"functionCall": {"name": c.name, "args": c.args}}
                          for c in h.get("tool_calls") or []]
                out.append({"role": "model", "parts": parts or [{"text": ""}]})
            else:
                out.append({"role": "user", "parts": [{"text": h.get("content") or ""}]})
        return out

    def complete(self, system: str, history: list[dict], specs: list[dict]) -> ModelTurn:
        turn, truncated = self._complete_once(system, history, specs, self.max_tokens)
        bigger = llm.retry_budget(self.max_tokens)
        if truncated and not turn.tool_calls and bigger:
            # MAX_TOKENS (a thinking model can spend it all before answering):
            # one retry with a doubled budget; keep the first text if the
            # retry brings nothing better.
            retry, _ = self._complete_once(system, history, specs, bigger)
            if retry.text or retry.tool_calls:
                turn = retry
        if not (turn.text or turn.tool_calls):
            raise ModelError(llm.EMPTY_ANSWER)
        return turn

    def _complete_once(self, system: str, history: list[dict], specs: list[dict],
                       budget: int) -> tuple[ModelTurn, bool]:
        payload = {"systemInstruction": {"parts": [{"text": system}]},
                   "contents": self._contents(history),
                   "generationConfig": {"temperature": 0.2, "maxOutputTokens": budget}}
        if specs:
            decls = []
            for s in specs:
                d = {"name": s["name"], "description": s["description"]}
                params = gemini_schema(s["parameters"])
                if params.get("properties"):
                    d["parameters"] = params
                decls.append(d)
            payload["tools"] = [{"functionDeclarations": decls}]
        # Key in a header, never in the URL (URLs end up in exception text).
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        data = _post(f"{self.base}/{self.model}:generateContent", payload, headers,
                     bool(specs), self.timeout)
        try:
            cand = data["candidates"][0]
        except (KeyError, IndexError, TypeError):
            reason = ((data or {}).get("promptFeedback") or {}).get("blockReason")
            raise ModelError("The AI provider returned no answer"
                             + (f" (blocked: {reason})." if reason else ".")) from None
        if cand.get("finishReason") == "MALFORMED_FUNCTION_CALL" and specs:
            raise ToolsUnsupported("malformed function call")
        texts, calls = [], []
        for i, part in enumerate((cand.get("content") or {}).get("parts") or []):
            if "functionCall" in part:
                fc = part["functionCall"] or {}
                if fc.get("name"):
                    calls.append(ToolCall(f"g{i}", fc["name"], _parse_args(fc.get("args"))))
            elif part.get("text") and not part.get("thought"):
                texts.append(part["text"])
        turn = ModelTurn(text="".join(texts).strip(), tool_calls=calls)
        return turn, cand.get("finishReason") == "MAX_TOKENS"


_DECODER = json.JSONDecoder()


def _first_json_object(text: str):
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = _DECODER.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_json_protocol(text: str) -> ModelTurn:
    """{"tool": name, "args": {...}} -> tool call; {"reply": "..."} ->
    text; anything else -> the raw text as the reply (never raises)."""
    raw = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.MULTILINE).strip()
    obj = _first_json_object(cleaned)
    if isinstance(obj, dict):
        name = obj.get("tool") or obj.get("name") or obj.get("function")
        if isinstance(name, str) and name.strip():
            args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
            return ModelTurn(tool_calls=[ToolCall("json_0", name.strip(), _parse_args(args))])
        for key in ("reply", "answer", "message", "response", "text"):
            if isinstance(obj.get(key), str):
                return ModelTurn(text=obj[key].strip())
    return ModelTurn(text=cleaned)


class JSONProtocolChat:
    native_tools = False

    def __init__(self, inner):
        self.inner = inner
        self.ident = getattr(inner, "ident", "model")

    @staticmethod
    def flatten(history: list[dict]) -> list[dict]:
        out: list[dict] = []
        for h in history:
            if h["role"] == "tool":
                role, content = "user", f"TOOL_RESULT {h['name']}: {h['content']}"
            elif h["role"] == "assistant" and h.get("tool_calls"):
                c = h["tool_calls"][0]
                role, content = "assistant", json.dumps({"tool": c.name, "args": c.args})
            else:
                role, content = h["role"], h.get("content") or ""
            if out and out[-1]["role"] == role:
                out[-1]["content"] += "\n\n" + content
            else:
                out.append({"role": role, "content": content})
        return out

    def complete(self, system: str, history: list[dict], specs: list[dict]) -> ModelTurn:
        sys_prompt = system + "\n\n" + prompts.json_protocol_prompt(specs)
        turn = self.inner.complete(sys_prompt, self.flatten(history), [])
        if turn.tool_calls:      # a native call slipped through anyway
            return turn
        return parse_json_protocol(turn.text)


_JSON_MODE_IDENTS: set[str] = set()


class AutoModel:
    """Native tools first; on ToolsUnsupported switch to the JSON protocol
    (remembered per provider+model for the life of the process)."""

    def __init__(self, inner, mode: str = "auto"):
        self.inner, self.mode = inner, mode
        self.ident = getattr(inner, "ident", "model")

    @property
    def tool_mode(self) -> str:
        if self.mode == "json" or (self.mode == "auto" and self.ident in _JSON_MODE_IDENTS):
            return "json"
        return "native"

    def complete(self, system: str, history: list[dict], specs: list[dict]) -> ModelTurn:
        if self.tool_mode == "json":
            return JSONProtocolChat(self.inner).complete(system, history, specs)
        try:
            return self.inner.complete(system, history, specs)
        except ToolsUnsupported:
            if self.mode == "native":
                raise ModelError("This model doesn't support tool calling. "
                                 "Set ASSISTANT_TOOL_MODE=json or pick another model.") from None
            log.info("assistant_tools_unsupported_fallback_json", extra={"model": self.ident})
            _JSON_MODE_IDENTS.add(self.ident)
            return JSONProtocolChat(self.inner).complete(system, history, specs)


def build_model():
    """The configured provider as an AutoModel, or None when no provider is
    configured (llm.not_configured_reason())."""
    if llm.not_configured_reason():
        return None
    mode = (os.getenv("ASSISTANT_TOOL_MODE") or "auto").strip().lower()
    if mode not in ("auto", "native", "json"):
        mode = "auto"
    if llm.provider() == "openai":
        inner = OpenAIChat(llm.openai_base_url(), llm.openai_model(),
                           (os.getenv("OPENAI_API_KEY") or "").strip())
    else:
        inner = GeminiChat(llm.gemini_model(), (os.getenv("GEMINI_API_KEY") or "").strip())
    return AutoModel(inner, mode)


# --------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------- #
@dataclass
class TurnResult:
    reply: str
    events: list[dict] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)   # every tool call (evals/tests)
    steps: int = 0
    error: str | None = None
    stopped: str | None = None    # None | "max_steps" | "time_budget" | "model_error" | "cancelled"
    progress: list[str] = field(default_factory=list)  # step names shown while thinking


# What the panel shows while a turn runs ("thinking" indicator), per tool.
STEP_LABELS = {
    "get_overview": "Looking at the overview…",
    "list_changes": "Comparing the latest runs…",
    "list_review_queue": "Reading the review queue…",
    "list_results": "Reading the latest results…",
    "explain_result": "Checking why this result has its label…",
    "list_alerts": "Looking at the alerts…",
    "list_runs": "Looking at the latest runs…",
    "list_notes": "Reading the notes…",
    "get_setup_status": "Checking the setup…",
    "list_reports": "Looking at the reports…",
    "app_help": "Reading the app guide…",
}


def step_label(call_name: str) -> str:
    tool = tools.get_tool(call_name)
    if call_name in STEP_LABELS:
        return STEP_LABELS[call_name]
    if tool is not None and tool.kind == "navigate":
        return "Opening the page…"
    if tool is not None and tool.kind == "action":
        return "Preparing a confirmation…"
    return "Working…"


# --------------------------------------------------------------------- #
# Page context sent by the panel (what the user is looking at)
# --------------------------------------------------------------------- #
_CTX_FILTERS = ("country", "date", "classification", "engine", "device", "min_severity",
                "has_note", "new_only", "include_homonyms", "sort_by", "order")


def _clip(value, n: int) -> str:
    return " ".join(str(value or "").split())[:n] if isinstance(value, (str, int, float)) else ""


def _path(value, n: int = 200) -> str:
    text = _clip(value, n)
    return text if text.startswith("/") and not text.startswith("//") else ""


def clean_context(raw) -> dict:
    """Client page context -> a small, typed dict (unknown keys dropped,
    strings clipped). It describes the screen; it is data, not instructions."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    page = _path(raw.get("page"))
    if page:
        out["page"] = page
    for key, n in (("title", 80), ("overlay_title", 80)):
        if _clip(raw.get(key), n):
            out[key] = _clip(raw.get(key), n)
    overlay = _path(raw.get("overlay"))
    if overlay:
        out["overlay"] = overlay
    res = raw.get("result")
    if isinstance(res, dict):
        rid = res.get("id")
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            rid = None
        if rid and rid > 0:
            item = {"result_id": rid}
            for key, n in (("title", 160), ("domain", 120), ("url", 300)):
                if _clip(res.get(key), n):
                    item[key] = _clip(res.get(key), n)
            label = _clip(res.get("label"), 20).lower()
            if label in tools.LABELS:
                item["label"] = label
            out["highlighted_result"] = item
    filters = raw.get("filters")
    if isinstance(filters, dict):
        kept = {k: _clip(filters[k], 40) for k in _CTX_FILTERS if _clip(filters.get(k), 40)}
        if kept:
            out["filters"] = kept
    return out


def clean_messages(messages) -> list[dict]:
    """Client transcript -> [{role: user|assistant, content}] (last N,
    clipped). Anything else (system/tool roles, junk) is dropped: the client
    can't inject a system prompt or fake tool results."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        out.append({"role": role, "content": content.strip()[:MAX_MESSAGE_CHARS]})
    out = out[-MAX_HISTORY_MESSAGES:]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _tool_payload(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = json.dumps({"truncated": True, "partial": text[:MAX_TOOL_RESULT_CHARS]},
                          ensure_ascii=False)
    return text


@dataclass
class _TurnState:
    ctx: tools.ToolContext
    session_key: str
    store: PendingStore
    events: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    proposals: int = 0
    explained: list = field(default_factory=list)   # explain_result results this turn
    cards: int = 0
    progress: list = field(default_factory=list)
    on_step: object = None

    def step(self, label: str) -> None:
        if not self.progress or self.progress[-1] != label:
            self.progress.append(label)
        if callable(self.on_step):
            try:
                self.on_step(label)
            except Exception:
                log.debug("assistant_progress_failed", exc_info=True)

    def add_card(self, card: dict | None) -> None:
        if not card:
            return
        # One card per kind (the latest wins), at most MAX_CARDS_PER_TURN.
        for i, ev in enumerate(self.events):
            if ev.get("type") == "card" and ev.get("kind") == card["kind"]:
                self.events[i] = card
                return
        if self.cards < cards.MAX_CARDS_PER_TURN:
            self.cards += 1
            self.events.append(card)


def _dispatch(state: _TurnState, call: ToolCall) -> dict:
    """Run one tool call -> the result object handed back to the model."""
    ctx = state.ctx
    tool = tools.get_tool(call.name)
    entry = {"name": call.name, "args": call.args, "kind": tool.kind if tool else None,
             "ok": False, "executed": False}
    state.trace.append(entry)
    if tool is None:
        return {"error": f"Unknown tool '{call.name}'. Use only the listed tools."}
    if not tools.role_allows(ctx.role, tool.min_role):
        msg = (f"Not allowed: {tool.name} needs the {tool.min_role} role and the user is "
               f"{ctx.role}. Tell the user an {tool.min_role} can do it.")
        entry["denied"] = True
        return {"error": msg}
    state.step(step_label(tool.name))
    try:
        args = tools.validate_args(tool, call.args)
        entry["args"] = args
        if tool.kind == "read":
            result = tool.executor(ctx, args)
            entry["ok"] = entry["executed"] = True
            entry["result"] = result
            summary = tool.summary(args, result) if tool.summary else f"Used: {tool.name}"
            state.events.append({"type": "tool", "name": tool.name, "summary": summary})
            if tool.name == "explain_result" and isinstance(result, dict):
                state.explained.append(result)
            try:
                state.add_card(cards.from_tool(tool.name, result, ctx))
            except Exception:
                log.exception("assistant_card_failed", extra={"tool": tool.name})
            return result
        if tool.kind == "navigate":
            intent = tool.executor(ctx, args)["intent"]
            entry["ok"] = entry["executed"] = True
            state.events.append({"type": "intent", "tool": tool.name, **intent})
            return {"ok": True, "opened": intent["label"], "url": intent["url"]}
        # action: propose only
        if state.proposals >= MAX_PROPOSALS_PER_TURN:
            return {"error": "Too many proposals in one message; ask the user to confirm these first."}
        prep = tool.prepare(ctx, args)
        action = state.store.add(
            tool=tool.name, args=prep.get("normalized_args", args), session_key=state.session_key,
            user=ctx.user, subject_id=ctx.subject_id, title=prep["title"],
            details=prep.get("details", []), cost_note=prep.get("cost_note", ""),
            button=prep.get("button", "Confirm"), edit=prep.get("edit"))
        state.proposals += 1
        entry["ok"] = True
        entry["pending_id"] = action.id
        state.events.append(action.card())
        return {"status": "awaiting_user_confirmation", "confirmation_id": action.id,
                "title": prep["title"],
                "note": "NOT executed. The user sees a confirmation card with Confirm/Cancel "
                        "above your reply. Tell them briefly what will happen and that they can "
                        "confirm or cancel in the card above. Do not say it is done."}
    except tools.ToolError as e:
        entry["error"] = str(e)
        return {"error": str(e)}
    except Exception:
        log.exception("assistant_tool_failed", extra={"tool": call.name})
        entry["error"] = "internal"
        return {"error": f"The {call.name} tool failed unexpectedly."}


def _maybe_inline_call(turn: ModelTurn) -> ModelTurn:
    """Some OpenAI-compatible models answer a tool request as plain JSON
    text instead of tool_calls; accept that if it names a known tool."""
    if turn.tool_calls or not turn.text.lstrip().startswith(("{", "```")):
        return turn
    parsed = parse_json_protocol(turn.text)
    if parsed.tool_calls and tools.get_tool(parsed.tool_calls[0].name):
        return parsed
    if parsed.text and parsed.text != turn.text:
        return parsed
    return turn


def _ensure_explained(reply: str, explained: list[dict]) -> str:
    """Live test: after explain_result some models only said "I've opened
    the Results page". When the final reply doesn't mention the label the
    tool returned, the tool's plain explanation is appended so the user
    still gets the answer."""
    if not explained:
        return reply
    result = explained[-1]
    label = str(result.get("label") or "").lower()
    if label and label in (reply or "").lower():
        return reply
    text = tools.explanation_text(result)
    return f"{reply}\n\n{text}" if reply else text


def run_turn(model, ctx: tools.ToolContext, messages, *, session_key: str,
             store: PendingStore | None = None, overview: dict | None = None,
             clock=time.monotonic, max_steps: int = MAX_STEPS, page_context: dict | None = None,
             on_step=None, should_stop=None) -> TurnResult:
    """One user turn. `page_context` (clean_context) tells the model what the
    user is looking at; `on_step(label)` reports progress ("Comparing the
    latest runs…"); `should_stop()` returning True ends the turn early (the
    user pressed Stop) before the next model call or tool."""
    # One provider routing session per chat session (OpenCode needs it; the
    # id is derived from the key, never the key itself).
    with llm.session_scope(f"assistant:{session_key}" if session_key else ""):
        return _run_turn(model, ctx, messages, session_key=session_key, store=store,
                         overview=overview, clock=clock, max_steps=max_steps,
                         page_context=clean_context(page_context), on_step=on_step,
                         should_stop=should_stop)


# --------------------------------------------------------------------- #
# Claims vs events (final live test): the reply must not describe a card,
# a prepared action, an opened screen or a saved/created item that no tool
# call of this turn produced.
# --------------------------------------------------------------------- #
CORRECTIVE_NOTE = ("[Note from the app, not from the user] You described an action but didn't "
                   "call a tool. Call the appropriate tool now, or say plainly that you didn't do it.")

# Sentences with a negation are never claims ("it's not saved yet").
_NEGATION = re.compile(
    r"(\b(not|never|nothing|no|none|nobody|cannot|unable|yet|nada|nunca|ningún|ninguna|sin|"
    r"pas|jamais|rien|nicht|kein|nie)\b|n't\b|n’t\b)", re.IGNORECASE)
_CLAIMS = {
    # A confirmation card / a prepared action.
    "confirm": re.compile(
        r"(\b(confirm|cancel)\w*\b[^.!?\n]{0,60}\b(below|above|card|button)\b"
        r"|\bconfirmation card\b|\bcard\b"
        r"|\b(i'?ve|i’ve|i have|i)\s+(\w+\s+)?(prepared|drafted|queued|set up)\b"
        r"|\b(prepared|ready)\s+(it|that|this|the \w+)\s+for you\b"
        r"|\b(he|te he|le he|lo he|la he)\s+preparado\b|\bpreparé\b|\btarjeta\b"
        r"|\b(confirma|confirmar|cancela|cancelar)\b[^.!?\n]{0,60}\b(abajo|arriba|bot[oó]n)\b"
        r"|\bj'?ai préparé\b|\b(confirmez|annulez)\b|\bcarte\b)", re.IGNORECASE),
    # An opened screen.
    "open": re.compile(
        r"(\b(i'?ve|i’ve|i have|i)\s+(just\s+)?opened\b|^\s*opened\b|\b(is|are|now)\s+open(ed)?\s+"
        r"(now|for you)\b|\bopened\b[^.!?\n]{0,40}\bfor you\b"
        r"|\b(he|te he)\s+abierto\b|\babr[ií]\b|\bj'?ai ouvert\b)", re.IGNORECASE),
    # Something saved / created / started (only ever true after a Confirm).
    "done": re.compile(
        r"(\b(i'?ve|i’ve|i have|it'?s been|it has been|has been|have been|was|were)\s+(\w+\s+){0,2}"
        r"(saved|created|started|launched|added|acknowledged|generated|labell?ed)\b"
        r"|\b(he|ha sido|han sido|fue|fueron)\s+(\w+\s+)?(guardad|cread|iniciad|añadid|generad)\w*"
        r"|\b(guardé|creé|añadí|generé)\b|\bj'?ai (enregistré|créé|ajouté)\b)", re.IGNORECASE),
}
# Event types that back each kind of claim.
_BACKED_BY = {"confirm": ("confirm",), "open": ("intent",), "done": ("confirm",)}
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def claimed_kinds(reply: str) -> set[str]:
    """Kinds of action the reply claims ('confirm', 'open', 'done'), ignoring
    negated sentences. Multilingual-tolerant heuristic (en/es/fr)."""
    kinds: set[str] = set()
    for sentence in _SENTENCE.findall(reply or ""):
        if _NEGATION.search(sentence):
            continue
        for kind, rx in _CLAIMS.items():
            if rx.search(sentence.strip()):
                kinds.add(kind)
    return kinds


def unbacked_claims(reply: str, events: list[dict]) -> set[str]:
    """Claimed kinds with no matching event in this turn."""
    have = {e.get("type") for e in events}
    if any(e.get("type") == "card" for e in events):
        have.add("confirm_card")
    out = set()
    for kind in claimed_kinds(reply):
        if not any(t in have for t in _BACKED_BY[kind]):
            # A "card" mention is fine when a result card was shown.
            if kind == "confirm" and "confirm_card" in have and not re.search(
                    r"confirm|cancel|prepar|confirma|cancela", reply or "", re.IGNORECASE):
                continue
            out.add(kind)
    return out


def strip_claims(reply: str) -> str:
    """The reply without the sentences that make an (unbacked) claim."""
    kept = [s for s in _SENTENCE.findall(reply or "")
            if _NEGATION.search(s) or not any(rx.search(s.strip()) for rx in _CLAIMS.values())]
    return " ".join(x.strip() for x in kept if x.strip())


# Deterministic fallback: a request that clearly maps to one tool (checked in
# order; navigate tools, or a proposal with obvious default args). Proposals
# only ever become confirmation cards -- nothing executes.
_REPORT_KIND = (("csv", "csv"), ("xlsx", "xlsx"), ("excel", "xlsx"), ("pdf", "pdf"))
_FALLBACKS = (
    (re.compile(r"\b(create|make|generate|build|export|download|crea|crear|haz|hazme|genera|generar|"
                r"exporta|exportar|descarga|cr[ée]e[rz]?|g[ée]n[ée]re[rz]?)\b.*\b(reports?|informes?|"
                r"reportes?|rapports?|pdf|csv|excel|xlsx)\b|\b(pdf|csv|excel|xlsx)\b.*\b(report|informe|"
                r"reporte|rapport)\b", re.IGNORECASE), "create_report"),
    (re.compile(r"\b(new search|search dialog|nueva b[uú]squeda|nouvelle recherche|open (the )?search)\b",
                re.IGNORECASE), "open_search"),
    (re.compile(r"\b(compare|comparison|compar[ae]r?|comparaci[oó]n|comparer)\b", re.IGNORECASE),
     "open_compare"),
    (re.compile(r"\b(review|revisar|revisi[oó]n|pendientes|r[ée]vision)\b", re.IGNORECASE), "open_review"),
    (re.compile(r"\b(ai checks|relabel|comprobaciones)\b", re.IGNORECASE), "open_ai_checks"),
    (re.compile(r"\b(reports?|informes?|reportes?|rapports?)\b", re.IGNORECASE), "open_reports"),
    (re.compile(r"\b(alerts?|alertas?|alertes?|bell)\b", re.IGNORECASE), "open_alerts"),
    (re.compile(r"\b(notes?|notas?|removal request)\b", re.IGNORECASE), "open_notes"),
    (re.compile(r"\b(timeline|cronolog[ií]a)\b", re.IGNORECASE), "open_timeline"),
    (re.compile(r"\b(evidence|evidencias?|pruebas)\b", re.IGNORECASE), "open_evidence"),
    (re.compile(r"\b(domains?|dominios?)\b", re.IGNORECASE), "open_domains"),
    (re.compile(r"\b(runs|run history|ejecuciones|historial)\b", re.IGNORECASE), "open_runs"),
    (re.compile(r"\b(search|buscar|b[uú]squeda|recherche)\b", re.IGNORECASE), "open_search"),
)


def fallback_call(user_text: str) -> ToolCall | None:
    """The one tool a plain request obviously maps to, or None."""
    for rx, name in _FALLBACKS:
        if rx.search(user_text or ""):
            args: dict = {}
            if name == "create_report":
                low = (user_text or "").lower()
                args["kind"] = next((k for word, k in _REPORT_KIND if word in low), "pdf")
            return ToolCall(f"fallback_{name}", name, args)
    return None


def _event_label(ev: dict) -> str:
    return str(ev.get("label") or ev.get("title") or ev.get("button") or "Open")


def _verify_claims(state: _TurnState, reply: str, user_text: str) -> str:
    """Last line of defence after the re-prompt: drop the unbacked claim and
    offer a deterministic fallback (a button, or a confirmation card with
    default args) when the request clearly maps to one."""
    if not unbacked_claims(reply, state.events):
        return reply
    kept = strip_claims(reply)
    call = fallback_call(user_text)
    added = None
    if call is not None:
        before = len(state.events)
        result = _dispatch(state, call)
        if len(state.events) > before and not (isinstance(result, dict) and result.get("error")):
            added = state.events[-1]
            added["fallback"] = True
    log.info("assistant_claim_replaced fallback=%s", call.name if added is not None else "-")
    line = "I couldn't prepare that."
    if added is not None:
        line += f" Try the button: {_event_label(added)} (above)."
    return f"{kept}\n\n{line}" if kept else line


def _run_turn(model, ctx: tools.ToolContext, messages, *, session_key: str,
              store: PendingStore | None, overview: dict | None,
              clock, max_steps: int, page_context: dict | None = None, on_step=None,
              should_stop=None) -> TurnResult:
    store = store or STORE
    history = clean_messages(messages)
    if not history or history[-1]["role"] != "user":
        return TurnResult(reply="Ask me something about your results or what to do next.")
    user_text = history[-1]["content"]
    if overview is None:
        try:
            overview = tools.get_overview(ctx, {})
        except Exception:
            log.exception("assistant_overview_failed")
            overview = None
    system = prompts.build_system_prompt(ctx.role, overview, page_context=page_context)
    specs = tools.tool_specs(ctx.role)
    state = _TurnState(ctx=ctx, session_key=session_key, store=store, on_step=on_step)

    def stop_requested() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:  # noqa: BLE001
            return False
    start = clock()
    run = {"reply": "", "stopped": None, "error": None, "steps": 0, "last_text": ""}

    def loop(limit: int) -> None:
        while True:
            if run["steps"] >= limit:
                run["stopped"] = "max_steps"
                return
            if clock() - start > TURN_BUDGET_S:
                run["stopped"] = "time_budget"
                return
            if stop_requested():
                run["stopped"] = "cancelled"
                return
            state.step("Thinking…" if run["steps"] == 0 else "Writing the answer…")
            run["steps"] += 1
            try:
                turn = _maybe_inline_call(model.complete(system, history, specs))
            except ModelError as e:
                run["error"], run["stopped"] = scrub_secrets(str(e)), "model_error"
                log.warning("assistant_model_error: %s", run["error"])
                ai_status.record(False, run["error"])
                return
            except Exception:
                log.exception("assistant_model_failed")
                run["error"], run["stopped"] = ("Unexpected error talking to the AI provider.",
                                                "model_error")
                ai_status.record(False, run["error"])
                return
            ai_status.record(True)
            if turn.text:
                run["last_text"] = turn.text
            if not turn.tool_calls:
                run["reply"] = turn.text
                return
            calls = turn.tool_calls[:MAX_CALLS_PER_STEP]
            history.append({"role": "assistant", "content": turn.text, "tool_calls": calls})
            for call in calls:
                if stop_requested():
                    result = {"error": "The user stopped this request."}
                    history.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                    "content": _tool_payload(result)})
                    continue
                result = _dispatch(state, call)
                history.append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                                "content": _tool_payload(result)})

    loop(max_steps)
    # Claims vs events: a reply describing a card / prepared action / opened
    # screen / saved item that no tool call produced gets ONE corrective
    # re-prompt (a couple of extra steps at most).
    if run["stopped"] is None and run["reply"] and unbacked_claims(run["reply"], state.events):
        log.info("assistant_unbacked_claim: %s", sorted(unbacked_claims(run["reply"], state.events)))
        history.append({"role": "assistant", "content": run["reply"]})
        history.append({"role": "user", "content": CORRECTIVE_NOTE})
        run["reply"] = ""
        loop(run["steps"] + 3)
    reply, stopped, error, steps = run["reply"], run["stopped"], run["error"], run["steps"]
    last_text = run["last_text"]
    has_confirm = any(e["type"] == "confirm" for e in state.events)
    if stopped == "cancelled":
        return TurnResult(reply="Stopped.", events=scrub_obj(state.events), trace=state.trace,
                          steps=steps, stopped=stopped, progress=state.progress)
    if stopped == "model_error":
        reply = f"Sorry, I couldn't get an answer from the AI provider. {error}"
    elif stopped in ("max_steps", "time_budget"):
        reason = ("I hit the limit of tool steps for one message" if stopped == "max_steps"
                  else "This took too long")
        reply = (last_text + "\n\n" if last_text else "") + (
            f"{reason}. Here's what I gathered so far; ask a narrower question to go further.")
    if stopped != "model_error":
        reply = _verify_claims(state, reply, user_text)
        reply = _ensure_explained(reply, state.explained)
    has_confirm = any(e["type"] == "confirm" for e in state.events)
    if not reply:
        reply = ("I've prepared that. Review the details and confirm or cancel in the card above."
                 if has_confirm else "I don't have an answer for that. Try rephrasing.")
    return TurnResult(reply=scrub_secrets(reply), events=scrub_obj(state.events),
                      trace=state.trace, steps=steps, error=error, stopped=stopped,
                      progress=state.progress)
