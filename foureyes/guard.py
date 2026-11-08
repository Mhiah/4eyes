"""The rules: decide whether a money action passes, waits for a human, or is refused."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

from . import chain
from .detect import MoneyAction
from .store import Store

CRITICAL, HIGH, NOTE = "critical", "high", "note"


@dataclass
class Reason:
    code: str
    text: str
    severity: str = HIGH


@dataclass
class Verdict:
    status: str                         # allow | hold | block
    reasons: list[Reason] = field(default_factory=list)
    hold_id: Optional[str] = None
    message: str = ""


# --- what the user said vs what came from content --------------------------------------------------

AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:\$|usd|usdc|usdt|dai|eth|sol|matic|bnb|[a-z]{2,6})?\b", re.I)
AMOUNT_TOKEN_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]{2,6})\b")


class Provenance:
    """Per-conversation memory of addresses the user typed and addresses that only appeared in
    tool output (emails, web pages, files). Kept in memory; capped."""

    def __init__(self, cap: int = 200):
        self._d: "OrderedDict[str, dict]" = OrderedDict()
        self._cap = cap
        self._lock = threading.Lock()

    def _get(self, cid: str) -> dict:
        with self._lock:
            if cid not in self._d:
                self._d[cid] = {"user": set(), "content": set(), "amounts": [], "names": set(), "text": ""}
                while len(self._d) > self._cap:
                    self._d.popitem(last=False)
            self._d.move_to_end(cid)
            return self._d[cid]

    def note_user(self, cid: str, text: str, payee_names: list[str]) -> None:
        p = self._get(cid)
        p["user"] |= chain.find_addresses(text)
        low = (text or "").lower()
        for m in AMOUNT_TOKEN_RE.finditer(text or ""):
            try:
                p["amounts"].append((float(m.group(1).replace(",", "")), m.group(2).upper()))
            except ValueError:
                pass
        for n in payee_names:
            if re.search(r"(?<![a-z0-9])" + re.escape(n.lower()) + r"(?![a-z0-9])", low):
                p["names"].add(n.lower())

    def note_content(self, cid: str, text: str) -> None:
        if text:
            self._get(cid)["content"] |= chain.find_addresses(text[:200_000])

    def view(self, cid: str) -> dict:
        return self._get(cid)


def fingerprint(tool: str, args: Any) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return hashlib.sha256(f"{tool}\n{blob}".encode()).hexdigest()[:16]


# --- the rules -------------------------------------------------------------------------------------

def assess(act: MoneyAction, store: Store, prov: dict) -> list[Reason]:
    reasons: list[Reason] = []
    book = store.payees()
    book_addrs = {p["address"].lower(): p.get("label", n) for n, p in book.items()}
    user_addrs = {a.lower() for a in prov.get("user", set())}
    content_addrs = {a.lower() for a in prov.get("content", set())}
    settings = store.settings()

    # Unlimited approvals, approve-all and permits: the way wallets are actually drained.
    if act.kind in ("approve", "approve_all", "permit") and act.unlimited:
        for sp in act.recipients or ["(unknown)"]:
            known = chain.KNOWN_SPENDERS.get(sp.lower()) or book_addrs.get(sp.lower())
            if not known:
                what = {"approve_all": "control of ALL your NFTs/tokens in this collection",
                        "permit": "a signed permit to move your tokens (no transaction, so wallets show no warning)"}
                reasons.append(Reason("drain", f"Gives {chain.short(sp)} {what.get(act.kind, 'UNLIMITED spending of your tokens')}. "
                                      "This is how wallet drainers work.", CRITICAL))
    if act.kind == "order":
        reasons.append(Reason("order", "Signing a marketplace order. A fake 'listing' or 'claim' signature can sell "
                              "your NFTs for nothing.", CRITICAL))

    for r in act.recipients:
        rl = r.lower()
        name = book_addrs.get(rl)
        if name:
            continue
        # Address poisoning: looks like someone you pay, but isn't.
        twins = [n for a, n in book_addrs.items() if chain.lookalike(a, rl)]
        twins += [chain.short(a) for a in user_addrs if chain.lookalike(a, rl)]
        if twins:
            reasons.append(Reason("lookalike", f"{chain.short(r)} starts and ends like {twins[0]} but is a different "
                                  "address. This is address poisoning.", CRITICAL))
            continue
        # The user named a saved payee, but the money is going somewhere else.
        named = [n for n in prov.get("names", set()) if n in book]
        if named:
            p = book[named[0]]
            reasons.append(Reason("changed", f"You asked to pay {p.get('label', named[0])}, whose saved address is "
                                  f"{chain.short(p['address'])}, but this goes to {chain.short(r)}. Changed payment "
                                  "details are the classic invoice-fraud move.", CRITICAL))
            continue
        if rl in user_addrs:
            if settings.get("hold_new_payees", True) and act.kind in ("transfer", "payment", "approve"):
                reasons.append(Reason("new", f"First payment to {chain.short(r)}. It's not in your trusted payees yet.",
                                      NOTE))
        elif rl in content_addrs:
            reasons.append(Reason("injected", f"{chain.short(r)} came from content Hermes read (an email, web page, "
                                  "file or tool result), not from you. Confirm it with the payee another way.",
                                  CRITICAL))
        elif act.kind not in ("approve", "approve_all", "permit") or not act.unlimited:
            reasons.append(Reason("unknown", f"{chain.short(r)} is not a trusted payee and you never gave it.", HIGH))

    # More than the user asked for.
    if act.amount is not None and act.kind in ("transfer", "payment", "swap"):
        asked = [a for a, t in prov.get("amounts", []) if not act.token or t == act.token.upper()]
        if asked and act.amount > max(asked) * 1.0001:
            reasons.append(Reason("intent", f"You asked for {max(asked):g} {act.token or ''}".rstrip() +
                                  f", this sends {act.amount:g}.", CRITICAL))

    # Limits.
    if act.amount is not None and act.token and act.kind in ("transfer", "payment", "swap"):
        lim = store.limits().get(act.token.upper())
        if lim:
            if lim.get("per_tx") is not None and act.amount > lim["per_tx"]:
                reasons.append(Reason("limit", f"{act.amount:g} {act.token} is over your per-payment limit of "
                                      f"{lim['per_tx']:g}.", HIGH))
            today = store.spent_today(act.token)
            if lim.get("per_day") is not None and today + act.amount > lim["per_day"]:
                reasons.append(Reason("daily", f"This would bring today's {act.token} total to "
                                      f"{today + act.amount:g}, over your daily limit of {lim['per_day']:g}.", HIGH))
    return reasons


def _message(act: MoneyAction, reasons: list[Reason], hold_id: str) -> str:
    lines = [f"4eyes HELD this payment (hold {hold_id}): {act.summary()}"]
    for r in reasons:
        lines.append(f"- {r.text}")
    lines.append(f"Do not retry, split, or re-route this payment, and do not try another tool. Tell the user "
                 f"exactly what is above and wait. Only the user can release it: they type "
                 f"/4eyes approve {hold_id} (or tap Approve in the 4eyes pane), or /4eyes reject {hold_id}.")
    return "\n".join(lines)


def evaluate(act: MoneyAction, args: Any, store: Store, prov: dict, *, session: str = "") -> Verdict:
    settings = store.settings()
    if settings.get("frozen"):
        msg = ("4eyes is FROZEN: every payment, approval and signature is blocked until the user types "
               "/4eyes unfreeze. Tell the user; do not try another way.")
        store.log({"outcome": "blocked", "summary": act.summary(), "reasons": ["frozen"]})
        return Verdict("block", [Reason("frozen", "4eyes is frozen", CRITICAL)], message=msg)

    reasons = assess(act, store, prov)
    fp = fingerprint(act.tool, args)
    now = time.time()
    if not reasons:
        _spend(act, store)
        store.log({"outcome": "allowed", "summary": act.summary(), "reasons": []})
        return Verdict("allow")

    # Already approved by the user? Single use, within the approval window, after any cooldown.
    window = float(settings.get("approval_minutes", 15)) * 60
    cool = float(settings.get("cooldown_minutes", 0)) * 60
    for h in store.holds().values():
        if h["fingerprint"] == fp and h["status"] == "approved" and now - h.get("decided_at", 0) <= window:
            if now - h["created"] < cool and any(r.code in ("new", "unknown") for r in reasons):
                left = int((cool - (now - h["created"])) / 60) + 1
                return Verdict("hold", reasons, h["id"], f"4eyes: approved, but this is a new payee and the "
                               f"cooling-off period has {left} more minute(s). Tell the user and wait.")
            h["status"], h["used_at"] = "used", now
            store.put_hold(h)
            _spend(act, store)
            store.log({"outcome": "approved", "summary": act.summary(), "reasons": [r.code for r in reasons],
                       "hold": h["id"]})
            return Verdict("allow", reasons, h["id"])
        if h["fingerprint"] == fp and h["status"] == "held":
            return Verdict("hold", reasons, h["id"], _message(act, reasons, h["id"]))
        if h["fingerprint"] == fp and h["status"] == "rejected" and now - h.get("decided_at", 0) <= window:
            return Verdict("block", reasons, h["id"], f"4eyes: the user REJECTED this payment (hold {h['id']}). "
                           "Do not try again.")

    hold_id = secrets.token_hex(3)
    store.put_hold({"id": hold_id, "fingerprint": fp, "status": "held", "created": now, "session": session,
                    "summary": act.summary(), "kind": act.kind, "recipients": act.recipients,
                    "amount": act.amount, "token": act.token, "unlimited": act.unlimited,
                    "reasons": [{"code": r.code, "text": r.text, "severity": r.severity} for r in reasons]})
    store.log({"outcome": "held", "summary": act.summary(), "reasons": [r.code for r in reasons], "hold": hold_id})
    return Verdict("hold", reasons, hold_id, _message(act, reasons, hold_id))


def _spend(act: MoneyAction, store: Store) -> None:
    if act.amount is not None and act.token and act.kind in ("transfer", "payment", "swap"):
        store.add_spend(act.token, act.amount)
