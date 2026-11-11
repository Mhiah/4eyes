"""4eyes: the four-eyes rule for AI agents. No crypto leaves your wallet without a second pair of eyes.

Hermes surfaces used (all public):
  pre_llm_call    remember the addresses, amounts and payee names YOU typed this conversation
  post_tool_call  remember addresses that only showed up in content Hermes read (emails, pages, files)
  pre_tool_call   inspect every tool call; hold or refuse payments, approvals and signatures that look wrong
  /4eyes          approve or reject holds, freeze everything, manage trusted payees and limits (human only)
  approve action  when a person is there to answer, a held payment is put to them through Hermes' own approval
                  prompt (one tap, the model cannot answer it); otherwise it is blocked until /4eyes approve
  tools           foureyes_check (dry run) and foureyes_status (read-only); the model can never approve
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import time
from typing import Any, Optional
from urllib.parse import unquote

from .foureyes import chain
from .foureyes.detect import detect
from .foureyes.guard import Provenance, _spend, evaluate, fingerprint
from .foureyes.store import PLUGIN_ID, Store

logger = logging.getLogger(__name__)

_STORE = Store()
_PROV = Provenance()
_OWN_TOOLS = {"foureyes_check", "foureyes_status"}


def _notify(event: str, payload: dict[str, Any]) -> None:
    """Tell the Desktop pane something changed (no-op when no Desktop window is attached)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:  # pragma: no cover - Desktop is optional
        logger.debug("4eyes: desktop event not delivered", exc_info=True)


def _cid(kwargs: dict) -> str:
    # session_id is the one id every hook gets (pre_llm_call has no conversation_id), so it keys
    # "what the user typed" and "what tools returned" to the same conversation.
    for k in ("session_id", "conversation_id", "task_id"):
        v = kwargs.get(k)
        if v:
            return str(v)
    return "default"


# --- hooks -------------------------------------------------------------------------------------------

def _on_pre_llm_call(user_message: str = "", **kwargs: Any):
    try:
        _PROV.note_user(_cid(kwargs), user_message or "", list(_STORE.payees()))
    except Exception:
        logger.debug("4eyes: could not record user message", exc_info=True)
    return None


def _on_post_tool_call(tool_name: str = "", result: Any = "", **kwargs: Any):
    if tool_name in _OWN_TOOLS:
        return None
    try:
        _settle_one_tap(tool_name, kwargs.get("args"), result, kwargs)
    except Exception:
        logger.debug("4eyes: could not settle a one-tap approval", exc_info=True)
    try:
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        _PROV.note_content(_cid(kwargs), text)
    except Exception:
        logger.debug("4eyes: could not record tool output", exc_info=True)
    return None


_COMMANDS = re.compile(r"/4eyes\s+(approve|unfreeze|trust|untrust|limit|newpayees|cooldown|reject|unpriced|onetap)")
_BACKEND = re.compile(r"/api/plugins/4eyes")
_OWN_CODE = re.compile(r"plugins/*4eyes")
_PLUGIN_ADMIN = re.compile(r"plugins\s+(disable|remove|uninstall|rm|update|install|reinstall)\b[^\n;&|]*4eyes|"
                           r"config\s+set\s+plugins\.")
_DESTRUCTIVE = re.compile(r"\b(rm|mv|shred|truncate|unlink|rmdir|rsync|chmod|chown|tee|dd|cp)\b|"
                          r"plugin-data/\S*[*?\[{]")
_READERS = {"read_file", "search_files", "list_files", "list_directory", "glob", "grep"}


def _flatten(args: Any) -> str:
    """One lower-case string of everything in the call, with the cheap disguises removed: URL-encoding
    (/api/plugins/%34eyes) and quotes or backslashes dropped into the middle of a word (4ey""es)."""
    try:
        blob = json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(args)
    for _ in range(2):
        blob = unquote(blob)
    return re.sub(r"[\"'`\\^]", "", blob).lower()


def _tampering(tool_name: str, args: Any) -> bool:
    """The model must never release its own payment: no running /4eyes through a nested Hermes, no calling
    the pane's backend, no touching 4eyes' data or code, no switching the plugin off. This is a tripwire for
    an agent that has been talked into it, not a sandbox: see "What 4eyes cannot see" in the README."""
    blob = _flatten(args)
    if _COMMANDS.search(blob) or _BACKEND.search(blob) or _PLUGIN_ADMIN.search(blob):
        return True
    if (tool_name or "").lower() in _READERS:
        return False                                    # reading is harmless: hold ids are no secret
    if _OWN_CODE.search(blob):
        return True
    return "plugin-data" in blob and ("4eyes" in blob or bool(_DESTRUCTIVE.search(blob)))


def _one_tap_ready(codes: list[str]) -> bool:
    """True only when Hermes' approval prompt will really reach a person. With approvals off, in yolo
    mode, under smart (model-judged) approvals, or with nobody at the other end, that prompt can pass
    without a human, so 4eyes keeps to its own hold and /4eyes approve. Any doubt means False."""
    settings = _STORE.settings()
    if not settings.get("one_tap", True):
        return False
    if float(settings.get("cooldown_minutes", 0)) > 0 and {"new", "unknown", "unreadable"} & set(codes):
        return False                                    # a cooling-off period cannot be tapped through
    try:
        # Hermes has no public "will a person answer?" call yet, so this reads its approval module. If a
        # release moves any of it, the except below turns one-tap off and 4eyes blocks as before.
        from tools import approval
        if approval._yolo_active() or approval.approval_context._get_approval_mode() != "manual":
            return False
        _callback, is_cli, is_gateway, is_ask = approval._presence(None)
        return bool(is_cli or is_gateway or is_ask)
    except Exception:
        return False


def _settle_one_tap(tool_name: str, args: Any, result: Any, kwargs: dict) -> None:
    """After a call 4eyes sent to Hermes' approval prompt: if it ran, the user said yes, so use the hold up
    and count the spend; if the user said no, mark it rejected."""
    if not isinstance(args, dict):
        return
    hold = _STORE.asked_hold(fingerprint(tool_name, args), str(kwargs.get("tool_call_id") or ""))
    if not hold:
        return
    status, text = kwargs.get("status"), result if isinstance(result, str) else ""
    blocked = status == "blocked" or (status is None and text.lstrip().startswith("BLOCKED"))
    if blocked:
        if "User denied" in text:
            _STORE.decide(hold["id"], "rejected", "user:hermes")
            _STORE.log({"outcome": "blocked", "summary": hold["summary"], "reasons": ["rejected"], "hold": hold["id"]})
            _notify("decided", {"hold": hold["id"], "status": "rejected"})
        return
    _STORE.decide(hold["id"], "approved", "user:hermes")
    hold = _STORE.holds().get(hold["id"]) or hold
    hold["status"], hold["used_at"] = "used", time.time()
    _STORE.put_hold(hold)
    act = detect(tool_name, args)
    if act is not None:
        _spend(act, _STORE)
    _STORE.log({"outcome": "approved", "summary": hold["summary"],
                "reasons": [r["code"] for r in hold.get("reasons", [])], "hold": hold["id"]})
    _notify("decided", {"hold": hold["id"], "status": "used"})


def _on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **kwargs: Any):
    if tool_name in _OWN_TOOLS:
        return None
    if _tampering(tool_name, args or {}):
        _STORE.log({"outcome": "blocked", "summary": f"{tool_name} tried to change 4eyes", "reasons": ["tamper"]})
        _notify("blocked", {"summary": f"{tool_name} tried to change 4eyes"})
        return {"action": "block", "message": "4eyes: tools may not approve payments, change 4eyes settings or "
                "touch its files. Only the user can, by typing /4eyes in the chat. Tell the user what you need."}
    act = detect(tool_name, args or {})
    if act is None:
        return None
    cid = _cid(kwargs)
    verdict = evaluate(act, args or {}, _STORE, _PROV.view(cid), session=cid)
    if verdict.status == "allow":
        _notify("checked", {"outcome": "allowed", "summary": act.summary(), "hold": verdict.hold_id})
        return None
    _notify("held" if verdict.status == "hold" else "blocked",
            {"summary": act.summary(), "hold": verdict.hold_id, "reasons": [r.text for r in verdict.reasons]})
    if verdict.status == "hold" and verdict.hold_id and verdict.askable \
            and _one_tap_ready([r.code for r in verdict.reasons]):
        asked = _STORE.mark_asked(verdict.hold_id, str(kwargs.get("tool_call_id") or ""))
        if asked:
            why = " ".join(r.text for r in verdict.reasons)
            # A fresh key every time: "always allow" on this prompt can never cover a later payment.
            return {"action": "approve", "rule_key": f"4eyes:{verdict.hold_id}:{asked}",
                    "message": f"4eyes held this: {act.summary()}. {why}"[:600]}
    return {"action": "block", "message": verdict.message}


# --- tools (read-only; the model can look, never approve) ---------------------------------------------

CHECK_SCHEMA = {
    "name": "foureyes_check",
    "description": ("Dry run: would 4eyes let this payment through? Pass the tool name and arguments of a "
                    "planned transfer, swap, approval or signature. Returns the reasons it would be held. "
                    "Creates no hold and approves nothing."),
    "parameters": {"type": "object", "properties": {
        "tool_name": {"type": "string", "description": "The tool you plan to call."},
        "args": {"type": "object", "description": "The arguments you plan to pass."}},
        "required": ["tool_name", "args"]},
}

STATUS_SCHEMA = {
    "name": "foureyes_status",
    "description": ("Read 4eyes state: frozen or not, payments waiting for the user's approval, trusted payees "
                    "and limits. Read-only. Only the user can approve holds, trust payees or change limits, "
                    "with the /4eyes command."),
    "parameters": {"type": "object", "properties": {}},
}


def tool_check(args: dict, **kwargs: Any) -> str:
    from .foureyes.guard import assess
    tool, targs = args.get("tool_name", ""), args.get("args") or {}
    act = detect(tool, targs)
    if act is None:
        return json.dumps({"money_action": False, "note": "4eyes does not treat this call as moving crypto."})
    reasons = assess(act, _STORE, _PROV.view(_cid(kwargs)))
    if _STORE.settings().get("frozen"):
        reasons_out = ["4eyes is frozen: every payment is blocked."]
    else:
        reasons_out = [r.text for r in reasons]
    return json.dumps({"money_action": True, "summary": act.summary(),
                       "would_pass": not reasons_out, "reasons": reasons_out})


def tool_status(args: dict, **kwargs: Any) -> str:
    return json.dumps(_status_dict(), default=str)


def _status_dict() -> dict:
    held = [h for h in _STORE.holds().values() if h["status"] == "held"]
    return {"frozen": bool(_STORE.settings().get("frozen")),
            "waiting_for_user": [{"id": h["id"], "summary": h["summary"],
                                  "reasons": [r["text"] for r in h["reasons"]]} for h in held],
            "trusted_payees": {p.get("label", n): p["address"] for n, p in _STORE.payees().items()},
            "limits": _STORE.limits()}


# --- demo wallet (opt-in: /4eyes demo on) ---------------------------------------------------------------
# A pretend wallet so anyone can see 4eyes work without a real wallet plugin. It moves no money: it only
# reports what a real wallet would have sent. 4eyes guards it exactly like any other wallet tool.

DEMO_SEND_SCHEMA = {
    "name": "demo_wallet_send",
    "description": ("4eyes DEMO wallet (simulated, no real funds). Send a token to an address. Use this "
                    "whenever the user asks to pay or send crypto while demo mode is on."),
    "parameters": {"type": "object", "properties": {
        "to": {"type": "string", "description": "Recipient wallet address (0x… or Solana)."},
        "amount": {"type": "number", "description": "Amount in whole tokens, e.g. 0.02"},
        "token": {"type": "string", "description": "Token symbol, e.g. ETH, USDC, SOL"}},
        "required": ["to", "amount", "token"]},
}

DEMO_TX_SCHEMA = {
    "name": "demo_wallet_send_transaction",
    "description": ("4eyes DEMO wallet (simulated, no real funds). Sign and send a raw EVM transaction, "
                    "e.g. a contract call a dapp or web page asks for."),
    "parameters": {"type": "object", "properties": {
        "to": {"type": "string", "description": "Contract address"},
        "data": {"type": "string", "description": "0x-prefixed calldata"},
        "value": {"type": "string", "description": "Native value in wei, default 0"}},
        "required": ["to", "data"]},
}


def _demo_on() -> bool:
    try:
        return bool(_STORE.settings().get("demo"))
    except Exception:
        return False


def tool_demo_send(args: dict, **kwargs: Any) -> str:
    return json.dumps({"simulated": True, "status": "sent (demo wallet, no real funds moved)",
                       "to": args.get("to"), "amount": args.get("amount"), "token": args.get("token")})


def tool_demo_tx(args: dict, **kwargs: Any) -> str:
    return json.dumps({"simulated": True, "status": "transaction sent (demo wallet, no real funds moved)",
                       "to": args.get("to"), "data": (args.get("data") or "")[:74]})


# --- /4eyes (typed by the human; never callable by the model) -----------------------------------------

HELP = """4eyes: no crypto leaves without a second pair of eyes
  /4eyes                         status: frozen?, payments waiting, payees, limits
  /4eyes holds                   payments waiting for you
  /4eyes approve <id>            release one held payment (single use, 15 min)
  /4eyes reject <id>             refuse it
  /4eyes freeze | unfreeze       stop every payment, approval and signature at once
  /4eyes trust <name> <address>  add a trusted payee
  /4eyes untrust <name>          remove one
  /4eyes payees                  list trusted payees
  /4eyes limit <TOKEN> <per_payment|off> [per_day]
  /4eyes newpayees hold|allow    hold the first payment to an address you typed (default: hold)
  /4eyes cooldown <minutes>      wait this long before a new payee's approval can be used
  /4eyes unpriced hold|allow     hold payments whose token or amount 4eyes cannot work out (default: hold)
  /4eyes onetap on|off           ask through Hermes' own approval prompt when a person is there (default: on)
  /4eyes log                     recent decisions
  /4eyes demo on|off             add a pretend wallet (no real funds) to try 4eyes"""


def _fmt_status() -> str:
    st = _status_dict()
    lines = ["4eyes is FROZEN: every payment is blocked." if st["frozen"] else "4eyes is guarding payments."]
    w = st["waiting_for_user"]
    lines.append(f"Waiting for you: {len(w)}" + ("" if not w else "  (/4eyes holds)"))
    lines.append(f"Trusted payees: {len(st['trusted_payees'])}")
    lim = ", ".join(f"{t} {v.get('per_tx', '-')}/{v.get('per_day', '-')}" for t, v in st["limits"].items())
    lines.append(f"Limits (per payment/per day): {lim}")
    return "\n".join(lines)


def _fmt_holds() -> str:
    held = [h for h in _STORE.holds().values() if h["status"] == "held"]
    if not held:
        return "Nothing is waiting for you."
    out = []
    for h in sorted(held, key=lambda x: x["created"]):
        out.append(f"[{h['id']}] {h['summary']}")
        out += [f"   - {r['text']}" for r in h["reasons"]]
        out.append(f"   /4eyes approve {h['id']}   |   /4eyes reject {h['id']}")
    return "\n".join(out)


def _num(s: str) -> Optional[float]:
    if s.lower() in ("off", "none", "-"):
        return None
    return float(s)


def handle_command(raw_args: str = "", **kwargs: Any) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        parts = (raw_args or "").split()
    if not parts:
        return _fmt_status()
    cmd, rest = parts[0].lower(), parts[1:]
    if cmd in ("help", "?"):
        return HELP
    if cmd in ("holds", "pending", "waiting"):
        return _fmt_holds()
    if cmd in ("approve", "reject"):
        if not rest:
            return f"Usage: /4eyes {cmd} <id>   (see /4eyes holds)"
        h = _STORE.decide(rest[0], "approved" if cmd == "approve" else "rejected", "user")
        if not h:
            return f"No hold called {rest[0]}. See /4eyes holds."
        if h["status"] not in ("approved", "rejected") or not str(h.get("decided_by", "")).startswith("user"):
            return f"Hold {rest[0]} is already {h['status']}."
        _notify("decided", {"hold": h["id"], "status": h["status"]})
        if cmd == "approve":
            mins = int(_STORE.settings().get("approval_minutes", 15))
            return (f"Approved {h['id']}: {h['summary']}\nHermes may send exactly this payment once in the next "
                    f"{mins} minutes. Ask Hermes to go ahead.")
        return f"Rejected {h['id']}: {h['summary']}"
    if cmd in ("freeze", "unfreeze"):
        _STORE.set_setting("frozen", cmd == "freeze")
        _notify("state", {"frozen": cmd == "freeze"})
        return ("FROZEN. Every payment, approval and signature is blocked until /4eyes unfreeze."
                if cmd == "freeze" else "Unfrozen. 4eyes is back to normal checks.")
    if cmd == "trust":
        if len(rest) < 2 or not chain.is_address(rest[-1]):
            return "Usage: /4eyes trust <name> <address>"
        name, addr = " ".join(rest[:-1]), chain.norm(rest[-1])
        for n, p in _STORE.payees().items():
            if chain.lookalike(p["address"], addr):
                return (f"Refused: {chain.short(addr)} looks like {p.get('label', n)}'s address "
                        f"{chain.short(p['address'])} but is different. Check it character by character.")
        _STORE.trust(name, addr)
        _notify("state", {})
        return f"Trusted {name}: {addr}"
    if cmd == "untrust":
        if not rest:
            return "Usage: /4eyes untrust <name>"
        ok = _STORE.untrust(" ".join(rest))
        _notify("state", {})
        return "Removed." if ok else f"No trusted payee called {' '.join(rest)}."
    if cmd in ("payees", "trusted"):
        book = _STORE.payees()
        return "\n".join(f"{p.get('label', n)}: {p['address']}" for n, p in book.items()) or "No trusted payees yet."
    if cmd == "limit":
        if len(rest) < 2:
            return "Usage: /4eyes limit <TOKEN> <per_payment|off> [per_day|off]"
        try:
            per_tx = _num(rest[1])
            per_day = _num(rest[2]) if len(rest) > 2 else (_STORE.limits().get(rest[0].upper()) or {}).get("per_day")
        except ValueError:
            return "Limits must be numbers or 'off'."
        _STORE.set_limit(rest[0], per_tx, per_day)
        return f"{rest[0].upper()}: per payment {per_tx if per_tx is not None else 'no limit'}, per day " \
               f"{per_day if per_day is not None else 'no limit'}"
    if cmd == "newpayees" and rest and rest[0] in ("hold", "allow"):
        _STORE.set_setting("hold_new_payees", rest[0] == "hold")
        return f"First payments to addresses you typed: {rest[0]}."
    if cmd == "unpriced" and rest and rest[0] in ("hold", "allow"):
        _STORE.set_setting("hold_unpriced", rest[0] == "hold")
        return f"Payments 4eyes cannot price: {rest[0]}."
    if cmd == "onetap" and rest and rest[0] in ("on", "off"):
        _STORE.set_setting("one_tap", rest[0] == "on")
        return ("Held payments are put to you through Hermes' approval prompt when you are there to answer."
                if rest[0] == "on" else "Held payments wait for /4eyes approve <id>.")
    if cmd == "cooldown" and rest:
        try:
            _STORE.set_setting("cooldown_minutes", max(0.0, float(rest[0])))
        except ValueError:
            return "Usage: /4eyes cooldown <minutes>"
        return f"New payees: approvals usable after {rest[0]} minute(s)."
    if cmd == "demo" and rest and rest[0] in ("on", "off"):
        _STORE.set_setting("demo", rest[0] == "on")
        if rest[0] == "on":
            return ("Demo wallet ON: Hermes now has demo_wallet_send and demo_wallet_send_transaction. They are "
                    "pretend wallets that move no real funds, and 4eyes guards them like real ones. Start a new "
                    "chat, then ask Hermes to pay someone. /4eyes demo off removes them.")
        return "Demo wallet OFF."
    if cmd == "log":
        rows = _STORE.ledger(15)
        if not rows:
            return "No decisions yet."
        return "\n".join(f"[{r['outcome']}] {r['summary']}" + (f"  ({', '.join(r['reasons'])})" if r["reasons"] else "")
                         for r in rows)
    return HELP


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_tool(name="foureyes_check", toolset="4eyes", schema=CHECK_SCHEMA, handler=tool_check)
    ctx.register_tool(name="foureyes_status", toolset="4eyes", schema=STATUS_SCHEMA, handler=tool_status)
    ctx.register_tool(name="demo_wallet_send", toolset="4eyes", schema=DEMO_SEND_SCHEMA,
                      handler=tool_demo_send, check_fn=_demo_on)
    ctx.register_tool(name="demo_wallet_send_transaction", toolset="4eyes", schema=DEMO_TX_SCHEMA,
                      handler=tool_demo_tx, check_fn=_demo_on)
    ctx.register_command("4eyes", handler=handle_command,
                         description="Approve or reject held payments, freeze, trusted payees, limits",
                         args_hint="[holds|approve <id>|reject <id>|freeze|trust <name> <addr>|demo on|log|help]")
