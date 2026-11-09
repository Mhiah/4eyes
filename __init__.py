"""4eyes: the four-eyes rule for AI agents. No crypto leaves your wallet without a second pair of eyes.

Hermes surfaces used (all public):
  pre_llm_call    remember the addresses, amounts and payee names YOU typed this conversation
  post_tool_call  remember addresses that only showed up in content Hermes read (emails, pages, files)
  pre_tool_call   inspect every tool call; hold or refuse payments, approvals and signatures that look wrong
  /4eyes          approve or reject holds, freeze everything, manage trusted payees and limits (human only)
  tools           foureyes_check (dry run) and foureyes_status (read-only); the model can never approve
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from typing import Any, Optional

from .foureyes import chain
from .foureyes.detect import detect
from .foureyes.guard import Provenance, evaluate
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
    for k in ("conversation_id", "session_id", "task_id"):
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
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        _PROV.note_content(_cid(kwargs), text)
    except Exception:
        logger.debug("4eyes: could not record tool output", exc_info=True)
    return None


_TAMPER = re.compile(r"(plugin-data[\\/]+4eyes|/api/plugins/4eyes|/4eyes\s+(approve|unfreeze|trust|untrust|limit|"
                     r"newpayees|cooldown|reject))", re.I)


def _tampering(tool_name: str, args: Any) -> bool:
    """The model must never release its own payment: no running /4eyes through a nested Hermes, no calling
    the pane's backend, no editing 4eyes' files."""
    try:
        blob = json.dumps(args, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    return bool(_TAMPER.search(blob))


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
