"""In-process five-stage evidence review graph."""

from __future__ import annotations

from typing import Any

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from langgraph.graph import END, START

from src.nodes.inner.stages import (
    CoverageMatchNode,
    EvidenceNormalizeNode,
    ProvenanceVerifyNode,
    RequirementSpecNode,
    SufficiencyNarrativeNode,
)
from src.schemas.state import State


class EvidenceReviewWorkflowGraph(BaseGraph):
    """A fixed five-stage graph, intentionally independent of the L1 backbone."""

    @property
    def name(self) -> str:
        return "evidence_review_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        if "payload_store" not in self.config:
            raise ValueError("EvidenceReviewWorkflowGraph requires payload_store")

    def register_nodes(self) -> None:
        self._nodes = {
            "normalize": EvidenceNormalizeNode(config=self.config),
            "requirement_spec": RequirementSpecNode(config=self.config),
            "coverage": CoverageMatchNode(),
            "narrative": SufficiencyNarrativeNode(config=self.config),
            "provenance": ProvenanceVerifyNode(config=self.config),
        }

    def add_edges(self) -> None:
        self._sg.add_edge(START, "normalize")
        self._sg.add_edge("normalize", "requirement_spec")
        self._sg.add_edge("requirement_spec", "coverage")
        self._sg.add_edge("coverage", "narrative")
        self._sg.add_edge("narrative", "provenance")
        self._sg.add_edge("provenance", END)

    def route(self, state: State) -> str:
        return "provenance" if state.get("status") == AgentStatus.SUCCESS.value else END

    def get_output(self, state: State) -> dict[str, Any]:
        keys = (
            "evaluation_state", "requirement_segments", "observations", "version_conflict_observations",
            "suggested_additional_evidence", "accountable_owners", "evidence_sources", "unresolved_questions",
            "degradation_reason", "status", "error_code", "error_type", "error_log",
        )
        return {key: state.get(key) for key in keys if key in state}
