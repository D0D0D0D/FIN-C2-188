"""Main GraphNode contract tests."""

import inspect

from framework.schemas.agent_status import AgentStatus
from src.nodes.main_node import EvidenceReviewWorkflowGraphNode, MainNode


def test_main_node_is_cached_cat2_graph_node_and_declares_trust():
    node = MainNode(config={"payload_store": object()})
    assert isinstance(node, EvidenceReviewWorkflowGraphNode)
    assert node.propagate_hitl is False
    assert node.error_strategy == "propagate"


def test_main_node_short_circuits_upstream_error_without_subgraph():
    node = MainNode(config={"payload_store": object()})
    state = {"status": AgentStatus.ERROR.value, "correlation_id": "c"}
    assert node._extra_security_gate_input(state)["status"] == AgentStatus.ERROR.value
    assert node._subgraph is None


def test_execute_signature_and_no_invoke_impl():
    params = list(inspect.signature(MainNode.execute).parameters)
    assert params[:2] == ["self", "state"]
    assert "_invoke_impl" not in MainNode.__dict__
