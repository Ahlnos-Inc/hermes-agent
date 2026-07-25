"""Readback tests for injected Kanban protocol guidance."""

from __future__ import annotations

from agent.prompt_builder import KANBAN_GUIDANCE


def test_kanban_worker_guidance_documents_block_immediate_boundary():
    # Phrased "A push/open-PR failure ..." since the BUILD-663 rewrite. The
    # constant has a hard 5500-char budget, so pin the contract, not the prose.
    assert "push/open-PR failure" in KANBAN_GUIDANCE
    assert "auth boundary" in KANBAN_GUIDANCE
    assert "external side-effect failure" in KANBAN_GUIDANCE
    assert "kanban_comment" in KANBAN_GUIDANCE
    assert "kanban_block" in KANBAN_GUIDANCE
    assert "do not burn iterations" in KANBAN_GUIDANCE


def test_kanban_orchestrator_guidance_documents_dependency_pattern():
    assert "Orchestrator mode" in KANBAN_GUIDANCE
    assert "kanban_compile_workflow" in KANBAN_GUIDANCE
    assert "compile once" in KANBAN_GUIDANCE
    assert "stable workflow_key" in KANBAN_GUIDANCE
    assert "yield" in KANBAN_GUIDANCE
    assert "exactly one terminal" in KANBAN_GUIDANCE
    assert "discover profiles first" in KANBAN_GUIDANCE
    assert "Do not assign follow-up work to yourself" in KANBAN_GUIDANCE
    orchestrator_block = KANBAN_GUIDANCE.split("## Orchestrator mode", 1)[1].split(
        "## Reference details", 1
    )[0]
    assert "return the workflow and terminal task ids" in orchestrator_block
    assert "complete your own card" not in orchestrator_block
