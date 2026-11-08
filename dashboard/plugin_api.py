"""Backend routes for the 4eyes Desktop pane, mounted at /api/plugins/4eyes/.

Reads and writes the same JSON files as the agent half, so the pane, /4eyes and the guard always agree.
The agent half refuses any tool call that targets these routes, so the model cannot approve its own payment.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

_PKG_DIR = Path(__file__).resolve().parents[1] / "foureyes"
_PKG_NAME = "_foureyes_pane_core"


def _load_core():
    if _PKG_NAME in sys.modules:
        return sys.modules[_PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        _PKG_NAME, _PKG_DIR / "__init__.py", submodule_search_locations=[str(_PKG_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


_load_core()
from _foureyes_pane_core.store import Store  # noqa: E402

router = APIRouter()
_store = Store()


def _snapshot() -> dict[str, Any]:
    holds = sorted(_store.holds().values(), key=lambda h: h["created"], reverse=True)
    return {
        "frozen": bool(_store.settings().get("frozen")),
        "waiting": [h for h in holds if h["status"] == "held"],
        "payees": [{"name": p.get("label", n), "address": p["address"]} for n, p in _store.payees().items()],
        "limits": _store.limits(),
        "ledger": _store.ledger(25),
    }


@router.get("/state")
async def state() -> dict[str, Any]:
    return _snapshot()


@router.post("/decide")
async def decide(body: dict) -> dict[str, Any]:
    body = body or {}
    hold_id, decision = body.get("id"), body.get("decision")
    if decision not in ("approve", "reject") or not hold_id:
        raise HTTPException(status_code=400, detail="id and decision (approve|reject) are required")
    h = _store.decide(str(hold_id), "approved" if decision == "approve" else "rejected", "user:desktop")
    if not h:
        raise HTTPException(status_code=404, detail=f"no hold {hold_id}")
    return _snapshot()


@router.post("/freeze")
async def freeze(body: dict) -> dict[str, Any]:
    _store.set_setting("frozen", bool((body or {}).get("on")))
    return _snapshot()
