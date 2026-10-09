"""Pending (proposed, not yet confirmed) assistant actions.

An action tool call never executes inside the chat loop: it becomes a
PendingAction here, keyed by a random id and bound to the session that
proposed it and to the active subject. POST /assistant/confirm executes it
at most once; POST /assistant/cancel discards it. Entries expire after
TTL_SECONDS. In-memory (one uvicorn process, like dashboard.jobs) -- a
restart drops pending proposals, which is the safe direction.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

TTL_SECONDS = 600
_MAX_ENTRIES = 500


@dataclass
class PendingAction:
    id: str
    tool: str
    args: dict
    session_key: str
    user: str
    subject_id: int
    title: str
    details: list
    cost_note: str
    button: str = "Confirm"
    edit: dict | None = None         # editable fields shown on the card (tools.EDITABLE)
    created_at: float = field(default_factory=time.time)
    status: str = "pending"          # pending | executing | done | failed | cancelled
    result: dict | None = None
    edited: bool = False             # confirmed with the user's changes

    @property
    def expires_at(self) -> float:
        return self.created_at + TTL_SECONDS

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at

    def card(self) -> dict:
        """The {type:'confirm', ...} event the client renders."""
        card = {"type": "confirm", "id": self.id, "tool": self.tool, "title": self.title,
                "details": list(self.details), "cost_note": self.cost_note,
                "button": self.button, "expires_in": max(0, int(self.expires_at - time.time()))}
        if self.edit:
            card["edit"] = self.edit
        return card


class PendingStore:
    def __init__(self):
        self._items: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    def add(self, **fields) -> PendingAction:
        action = PendingAction(id=secrets.token_urlsafe(18), **fields)
        with self._lock:
            self._prune_locked()
            self._items[action.id] = action
        return action

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock:
            return self._items.get(action_id or "")

    def claim(self, action_id: str) -> tuple[str, PendingAction | None]:
        """Atomically move a pending action to 'executing'. Returns
        (state, action): 'claimed', 'missing', 'expired' or the action's
        current status ('executing', 'done', 'failed', 'cancelled')."""
        with self._lock:
            action = self._items.get(action_id or "")
            if action is None:
                return "missing", None
            if action.status != "pending":
                return action.status, action
            if action.expired():
                return "expired", action
            action.status = "executing"
            return "claimed", action

    def finish(self, action: PendingAction, ok: bool, result: dict) -> None:
        with self._lock:
            action.status = "done" if ok else "failed"
            action.result = result

    def cancel(self, action_id: str) -> tuple[str, PendingAction | None]:
        with self._lock:
            action = self._items.get(action_id or "")
            if action is None:
                return "missing", None
            if action.status != "pending":
                return action.status, action
            action.status = "cancelled"
            return "cancelled", action

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _prune_locked(self) -> None:
        now = time.time()
        # Keep finished entries for a while so a repeated confirm answers
        # "already done" (idempotent) instead of 404.
        stale = [k for k, a in self._items.items() if now > a.expires_at + TTL_SECONDS]
        for k in stale:
            del self._items[k]
        if len(self._items) >= _MAX_ENTRIES:
            for k, _a in sorted(self._items.items(), key=lambda kv: kv[1].created_at)[
                    : len(self._items) - _MAX_ENTRIES + 1]:
                del self._items[k]


STORE = PendingStore()
