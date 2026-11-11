"""4eyes state: payee book, limits, holds and the ledger, as JSON in the plugin's data folder."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_LIMITS = {
    "USDC": {"per_tx": 100.0, "per_day": 500.0},
    "USDT": {"per_tx": 100.0, "per_day": 500.0},
    "DAI": {"per_tx": 100.0, "per_day": 500.0},
    "ETH": {"per_tx": 0.05, "per_day": 0.2},
    "SOL": {"per_tx": 1.0, "per_day": 5.0},
    "USD": {"per_tx": 100.0, "per_day": 500.0},      # x402 / MPP payments priced in dollars
}
DEFAULT_SETTINGS = {"frozen": False, "hold_new_payees": True, "hold_unpriced": True, "one_tap": True,
                    "approval_minutes": 15, "cooldown_minutes": 0}
LEDGER_MAX = 200


PLUGIN_ID = "4eyes"


def _default_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir  # Hermes' documented per-plugin data dir
    return plugin_data_dir(PLUGIN_ID)


class Store:
    """Every read goes to disk, so the agent, the gateway and the Desktop pane (which can be separate
    processes) always see the same holds, approvals and payees. ``memory=True`` keeps state in RAM
    (tests)."""

    def __init__(self, root: Optional[Path] = None, memory: bool = False):
        self._root = Path(root) if root else None
        self._memory_only = memory
        self._lock = threading.RLock()
        self._mem: dict[str, Any] = {}

    def bind(self, root: Path) -> None:
        self._root = Path(root)
        self._mem.clear()

    # --- io ----------------------------------------------------------------------------------------
    def _path(self, name: str) -> Optional[Path]:
        if self._memory_only:
            return None
        if self._root is None:
            try:
                self._root = Path(_default_dir())
            except Exception:
                self._memory_only = True
                return None
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / f"{name}.json"

    def _load(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if p is None:
            if name not in self._mem:
                self._mem[name] = default
            return self._mem[name]
        val = default
        if p.exists():
            try:
                val = json.loads(p.read_text("utf-8"))
            except (OSError, ValueError):
                val = default
        self._mem[name] = val
        return val

    def _save(self, name: str) -> None:
        p = self._path(name)
        if p is None:
            return
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._mem[name], fh, indent=1)
        os.replace(tmp, p)

    # --- settings / limits -------------------------------------------------------------------------
    def settings(self) -> dict:
        with self._lock:
            s = self._load("settings", {})
            return {**DEFAULT_SETTINGS, **s}

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            s = self._load("settings", {})
            s[key] = value
            self._save("settings")

    def limits(self) -> dict:
        with self._lock:
            lim = self._load("limits", None)
            if lim is None:
                lim = {k: dict(v) for k, v in DEFAULT_LIMITS.items()}
                self._mem["limits"] = lim
            return lim

    def set_limit(self, token: str, per_tx: Optional[float], per_day: Optional[float]) -> None:
        with self._lock:
            lim = self.limits()
            lim[token.upper()] = {"per_tx": per_tx, "per_day": per_day}
            self._save("limits")

    # --- payees ------------------------------------------------------------------------------------
    def payees(self) -> dict:
        with self._lock:
            return self._load("payees", {})

    def trust(self, name: str, address: str, note: str = "") -> None:
        with self._lock:
            book = self.payees()
            book[name.strip().lower()] = {"address": address, "added": time.time(), "note": note,
                                          "label": name.strip()}
            self._save("payees")

    def untrust(self, name: str) -> bool:
        with self._lock:
            book = self.payees()
            gone = book.pop(name.strip().lower(), None)
            self._save("payees")
            return gone is not None

    def payee_for(self, address: str) -> Optional[str]:
        for name, p in self.payees().items():
            if p["address"].lower() == address.lower():
                return p.get("label", name)
        return None

    # --- spend -------------------------------------------------------------------------------------
    def spent_today(self, token: str) -> float:
        day = time.strftime("%Y-%m-%d")
        return float(self._load("spent", {}).get(day, {}).get(token.upper(), 0.0))

    def add_spend(self, token: str, amount: float) -> None:
        with self._lock:
            day = time.strftime("%Y-%m-%d")
            sp = self._load("spent", {})
            for d in list(sp):
                if d != day:
                    sp.pop(d)
            sp.setdefault(day, {})
            sp[day][token.upper()] = sp[day].get(token.upper(), 0.0) + amount
            self._save("spent")

    # --- holds -------------------------------------------------------------------------------------
    def holds(self) -> dict:
        with self._lock:
            return self._load("holds", {})

    def put_hold(self, hold: dict) -> None:
        with self._lock:
            h = self.holds()
            h[hold["id"]] = hold
            # Keep the file small: drop finished holds older than a day.
            cutoff = time.time() - 86400
            for k in [k for k, v in h.items() if v["status"] != "held" and v.get("decided_at", 0) < cutoff]:
                h.pop(k)
            self._save("holds")

    def decide(self, hold_id: str, status: str, by: str = "user") -> Optional[dict]:
        with self._lock:
            h = self.holds().get(hold_id)
            if not h or h["status"] != "held":
                return h
            h["status"], h["decided_at"], h["decided_by"] = status, time.time(), by
            self._save("holds")
            return h

    def mark_asked(self, hold_id: str, call_id: str = "") -> int:
        """Record that this hold was put to the user through Hermes' approval prompt. Returns how many
        times it has been asked (0 if the hold is gone or already answered)."""
        with self._lock:
            h = self.holds().get(hold_id)
            if not h or h["status"] != "held":
                return 0
            h["asked"], h["asked_at"], h["asked_call"] = int(h.get("asked", 0)) + 1, time.time(), call_id
            self._save("holds")
            return h["asked"]

    def asked_hold(self, fp: str, call_id: str = "", within: float = 3600.0) -> Optional[dict]:
        """The hold that was put to the user for exactly this call, if any."""
        now = time.time()
        for h in self.holds().values():
            if h.get("fingerprint") != fp or not h.get("asked") or h["status"] not in ("held", "approved"):
                continue
            if now - h.get("asked_at", 0) > within:
                continue
            if call_id and h.get("asked_call") and h["asked_call"] != call_id:
                continue
            return h
        return None

    # --- ledger ------------------------------------------------------------------------------------
    def log(self, entry: dict) -> None:
        with self._lock:
            led = self._load("ledger", [])
            led.append({"at": time.time(), **entry})
            del led[:-LEDGER_MAX]
            self._save("ledger")

    def ledger(self, n: int = 20) -> list:
        return list(reversed(self._load("ledger", [])[-n:]))
