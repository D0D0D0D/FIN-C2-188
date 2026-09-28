"""Outer AgentBaseGraph for FIN-C2-188."""

from __future__ import annotations

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import EvidenceReviewWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class ExamEvidenceSufficiencyReviewGraph(AgentBaseGraph):
    """Fixed outer backbone whose main slot hosts the domain subgraph."""

    @property
    def name(self) -> str:
        return "ExamEvidenceSufficiencyReviewAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        super()._validate_config()
        if not isinstance(self.config.get("max_evidence_entries", 200), int):
            raise ValueError("max_evidence_entries must be an integer")
        if "payload_store" not in self.config:
            raise ValueError("payload_store must be configured")

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = PreProcessNode(config=self.config)
        self._nodes["main"] = EvidenceReviewWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = PostProcessNode(config=self.config)


# Compatibility alias for existing scaffold callers.
Graph = ExamEvidenceSufficiencyReviewGraph
