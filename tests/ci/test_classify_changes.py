"""Tests for scripts/ci/classify_changes.py.

Check some common patterns of file modifications and the CI lanes they should run.
We should always fail open. We may run a lane we didn't need, never skip one a
change could have broken.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify

DEFAULT = {
    "python": True,
    "frontend": True,
    "docker_meta": True,
    "site": True,
    "scan": True,
    "deps": True,
    "npm_lock": True,
    "mcp_catalog": False,
    "ci_review": True,
}


def _lanes(python=False, frontend=False, site=False, scan=False, deps=False, npm_lock=False, mcp_catalog=False, docker_meta=False, ci_review=False) -> dict[str, bool]:
    return {
        "python": python,
        "frontend": frontend,
        "docker_meta": docker_meta,
        "site": site,
        "scan": scan,
        "deps": deps,
        "npm_lock": npm_lock,
        "mcp_catalog": mcp_catalog,
        "ci_review": ci_review,
    }


CASES = {
    "docs-only → nothing heavy": (["README.md", "docs/guide.md"], _lanes()),
    "python source → python": (["run_agent.py"], _lanes(python=True, scan=True)),
    "dep manifest → python": (["pyproject.toml"], _lanes(python=True, scan=True, deps=True)),
    "uv.lock → python": (["uv.lock"], _lanes(python=True)),
    "ts package → frontend": (["apps/desktop/src/app.tsx"], _lanes(frontend=True)),
    "ui-tui → frontend": (["ui-tui/src/entry.ts"], _lanes(frontend=True)),
    # Lockfile bump shifts every TS package's tree, but not the Python suite.
    "root lockfile → frontend, not python": (["package-lock.json"], _lanes(frontend=True, npm_lock=True)),
    "nested lockfile → npm_lock": (["website/package-lock.json"], _lanes(site=True, npm_lock=True)),
    "website → site": (["website/docs/intro.md"], _lanes(site=True)),
    # SKILL.md reads like docs, but the skill-doc tests read skills/, so a
    # skill edit must still run Python.
    "skill md → python + site": (["skills/github/SKILL.md"], _lanes(python=True, site=True)),
    "dockerfile → docker meta": (["Dockerfile"], _lanes(docker_meta=True)),
    # Unknown top-level file keeps Python on rather than risk a silent skip.
    "unknown toplevel → python": (["Makefile"], _lanes(python=True)),
    "mixed docs+python → python": (["README.md", "agent/x.py"], _lanes(python=True, scan=True)),
    "mixed docs+frontend → frontend": (["README.md", "apps/x.tsx"], _lanes(frontend=True)),
    # Supply-chain lanes
    ".pth file → scan": (["evil.pth"], _lanes(python=True, scan=True)),
    "setup.py → scan": (["setup.py"], _lanes(python=True, scan=True)),
    "mcp catalog manifest → mcp_catalog": (
        ["optional-mcps/foo/manifest.yaml"],
        _lanes(python=True, mcp_catalog=True),
    ),
    "mcp_catalog.py → mcp_catalog": (
        ["hermes_cli/mcp_catalog.py"],
        _lanes(python=True, scan=True, mcp_catalog=True),
    ),
    # CI-sensitive files require explicit review label.
    "eslint config → ci_review": (
        ["apps/desktop/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared eslint config → ci_review": (
        ["eslint.config.shared.mjs"],
        _lanes(python=True, ci_review=True),
    ),
    "ui-tui eslint config → ci_review": (
        ["ui-tui/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "web eslint config → ci_review": (
        ["web/eslint.config.js"],
        _lanes(frontend=True, ci_review=True),
    ),
    "shared package eslint config → ci_review": (
        ["apps/shared/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "bootstrap-installer eslint config → ci_review": (
        ["apps/bootstrap-installer/eslint.config.mjs"],
        _lanes(frontend=True, ci_review=True),
    ),
    "prettier config → ci_review": (
        [".prettierrc"],
        _lanes(python=True, ci_review=True),
    ),
    "workflow yml → ci_review (also fail-open all)": (
        [".github/workflows/typecheck.yml"],
        DEFAULT,
    ),
    "composite action → ci_review (also fail-open all)": (
        [".github/actions/retry/action.yml"],
        DEFAULT,
    ),
    # Normal desktop source doesn't trigger ci_review.
    "desktop src → no ci_review": (
        ["apps/desktop/src/app.tsx"],
        _lanes(frontend=True),
    ),
    # Fail open: CI-config / empty / blank diffs run everything.
    ".github change → all": ([".github/workflows/tests.yml"], DEFAULT),
    "action change → all": ([".github/actions/detect-changes/action.yml"], DEFAULT),
    "empty diff → all": ([], DEFAULT),
    "blank lines → all": (["", "  "], DEFAULT),
}


@pytest.mark.parametrize("files,expected", CASES.values(), ids=CASES.keys())
def test_classify(files, expected):
    assert classify(files) == expected


# ---------------------------------------------------------------------------
# BUILD-871: the gates are only as good as their ability to read GitHub.
#
# An undefined secret resolves to the empty STRING, which Actions counts as a
# provided value — so `${{ secrets.SOME_UPSTREAM_PAT }}` overrides an input's
# `default:` and hands gh no credential at all. That broke the change
# classifier (every lane failed open, so the CI-sensitive label gate demanded
# its label on every PR) and it silently breaks any mandatory label read the
# same way. These guards parse the real YAML nodes rather than the file text,
# so a fix surviving only in a comment does not satisfy them.
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_UPSTREAM_ONLY_SECRET = "AUTOFIX_BOT_PAT"


def _yaml(rel: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((_REPO / rel).read_text())


def _guarded(expr: object) -> bool:
    """True when an expression cannot evaluate to the empty string."""
    text = str(expr)
    return _UPSTREAM_ONLY_SECRET not in text or "||" in text


def test_the_classifier_can_authenticate():
    """The compare call must never run with an empty credential: an empty file
    list makes classify() fail open with every lane true, which turns the
    CI-sensitive review gate into a label every PR has to carry."""
    action = _yaml(".github/actions/detect-changes/action.yml")
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "classify")
    assert step["env"]["GH_TOKEN"] == "${{ inputs.github-token || github.token }}"


def test_no_mandatory_gate_reads_labels_with_an_unguarded_secret():
    """Every label read that BLOCKS a merge needs a fallback.

    Without one the step exits before the label is checked, so a correctly
    labelled PR fails the gate — the failure mode is unsatisfiable rather than
    merely noisy, and it appears the moment the classifier starts working.
    """
    for workflow, job in (
        (".github/workflows/lint.yml", "ci-review"),
        (".github/workflows/supply-chain-audit.yml", "mcp-catalog-review"),
        (".github/workflows/ci.yml", "detect"),
    ):
        for step in _yaml(workflow)["jobs"][job]["steps"]:
            for key, value in (step.get("env") or {}).items():
                assert _guarded(value), f"{workflow}:{job}: unguarded {key}"
            for key, value in (step.get("with") or {}).items():
                assert _guarded(value), f"{workflow}:{job}: unguarded input {key}"


def test_the_classifier_sees_both_sides_of_a_rename():
    """A rename reports the NEW path in `filename` and the OLD one in
    `previous_filename`. Reading only the former loses the sensitive path when
    a workflow or an eslint config is renamed away from a gated name."""
    action = (_REPO / ".github/actions/detect-changes/action.yml").read_text()
    assert ".previous_filename" in action


def test_a_truncated_compare_is_treated_as_unknown():
    """The compare endpoint caps `files` at 300 whatever the pagination, and a
    truncated list can omit the one sensitive path a gate exists to catch."""
    action = (_REPO / ".github/actions/detect-changes/action.yml").read_text()
    assert "-ge 300" in action and "failing open" in action
