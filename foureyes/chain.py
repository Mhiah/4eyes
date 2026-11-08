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
}


def decode_calldata(data: str) -> Optional[Decoded]:
    if not isinstance(data, str) or not CALLDATA_RE.match(data.strip()):
        return None
    d = data.strip()[2:].lower()
    sel = d[:8]
    try:
        if sel == "a9059cbb":
            return Decoded("transfer", _addr(_word(d, 0)), _int(_word(d, 1)), label=SELECTORS[sel])
        if sel in ("23b872dd", "42842e0e", "b88d4fde"):
            return Decoded("transfer", _addr(_word(d, 1)), _int(_word(d, 2)), label=SELECTORS[sel])
        if sel in ("095ea7b3", "39509351"):
            amt = _int(_word(d, 1))
            return Decoded("approve", _addr(_word(d, 0)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
        if sel == "a22cb465":
            on = _int(_word(d, 1)) != 0
            return Decoded("approve_all", _addr(_word(d, 0)), None, on, SELECTORS[sel]) if on else None
        if sel == "d505accf":
            amt = _int(_word(d, 2))
            return Decoded("permit", _addr(_word(d, 1)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
        if sel == "87517c45":
            amt = _int(_word(d, 2))
            return Decoded("approve", _addr(_word(d, 1)), amt, amt >= UNLIMITED_FLOOR, SELECTORS[sel])
    except ValueError:
        return None
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
