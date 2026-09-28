"""Payload-store access helpers used only by the permitted workflow stages."""

from __future__ import annotations

from typing import Any

from src.payload_store import PayloadStore, PayloadStoreError


def resolve_text(store: PayloadStore, ref: str, *, case_id: str, session_id: str) -> str:
    """Resolve a body without consuming it; stages 1, 4 and 5 may reread it."""

    value: Any = store.resolve(ref, scope={"case_id": case_id}, session_id=session_id, consume=False)
    if not isinstance(value, str):
        raise PayloadStoreError("payload_ref_not_text")
    return value
