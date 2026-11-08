"""Recognise tool calls that move crypto, from any plugin, MCP server or terminal command."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from . import chain

RECIPIENT_KEYS = {"to", "recipient", "destination", "dest", "receiver", "payee", "to_address", "toaddress",
                  "recipient_address", "address", "spender", "operator", "beneficiary", "target_address",
                  "wallet_address", "destination_address", "to_wallet"}
SKIP_KEYS = {"from", "from_address", "fromaddress", "sender", "owner", "token", "token_address", "tokenaddress",
             "mint", "contract", "contract_address", "asset", "token_in", "token_out", "tokenin", "tokenout",
             "input_mint", "output_mint", "pool", "router", "chain", "network"}
AMOUNT_KEYS = {"amount", "value", "quantity", "qty", "amount_in", "amountin", "sum", "total", "amount_usd",
               "max_amount", "price", "max_price", "ui_amount", "uiamount"}
TOKEN_KEYS = {"token", "asset", "symbol", "currency", "coin", "ticker", "token_symbol", "token_in", "tokenin"}
DATA_KEYS = {"data", "input", "calldata", "call_data", "tx_data"}

MONEY_NAME = re.compile(
    r"(transfer|send_?(token|eth|sol|usdc|crypto|funds|payment|transaction|tx|native|coin)|^send$|pay|swap|"
    r"withdraw|bridge|approv|permit|sign_?(typed|transaction|tx|order)|send_?raw|x402|mpp|purchase|buy|sell|"
    r"trade|place_?order|stake|deposit|wallet_send|execute_?(swap|trade|transaction))", re.I)
NOT_MONEY_NAME = re.compile(r"(send_?message|send_?email|send_?mail|reply|post_?message|notify|dm$|"
                            r"get_|list_|read_|search|quote|price_?feed|balance|history|status)", re.I)
TERMINAL_TOOLS = {"terminal", "shell", "bash", "run_command", "execute_command", "exec", "run_terminal_cmd"}
TERMINAL_MONEY = re.compile(r"\b(cast\s+(send|mktx|wallet\s+sign|erc20\s+(transfer|approve))|"
                            r"solana\s+transfer|spl-token\s+(transfer|approve)|seth\s+send)\b", re.I)


@dataclass
class MoneyAction:
    tool: str
    kind: str                                   # transfer | approve | approve_all | permit | order | swap | payment
    recipients: list[str] = field(default_factory=list)
    amount: Optional[float] = None              # human units when known
    token: Optional[str] = None
    unlimited: bool = False
    detail: str = ""

    def summary(self) -> str:
        what = {"transfer": "Send", "approve": "Approve spending of", "approve_all": "Approve ALL NFTs/tokens for",
                "permit": "Sign a permit for", "order": "Sign a marketplace order", "swap": "Swap",
                "payment": "Pay"}.get(self.kind, self.kind)
        amt = ("UNLIMITED" if self.unlimited and self.kind in ("approve", "permit", "approve_all")
               else (f"{self.amount:g}" if self.amount is not None else ""))
        tok = self.token or ""
        to = ", ".join(chain.short(r) for r in self.recipients)
        bits = [what, f"{amt} {tok}".strip()]
        if to:
            bits.append(("to " if self.kind in ("transfer", "payment") else "→ ") + to)
        return " ".join(b for b in bits if b) + f"  [{self.tool}]"


def _walk(node: Any, key: str = "", depth: int = 0) -> Iterator[tuple[str, Any]]:
    if depth > 5:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k).lower(), v
            yield from _walk(v, str(k).lower(), depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _walk(v, key, depth + 1)


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", v)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _token_from(v: Any) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        if chain.is_address(s):
            return None
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{1,11}", s):
            return s.upper()
    return None


def _terminal(tool: str, cmd: str) -> Optional[MoneyAction]:
    if not TERMINAL_MONEY.search(cmd):
        return None
    low = cmd.lower()
    act = MoneyAction(tool, "transfer", detail=cmd[:240])
    if "approve" in low or "setapprovalforall" in low:
        act.kind = "approve_all" if "setapprovalforall" in low else "approve"
        act.unlimited = (str(chain.UINT256_MAX) in cmd or "0x" + "f" * 64 in low or "max" in low.split()
                         or "setapprovalforall" in low)
    for m in re.finditer(r"0x[0-9a-fA-F]{8,}", cmd):
        dec = chain.decode_calldata(m.group(0))
        if dec:
            act.kind, act.unlimited = dec.kind, dec.unlimited
            if dec.target:
                act.recipients.append(dec.target)
    ordered = sorted(chain.find_addresses(cmd), key=lambda a: low.find(a.lower()))
    if "(" in cmd and len(ordered) > 1:
        ordered = ordered[1:]          # cast send <token contract> "fn(...)" <recipient/spender> ...
    if not act.recipients:
        act.recipients = ordered[:3]
    m = re.search(r"(?:transfer|send)\s+\S*\s*(\d+(?:\.\d+)?)", low)
    nums = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", cmd)
    if nums:
        act.amount = float(nums[-1]) if not act.unlimited else None
    mv = re.search(r"--value\s+(\d+(?:\.\d+)?)\s*(ether|eth)?\b", low)
    if mv:
        act.amount = float(mv.group(1)) if mv.group(2) else float(mv.group(1)) / 1e18
        act.token = "ETH"
    if "solana" in low:
        act.token = "SOL"
    return act


def detect(tool_name: str, args: Any) -> Optional[MoneyAction]:
    """Return a MoneyAction when this tool call can move or unlock crypto, else None."""
    name = (tool_name or "").lower()
    if not isinstance(args, dict):
        return None
    if name in TERMINAL_TOOLS or (set(args) & {"command", "cmd"} and not MONEY_NAME.search(name)):
        cmd = args.get("command") or args.get("cmd") or ""
        return _terminal(tool_name, str(cmd)) if isinstance(cmd, str) else None

    pairs = list(_walk(args))
    decoded = []
    typed = None
    for k, v in pairs:
        if k in DATA_KEYS and isinstance(v, str):
            d = chain.decode_calldata(v)
            if d:
                decoded.append(d)
        if typed is None:
            t = chain.decode_typed_data(v)
            if t:
                typed = t

    keyed = []
    for k, v in pairs:
        if isinstance(v, str) and k in RECIPIENT_KEYS and k not in SKIP_KEYS and chain.is_address(v.strip()):
            keyed.append(chain.norm(v))
    amount = next((n for k, v in pairs if k in AMOUNT_KEYS and (n := _num(v)) is not None), None)
    token = next((t for k, v in pairs if k in TOKEN_KEYS and (t := _token_from(v))), None)
    if token is None:
        for k, v in pairs:
            if k in AMOUNT_KEYS and isinstance(v, str):
                m = re.search(r"\d\s*([A-Za-z]{2,10})\b", v)
                if m:
                    token = m.group(1).upper()
                    break

    money_name = bool(MONEY_NAME.search(name)) and not NOT_MONEY_NAME.search(name)
    if not (decoded or typed or (money_name and (keyed or amount is not None))):
        return None

    if typed:
        act = MoneyAction(tool_name, typed.kind, [typed.target] if typed.target else [], None, token,
                          typed.unlimited, typed.label)
        if typed.amount is not None and not typed.unlimited:
            act.amount = float(typed.amount)
        return act
    if decoded:
        d = decoded[0]
        act = MoneyAction(tool_name, d.kind, [d.target] if d.target else [], None, token, d.unlimited, d.label)
        if d.kind == "transfer" and amount is not None:
            act.amount = amount
        return act

    kind = "transfer"
    if re.search(r"approv|permit", name):
        kind = "approve"
    elif re.search(r"swap|trade|buy|sell|order", name):
        kind = "swap"
    elif re.search(r"x402|mpp|pay|purchase", name) and not keyed:
        kind = "payment"
    unlimited = False
    if kind == "approve":
        raw = next((v for k, v in pairs if k in AMOUNT_KEYS), None)
        unlimited = raw is None or (isinstance(raw, str) and raw.strip().lower() in
                                    ("max", "unlimited", "infinite", str(chain.UINT256_MAX))) \
            or (amount is not None and amount >= chain.UNLIMITED_FLOOR)
    # Raw native-coin transaction: {"to": ..., "value": wei}
    if kind == "transfer" and any(k == "value" for k, _ in pairs) and token is None and keyed:
        v = next(v for k, v in pairs if k == "value")
        n = _num(v) if not (isinstance(v, str) and v.startswith("0x")) else int(v, 16)
        if n is not None and n > 1e12:
            amount, token = n / 1e18, "ETH"
    return MoneyAction(tool_name, kind, keyed, None if unlimited else amount, token, unlimited)
