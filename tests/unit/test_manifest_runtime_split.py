"""AgentRegistry manifest and runtime-config split regression tests."""

from pathlib import Path

import yaml

from src.api.server import _runtime_config


ROOT = Path(__file__).parents[2]
RUNTIME_VALUES = {
    "max_retry": 3,
    "timeout_seconds": 60,
    "memory_enabled": False,
    "max_evidence_entries": 200,
    "max_requirement_segments": 50,
    "payload_ttl_seconds": 300,
    "max_quotable_span_chars": 120,
    "min_quotable_span_chars": 8,
    "max_text_chars": 200000,
}


def test_agent_manifest_is_root_level_and_runtime_values_are_moved_intact() -> None:
    manifest = yaml.safe_load((ROOT / "config" / "agent.yaml").read_text(encoding="utf-8"))
    runtime = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))

    assert "agent" not in manifest
    assert "config" not in manifest
    assert "sufficiency_rubric" not in manifest
    assert manifest["namespace"] == "fin"
    assert manifest["enabled"] is True
    assert manifest["class"] == "src.graph.graph.ExamEvidenceSufficiencyReviewGraph"
    assert {key: runtime[key] for key in RUNTIME_VALUES} == RUNTIME_VALUES
    assert runtime["sufficiency_rubric"]["rubric_id"] == "fsa-exam-evidence-v1"
    assert _runtime_config() == runtime
