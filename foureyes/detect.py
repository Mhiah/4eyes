"""Recognise tool calls that move crypto, from any plugin, MCP server or terminal command."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from . import chain


def _k(key: Any) -> str:
    """Argument names compared without case, underscores or dashes: toAddress == to_address."""
    return re.sub(r"[_\-\s]", "", str(key).lower())


def _keys(*names: str) -> set[str]:
    return {_k(n) for n in names}


RECIPIENT_KEYS = _keys("to", "recipient", "recipients", "destination", "dest", "dst", "receiver", "payee", "pay_to",
                       "to_address", "recipient_address", "receiver_address", "address", "spender", "operator",
                       "beneficiary", "target", "target_address", "wallet_address", "destination_address",
                       "dest_address", "withdraw_address", "to_wallet", "to_pubkey", "to_account", "account_to")
SKIP_KEYS = _keys("from", "from_address", "from_pubkey", "sender", "owner", "token", "token_address", "token_mint",
                  "token_contract", "mint", "contract", "contract_address", "asset", "token_in", "token_out",
                  "input_mint", "output_mint", "from_token", "to_token", "sell_token", "buy_token", "pool", "router",
                  "chain", "network")
AMOUNT_KEYS = _keys("amount", "value", "quantity", "qty", "amount_in", "sum", "total", "amount_usd", "max_amount",
                    "price", "max_price", "ui_amount")
TOKEN_KEYS = _keys("token", "asset", "symbol", "currency", "coin", "ticker", "token_symbol", "token_in",
                   "token_address", "token_mint", "token_contract", "mint", "input_mint", "from_token", "sell_token")
DATA_KEYS = _keys("data", "input", "calldata", "call_data", "tx_data")
FUNC_KEYS = _keys("function_name", "function", "func", "fn", "method", "function_signature", "signature")
ARGS_KEYS = _keys("args", "arguments", "params", "parameters", "function_args", "inputs")
CONTRACT_KEYS = _keys("to", "address", "contract", "contract_address", "token", "token_address", "target")
MESSAGE_KEYS = _keys("message", "msg", "text", "statement")

MONEY_NAME = re.compile(
    r"(transfer|send_?(token|eth|sol|usdc|crypto|funds|payment|transaction|tx|native|coin)|^send$|(?<!dis)pay(?!load)|"
    r"swap|withdraw|bridge|approv|permit|sign_?(typed|transaction|tx|order)|send_?raw|x402|mpp|purchase|buy|sell|"
    r"trade|place_?order|stake|deposit|wallet_send|execute_?(swap|trade|transaction))", re.I)
NOT_MONEY_NAME = re.compile(r"(send_?(message|email|mail|sms|notification)|^reply|post_?message|notify|dm$|"
                            r"price_?feed)", re.I)
# A whole word in the tool name that marks it read-only. Matched per word, so "budget_transfer" is still
# a transfer even though "get_" hides inside "budget_".
READ_ONLY_WORDS = {"get", "list", "read", "search", "quote", "balance", "balances", "history", "status", "fetch",
                   "lookup", "estimate", "simulate", "preview", "decode", "encode", "describe", "view", "query"}
NAME_TOKENS = {"eth", "sol", "usdc", "usdt", "dai", "btc", "bnb", "matic"}

TERMINAL_TOOLS = {"terminal", "shell", "bash", "run_command", "execute_command", "exec", "run_terminal_cmd", "process"}
CODE_TOOLS = {"execute_code", "run_code", "run_python", "python", "code_interpreter", "execute_python", "run_script",
              "execute_javascript", "run_javascript"}
TERMINAL_MONEY = re.compile(r"\b(cast\s+(send|mktx|publish|wallet\s+sign|erc20\s+(transfer|approve))|"
                            r"solana\s+transfer|spl-token\s+(transfer|approve|authorize)|seth\s+send)\b", re.I)
# Code typed straight into a shell (python -c, node -e, a heredoc) ...
INLINE_CODE = re.compile(r"\b(python[0-9.]*|node|bun|deno|tsx|ts-node|ruby|perl|php)\b[^|;&\n]*?"
                         r"(\s-c\b|\s-e\b|\s--eval\b|\seval\b|<<)", re.I)
# ... or passed to a code tool, that signs or sends a transaction by itself.
SCRIPT_MONEY = re.compile(r"(send_?(raw_?)?transaction|sign_?(and_?send_?)?transaction|sendAndConfirm|"
                          r"eth_send(Raw)?Transaction|writeContract|\.transact\(|setApprovalForAll|"
                          r"SystemProgram\.transfer|createTransferInstruction|createApproveInstruction|"
                          r"walletClient\.|\.sendTransaction\()", re.I)

# "50", "1,000.5", "5e3", "$40", "0.5 ETH". Anything else is not a number 4eyes will guess at.
NUM_RE = re.compile(r"^\s*\$?\s*(\d[\d,_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z][A-Za-z0-9.$₮]{0,11})?\s*$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")


@dataclass
class MoneyAction:
    tool: str
    kind: str                                   # transfer | approve | approve_all | permit | order | swap | payment
    recipients: list[str] = field(default_factory=list)     # | sign | script
    amount: Optional[float] = None              # human units when known
    token: Optional[str] = None
    unlimited: bool = False
    detail: str = ""
    unreadable: list[str] = field(default_factory=list)     # recipients given as a name, not an address
    opaque: str = ""                            # what Hermes is asked to sign, when 4eyes cannot read it
    script: bool = False                        # code that sends a transaction without a wallet tool
    unpriced: str = ""                          # why the amount or token is unknown ("" when both are known)

    def summary(self) -> str:
        if self.kind == "sign":
            return f"Sign {self.opaque or 'something 4eyes cannot read'}  [{self.tool}]"
        what = {"transfer": "Send", "approve": "Approve spending of", "approve_all": "Approve ALL NFTs/tokens for",
                "permit": "Sign a permit for", "order": "Sign a marketplace order", "swap": "Swap",
                "payment": "Pay", "script": "Run code that sends a transaction"}.get(self.kind, self.kind)
        amt = ("UNLIMITED" if self.unlimited and self.kind in ("approve", "permit", "approve_all")
               else (f"{self.amount:g}" if self.amount is not None else ""))
        tok = self.token or ""
        to = ", ".join(chain.short(r) for r in self.recipients) or ", ".join(f'"{u[:40]}"' for u in self.unreadable)
        bits = [what, f"{amt} {tok}".strip()]
        if to:
            bits.append(("to " if self.kind in ("transfer", "payment", "script") else "→ ") + to)
        return " ".join(b for b in bits if b) + f"  [{self.tool}]"


def _walk(node: Any, key: str = "", depth: int = 0) -> Iterator[tuple[str, Any]]:
    """Every (argument name, value) pair, nested ones included. Items of a list carry the list's name."""
    if depth > 6:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield _k(k), v
            yield from _walk(v, _k(k), depth + 1)
    elif isinstance(node, (list, tuple)):
        for v in node:
            if isinstance(v, (dict, list, tuple)):
                yield from _walk(v, key, depth + 1)
            else:
                yield key, v


def _words(tool_name: str) -> list[str]:
    """sendTransaction, send_transaction and mcp__wallet__send-transaction all become [.., send, transaction]."""
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool_name or "")
    return [w for w in re.split(r"[^a-z0-9]+", s.lower()) if w]


def _number(v: Any) -> tuple[Optional[float], bool]:
    """(amount, understood). An amount 4eyes cannot read exactly is reported as not understood rather
    than guessed: "5e3" must not be read as 5."""
    if v is None or isinstance(v, bool):
        return None, v is None
    if isinstance(v, (int, float)):
        f = float(v)
        return (f, True) if math.isfinite(f) and f >= 0 else (None, False)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None, True
        if HEX_RE.match(s):
            return float(int(s, 16)), True
        m = NUM_RE.match(s)
        if m:
            try:
                f = float(m.group(1).replace(",", "").replace("_", ""))
            except ValueError:
                return None, False
            return (f, True) if math.isfinite(f) else (None, False)
    return None, False


def _num(v: Any) -> Optional[float]:
    return _number(v)[0]


def _token_from(v: Any) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        if chain.is_address(s):
            known = chain.known_token(s)
            return known[0] if known else None
        if re.fullmatch(r"[A-Za-z$][A-Za-z0-9.$₮]{1,11}", s):
            return s.upper()
    return None


def _script(tool: str, code: str) -> Optional[MoneyAction]:
    if not SCRIPT_MONEY.search(code):
        return None
    low = code.lower()
    found = sorted(chain.find_addresses(code), key=lambda a: low.find(a.lower()))
    return MoneyAction(tool, "script", [a for a in found if not chain.known_token(a)][:3], script=True,
                       detail=code[:240], unpriced="amount")


def _terminal(tool: str, cmd: str) -> Optional[MoneyAction]:
    if not TERMINAL_MONEY.search(cmd):
        return _script(tool, cmd) if INLINE_CODE.search(cmd) else None
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
    contract = None
    if "(" in cmd and len(ordered) > 1:
        contract = chain.known_token(ordered[0])
        ordered = ordered[1:]          # cast send <token contract> "fn(...)" <recipient/spender> ...
    if not act.recipients:
        act.recipients = ordered[:3]
    nums = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", cmd)
    if nums and not act.unlimited:
        act.amount = float(nums[-1])
        if contract and act.kind == "transfer":      # cast send <USDC> "transfer(address,uint256)" <to> 5000000
            act.token, act.amount = contract[0], act.amount / 10 ** contract[1]
    mv = re.search(r"--value\s+(\d+(?:\.\d+)?)\s*(ether|eth)?\b", low)
    if mv:
        act.amount = float(mv.group(1)) if mv.group(2) else float(mv.group(1)) / 1e18
        act.token = "ETH"
    if "solana" in low:
        act.token = "SOL"
    return act


def _signs(words: list[str], name: str) -> bool:
    """Tools that sign or broadcast: sign_transaction, personal_sign, eth_signTypedData_v4, send_raw_transaction."""
    for i, w in enumerate(words):
        if w == "sign" and (i + 1 >= len(words) or words[i + 1] not in ("in", "up", "out", "off")):
            return True
    return bool(re.search(r"broadcast|send_?raw|submit_?(tx|transaction)", name))


def _priced(d: chain.Decoded, contract: Optional[str]) -> tuple[Optional[float], Optional[str], str]:
    """(amount, token, unpriced) for a decoded token transfer. Raw units only mean something when 4eyes
    knows the token's decimals."""
    known = chain.known_token(contract) if contract else None
    if d.kind != "transfer":
        return None, (known[0] if known else None), ""
    if known and d.amount is not None:
        return d.amount / 10 ** known[1], known[0], ""
    return None, (known[0] if known else None), "amount" if known else "token"


def detect(tool_name: str, args: Any) -> Optional[MoneyAction]:
    """Return a MoneyAction when this tool call can move or unlock crypto, else None."""
    if not isinstance(args, dict):
        return None
    words = _words(tool_name)
    name = "_".join(words)
    read_only = bool(READ_ONLY_WORDS & set(words))
    money_name = bool(MONEY_NAME.search(name)) and not NOT_MONEY_NAME.search(name) and not read_only
    arg_names = {_k(a) for a in args}

    if name in TERMINAL_TOOLS or (arg_names & {"command", "cmd"} and not money_name):
        cmd = args.get("command") or args.get("cmd") or ""
        return _terminal(tool_name, str(cmd)) if isinstance(cmd, str) else None
    if name in CODE_TOOLS or (arg_names & {"code", "script", "source"} and not money_name):
        code = next((args[a] for a in args if _k(a) in ("code", "script", "source")), "")
        return _script(tool_name, code) if isinstance(code, str) else None

    pairs = list(_walk(args))
    decoded = []
    typed = chain.decode_typed_data(args)
    typed_unknown = None if typed else chain.typed_primary(args)
    for k, v in pairs:
        if k in DATA_KEYS and isinstance(v, str):
            d = chain.decode_calldata(v)
            if d:
                decoded.append(d)
        if typed is None:
            typed = chain.decode_typed_data(v)
            if typed is None and typed_unknown is None:
                typed_unknown = chain.typed_primary(v)

    # viem / ethers style: {"functionName": "approve", "args": [spender, amount]}
    if not decoded and not read_only:
        fn = next((v for k, v in pairs if k in FUNC_KEYS and isinstance(v, str)), None)
        call_args = next((v for k, v in pairs if k in ARGS_KEYS and isinstance(v, (list, tuple))), None)
        named = chain.decode_named_call(fn, call_args) if fn else None
        if named:
            decoded.append(named)

    keyed, unreadable, loose = [], [], []
    for k, v in pairs:
        if not isinstance(v, str) or not v.strip():
            continue
        val = v.strip()
        if k in RECIPIENT_KEYS and k not in SKIP_KEYS:
            if chain.is_address(val):
                keyed.append(chain.norm(val))
            elif len(val) <= 120:
                unreadable.append(val)         # "attacker.eth", "Bob": the wallet decides who that is, not 4eyes
        elif k not in SKIP_KEYS and k not in DATA_KEYS and not chain.known_token(val) \
                and (chain.is_evm(val) or val in chain.find_addresses(val)):
            loose.append(chain.norm(val))      # an address under a name 4eyes does not know ("dst")
    keyed, loose = list(dict.fromkeys(keyed)), list(dict.fromkeys(loose))

    raw_amount = next((v for k, v in pairs if k in AMOUNT_KEYS and not isinstance(v, bool) and v not in (None, "")),
                      None)
    amount, understood = _number(raw_amount)
    token_vals = [v for k, v in pairs if k in TOKEN_KEYS and isinstance(v, str) and v.strip()]
    token = next((t for v in token_vals if (t := _token_from(v))), None)
    token_unknown = token is None and any(chain.is_address(v.strip()) for v in token_vals)
    if token is None and not token_unknown:
        if isinstance(raw_amount, str) and (m := NUM_RE.match(raw_amount)) and m.group(2):
            token = m.group(2).upper()
        else:
            token = next((w.upper() for w in words if w in NAME_TOKENS), None)

    if typed:
        act = MoneyAction(tool_name, typed.kind, [typed.target] if typed.target else [], None, token,
                          typed.unlimited, typed.label)
        if typed.amount is not None and not typed.unlimited:
            act.amount = float(typed.amount)
        return act
    if decoded:
        d = next((x for x in decoded if x.unlimited), decoded[0])
        contract = next((v.strip() for k, v in pairs if k in CONTRACT_KEYS and isinstance(v, str)
                         and chain.is_address(v.strip())), None)
        amt, tok, unpriced = _priced(d, contract)
        return MoneyAction(tool_name, d.kind, [d.target] if d.target else [], amt, tok or token, d.unlimited, d.label,
                           unpriced=unpriced)

    # A pre-built transaction, a raw hash or typed data 4eyes has no rule for: it cannot say what signing it does.
    if _signs(words, name) and not read_only:
        blob = any(chain.is_blob(v) for k, v in pairs if isinstance(v, str) and k not in DATA_KEYS)
        readable_message = ("message" in words or "personal" in words or bool(arg_names & MESSAGE_KEYS)) and not blob
        if typed_unknown:
            return MoneyAction(tool_name, "sign", opaque=f"an EIP-712 {typed_unknown[:40]} message")
        if blob:
            return MoneyAction(tool_name, "sign", opaque="a pre-built transaction or raw hash")
        if not readable_message and not keyed and not loose and amount is None:
            return MoneyAction(tool_name, "sign", opaque="a transaction 4eyes was not shown")
        if readable_message and not keyed:
            return None                         # a plain-text login message

    if not (money_name and (keyed or unreadable or loose or raw_amount is not None)):
        return None

    kind = "transfer"
    if re.search(r"approv|permit", name):
        kind = "approve"
    elif re.search(r"swap|trade|buy|sell|order", name):
        kind = "swap"
    elif re.search(r"x402|mpp|pay|purchase", name) and not keyed:
        kind = "payment"
    unlimited = False
    if kind == "approve":
        unlimited = raw_amount is None or (isinstance(raw_amount, str) and raw_amount.strip().lower() in
                                           ("max", "unlimited", "infinite", str(chain.UINT256_MAX))) \
            or (amount is not None and amount >= chain.UNLIMITED_FLOOR)
    # Raw native-coin transaction: {"to": ..., "value": wei}
    if kind == "transfer" and any(k == "value" for k, _ in pairs) and token is None and not token_unknown and keyed:
        n = _num(next(v for k, v in pairs if k == "value"))
        if n is not None and n > 1e12:
            amount, token = n / 1e18, "ETH"
    if kind == "payment" and token is None and not token_unknown and amount is not None:
        token = "USD"                          # x402 / MPP prices are dollars
    unpriced = ""
    if kind in ("transfer", "payment", "swap"):
        unpriced = "amount" if (amount is None or not understood) else ("token" if token is None else "")
    recipients = keyed or (loose if kind != "swap" else [])
    return MoneyAction(tool_name, kind, recipients, None if (unlimited or not understood) else amount, token,
                       unlimited, unreadable=[] if recipients else unreadable, unpriced=unpriced)
