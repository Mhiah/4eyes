"""Addresses, calldata and signed-message decoding for EVM chains and Solana. No network, no deps."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

EVM_RE = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
SOL_RE = re.compile(r"(?<![0-9A-Za-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![0-9A-Za-z])")
CALLDATA_RE = re.compile(r"^0x[0-9a-fA-F]{8}(?:[0-9a-fA-F]{2})*$")

UINT256_MAX = 2 ** 256 - 1
# An allowance this large is "everything, forever" in practice (also covers uint160/uint96 maxima).
UNLIMITED_FLOOR = 2 ** 95

# Contracts that legitimately take open-ended allowances. Users can add their own with /4eyes trust.
KNOWN_SPENDERS = {
    "0x000000000022d473030f116ddee9f6b43ac78ba3": "Uniswap Permit2",
}

# Token contracts 4eyes can name and price, so limits still apply when a wallet tool passes the contract
# address instead of a symbol. address -> (symbol, decimals). EVM addresses from the Uniswap default token
# list, Solana mints from the Jupiter verified list.
KNOWN_TOKENS = {
    # Ethereum
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6),
    "0x6b175474e89094c44da98b954eedeac495271d0f": ("DAI", 18),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18),
    # Base
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": ("USDC", 6),
    "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": ("USDC", 6),     # USDbC
    "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": ("USDT", 6),
    "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": ("DAI", 18),
    "0x4200000000000000000000000000000000000006": ("WETH", 18),    # also Optimism
    # Arbitrum
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831": ("USDC", 6),
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8": ("USDC", 6),     # USDC.e
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": ("USDT", 6),
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1": ("DAI", 18),     # also Optimism
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": ("WETH", 18),
    # Optimism
    "0x0b2c639c533813f4aa9d7837caf62653d097ff85": ("USDC", 6),
    "0x7f5c764cbc14f9669b88837ca1490cca17c31607": ("USDC", 6),     # USDC.e
    "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58": ("USDT", 6),
    # Polygon
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": ("USDC", 6),
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": ("USDC", 6),     # USDC.e
    "0xc2132d05d31c914a87c6611c10748aeb04b58e8f": ("USDT", 6),
    "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063": ("DAI", 18),
    "0x7ceb23fd6bc0add59e62ac25578270cff1b9f619": ("WETH", 18),
    # BNB Chain (18 decimals there)
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": ("USDC", 18),
    "0x55d398326f99059ff775485246999027b3197955": ("USDT", 18),
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": ("DAI", 18),
    "0x2170ed0880ac9a755fd29b2688956bd959f933f8": ("WETH", 18),
    # Solana mints (case-sensitive)
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", 6),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", 6),
    "So11111111111111111111111111111111111111112": ("SOL", 9),
}

# Symbols that spend from the same limit as another one.
LIMIT_ALIASES = {"WETH": "ETH", "WSOL": "SOL", "USDC.E": "USDC", "USDBC": "USDC", "USDT0": "USDT", "USD₮0": "USDT"}


def known_token(addr: str):
    """(symbol, decimals) for a token contract 4eyes knows, else None."""
    if not isinstance(addr, str):
        return None
    a = addr.strip()
    return KNOWN_TOKENS.get(a.lower() if a.lower().startswith("0x") else a)


def limit_token(symbol):
    """The symbol whose limit a token spends from (WETH spends the ETH limit)."""
    if not symbol:
        return None
    s = str(symbol).upper()
    return LIMIT_ALIASES.get(s, s)


def norm(addr: str) -> str:
    addr = addr.strip()
    return addr.lower() if addr.lower().startswith("0x") else addr


def is_evm(addr: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""))


def is_sol(addr: str) -> bool:
    return bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr or "")) and not is_evm(addr)


def is_address(s: str) -> bool:
    return is_evm(s) or is_sol(s)


def find_addresses(text: str) -> set[str]:
    """Every EVM or Solana address in a blob of text (emails, web pages, tool output)."""
    if not text:
        return set()
    found = {norm(m) for m in EVM_RE.findall(text)}
    for m in SOL_RE.findall(text):
        # Base58 words of 32+ chars without digits are almost never real prose; keep only mixed ones.
        if any(c.isdigit() for c in m) and any(c.isalpha() for c in m):
            found.add(m)
    return found


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 14 else addr


def lookalike(a: str, b: str) -> bool:
    """Address poisoning: a different address that starts and ends like one you trust."""
    a, b = norm(a), norm(b)
    if a == b:
        return False
    if is_evm(a) and is_evm(b):
        a, b = a[2:], b[2:]
    elif not (is_sol(a) and is_sol(b)):
        return False
    return a[:4] == b[:4] and a[-4:] == b[-4:]


# --- calldata ----------------------------------------------------------------------------------------

@dataclass
class Decoded:
    kind: str                      # transfer | approve | approve_all | permit
    target: Optional[str] = None   # recipient, spender or operator
    amount: Optional[int] = None   # raw token units
    unlimited: bool = False
    label: str = ""


def _word(data: str, i: int) -> str:
    return data[8 + 64 * i: 8 + 64 * (i + 1)]


def _addr(w: str) -> str:
    return "0x" + w[-40:].lower()


def _int(w: str) -> int:
    return int(w or "0", 16)


SELECTORS = {
    "a9059cbb": "transfer(address,uint256)",
    "23b872dd": "transferFrom(address,address,uint256)",
    "095ea7b3": "approve(address,uint256)",
    "39509351": "increaseAllowance(address,uint256)",
    "a22cb465": "setApprovalForAll(address,bool)",
    "d505accf": "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",
    "87517c45": "Permit2.approve(address,address,uint160,uint48)",
    "42842e0e": "safeTransferFrom(address,address,uint256)",
    "b88d4fde": "safeTransferFrom(address,address,uint256,bytes)",
    "f242432a": "safeTransferFrom(address,address,uint256,uint256,bytes)",
    "2eb2c2d6": "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)",
    "8fcbaf0c": "permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)",
}
# Selectors whose amount word is a fungible token amount (not an NFT id).
FUNGIBLE_TRANSFERS = {"a9059cbb", "23b872dd"}


def decode_calldata(data: str) -> Optional[Decoded]:
    if not isinstance(data, str) or not CALLDATA_RE.match(data.strip()):
        return None
    d = data.strip()[2:].lower()
    sel = d[:8]
    try:
        if sel == "a9059cbb":
            return Decoded("transfer", _addr(_word(d, 0)), _int(_word(d, 1)), label=SELECTORS[sel])
        if sel == "23b872dd":
            return Decoded("transfer", _addr(_word(d, 1)), _int(_word(d, 2)), label=SELECTORS[sel])
        if sel in ("42842e0e", "b88d4fde", "f242432a", "2eb2c2d6"):     # NFT moves: the number is an id
            return Decoded("transfer", _addr(_word(d, 1)), None, label=SELECTORS[sel])
        if sel in ("095ea7b3", "39509351"):
            amt = _int(_word(d, 1))
            return Decoded("approve", _addr(_word(d, 0)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
        if sel == "a22cb465":
            on = _int(_word(d, 1)) != 0
            return Decoded("approve_all", _addr(_word(d, 0)), None, on, SELECTORS[sel]) if on else None
        if sel == "d505accf":
            amt = _int(_word(d, 2))
            return Decoded("permit", _addr(_word(d, 1)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
        if sel == "8fcbaf0c":       # DAI-style permit: allowed=true means unlimited
            on = _int(_word(d, 4)) != 0
            return Decoded("permit", _addr(_word(d, 1)), None, True, SELECTORS[sel]) if on else None
        if sel == "87517c45":
            amt = _int(_word(d, 2))
            return Decoded("approve", _addr(_word(d, 1)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
    except ValueError:
        return None
    return None


def _as_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        t = v.strip().lower().rstrip("n")
        if t in ("max", "maxuint256", "unlimited", "infinite", "type(uint256).max"):
            return UINT256_MAX
        try:
            return int(t, 0)
        except ValueError:
            try:
                return int(float(t))
            except ValueError:
                return None
    return None


def decode_named_call(fn: Any, call_args: Any) -> Optional[Decoded]:
    """The same decoding for tools that pass a function name and an argument list instead of calldata
    (viem ``writeContract``, ethers, most EVM MCP servers)."""
    if not isinstance(fn, str) or not isinstance(call_args, (list, tuple)):
        return None
    name = fn.strip().split("(")[0].strip().lower()
    a = [x.strip() if isinstance(x, str) else x for x in call_args]

    def addr(i: int) -> Optional[str]:
        return norm(a[i]) if len(a) > i and isinstance(a[i], str) and is_evm(a[i]) else None

    def num(i: int) -> Optional[int]:
        return _as_int(a[i]) if len(a) > i else None

    label = f"{fn.strip().split('(')[0]}(…)"
    if name == "transfer":
        return Decoded("transfer", addr(0), num(1), label=label)
    if name == "transferfrom":
        return Decoded("transfer", addr(1), num(2), label=label)
    if name in ("safetransferfrom", "safebatchtransferfrom"):
        return Decoded("transfer", addr(1), None, label=label)
    if name in ("approve", "increaseallowance"):
        if len(a) >= 4 and addr(1):                       # Permit2.approve(token, spender, amount, expiry)
            amt = num(2)
            return Decoded("approve", addr(1), amt, amt is None or amt >= UNLIMITED_FLOOR, label)
        amt = num(1)
        return Decoded("approve", addr(0), amt, amt is None or amt >= UNLIMITED_FLOOR, label)
    if name == "setapprovalforall":
        on = len(a) < 2 or (a[1] not in (False, 0, "0", "false", "False"))
        return Decoded("approve_all", addr(0), None, True, label) if on else None
    if name == "permit":
        amt = num(2)
        return Decoded("permit", addr(1), amt, amt is None or amt >= UNLIMITED_FLOOR, label)
    return None


# --- signed messages (EIP-712) ------------------------------------------------------------------------

PERMIT_TYPES = {"permit", "permitsingle", "permitbatch", "permittransferfrom", "permitbatchtransferfrom",
                "permitwitnesstransferfrom", "permitbatchwitnesstransferfrom"}
ORDER_TYPES = {"ordercomponents", "order", "bulkorder"}


def _as_obj(v: Any) -> Any:
    if isinstance(v, str) and "primaryType" in v:
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return None
    return v


def decode_typed_data(v: Any) -> Optional[Decoded]:
    """Spot signatures that hand over tokens: ERC-2612 / Permit2 permits and NFT marketplace orders."""
    obj = _as_obj(v)
    if not isinstance(obj, dict) or "primaryType" not in obj:
        return None
    ptype = str(obj.get("primaryType", "")).lower()
    msg = obj.get("message") or {}
    if ptype in PERMIT_TYPES:
        details = msg.get("details") or msg.get("permitted") or {}
        if isinstance(details, list):
            details = details[0] if details else {}
        raw = msg.get("value", details.get("amount") if isinstance(details, dict) else None)
        try:
            amt = int(str(raw), 0) if raw is not None else None
        except ValueError:
            amt = None
        spender = msg.get("spender") or ""
        return Decoded("permit", norm(spender) if spender else None, amt,
                       amt is None or amt >= UNLIMITED_FLOOR, f"EIP-712 {obj.get('primaryType')}")
    if ptype in ORDER_TYPES:
        return Decoded("order", None, None, True, f"EIP-712 {obj.get('primaryType')} (marketplace order)")
    return None


def typed_primary(v: Any) -> Optional[str]:
    """The primaryType of an EIP-712 payload, whatever it is (None when ``v`` is not typed data)."""
    obj = _as_obj(v)
    if isinstance(obj, dict) and "primaryType" in obj:
        return str(obj.get("primaryType") or "?")
    return None


BLOB_HEX = re.compile(r"^0x[0-9a-fA-F]{64,}$")
BLOB_B64 = re.compile(r"^[A-Za-z0-9+/_-]{64,}={0,2}$")


def is_blob(v: Any) -> bool:
    """A serialized transaction, a raw hash, or any other signing payload a human cannot read."""
    if not isinstance(v, str):
        return False
    t = v.strip()
    return bool(BLOB_HEX.match(t) or (BLOB_B64.match(t) and not is_address(t)))
