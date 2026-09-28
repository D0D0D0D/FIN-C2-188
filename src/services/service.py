"""Service boundary marker for the no-external-dependency template."""

from __future__ import annotations

from typing import Any


class Service:
    """Explicitly reject accidental external service use in this template."""

    def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        raise RuntimeError("FIN-C2-188 has no external service boundary; inject an LLM fake through config['llm']")
