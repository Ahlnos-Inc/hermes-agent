"""Tests for scripts/ci/classify_changes.py.

Check some common patterns of file modifications and the CI lanes they should run.
We should always fail open. We may run a lane we didn't need, never skip one a
change could have broken.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "classify_changes.py"
_spec = importlib.util.spec_from_file_location("classify_changes", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load classify_changes.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
classify = _mod.classify

# A KNOWN diff that touches .github/: everything runs except the MCP catalog
# review, which is deliberately skipped — we can see its files were untouched.
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

# An UNKNOWN diff (push/dispatch, or the compare call failed or was truncated).
# Here the MCP review runs too: nobody can see whether its files were touched,
# and skipping a review gate on an unreadable diff is how a gate silently stops
# gating (BUILD-871).
UNKNOWN = {**DEFAULT, "mcp_catalog": True}


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
    "empty diff → all, including the MCP gate": ([], UNKNOWN),
    "blank lines → all, including the MCP gate": (["", "  "], UNKNOWN),
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
# its label on every PR) and it breaks any mandatory label read the same way.
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_ACTION = _REPO / ".github/actions/detect-changes/action.yml"
_UPSTREAM_ONLY_SECRET = "AUTOFIX_BOT_PAT"
# Sources that always resolve to a usable value. Anything else — an
# upstream-only PAT, any secret this repo may not define — can be the empty
# string, and an empty credential is what BUILD-871 was.
_ALWAYS_PRESENT = ("github.token", "secrets.GITHUB_TOKEN")
_EXPRESSION = re.compile(r"^\$\{\{(?P<body>[^{}]+)\}\}$")


def _assert_cannot_resolve_empty(value: str, where: str) -> None:
    """The value must be ONE Actions expression whose last fallback is a source
    that always exists.

    Checking merely for the presence of `||` is not enough: two separate
    expressions joined by a literal `||`
    (`${{ secrets.A }} || ${{ secrets.B }}`) reads as a fallback but evaluates
    to the literal text, and a chain ending in another optional secret is still
    emptyable.
    """
    match = _EXPRESSION.match(str(value).strip())
    assert match, f"{where}: not a single Actions expression: {value!r}"
    operands = [o.strip() for o in match.group("body").split("||")]
    assert operands[-1] in _ALWAYS_PRESENT, (
        f"{where}: last fallback {operands[-1]!r} can resolve to the empty "
        f"string; expected one of {_ALWAYS_PRESENT}"
    )

# Every job whose failure BLOCKS a merge and that reads state from the API.
_MANDATORY_GATES = (
    (".github/workflows/ci.yml", "detect"),
    (".github/workflows/lint.yml", "ci-review"),
    (".github/workflows/supply-chain-audit.yml", "mcp-catalog-review"),
)


def _yaml(path):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(Path(path).read_text())


def _classify_step():
    action = _yaml(_ACTION)
    return next(s for s in action["runs"]["steps"] if s.get("id") == "classify")


def _compare_jq() -> str:
    """The jq expression the action actually runs against the compare payload."""
    script = _classify_step()["run"]
    line = next(ln for ln in script.splitlines() if "--jq '.files[]" in ln)
    return line.split("--jq '", 1)[1].rsplit("'", 1)[0]


def test_the_classifier_can_authenticate():
    """The compare call must never run with an empty credential: an empty file
    list makes classify() fail open, and this is where that started."""
    assert _classify_step()["env"]["GH_TOKEN"] == "${{ inputs.github-token || github.token }}"


@pytest.mark.parametrize("workflow,job", _MANDATORY_GATES)
def test_a_mandatory_gate_never_reads_with_an_empty_credential(workflow, job):
    """A blocking gate that cannot authenticate exits before it checks
    anything, so a correctly labelled PR fails it. Every credential it passes
    must be present AND unable to evaluate to the empty string."""
    steps = _yaml(_REPO / workflow)["jobs"][job]["steps"]
    reads = [s for s in steps if "gh " in str(s.get("with", {}).get("command", ""))
             or "gh " in str(s.get("run", ""))
             or s.get("uses", "").endswith("detect-changes")]
    assert reads, f"{workflow}:{job}: no API-reading step found — did the job change shape?"
    for step in reads:
        supplied = {**(step.get("env") or {}), **(step.get("with") or {})}
        creds = {k: v for k, v in supplied.items()
                 if "TOKEN" in k.upper() or k == "github-token"}
        assert creds, f"{workflow}:{job}: an API read with no credential at all"
        for key, value in creds.items():
            _assert_cannot_resolve_empty(value, f"{workflow}:{job}: {key}")


def _run_compare_jq(payload: dict) -> list[str]:
    """Run the action's real jq expression over a compare payload."""
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not installed")
    out = subprocess.run(
        [jq, "-r", _compare_jq()], input=json.dumps(payload),
        capture_output=True, text=True, check=True,
    ).stdout
    # The action splits the tab-separated row into one path per line and drops
    # the blanks; mirror that here so the test covers the same pipeline.
    return [p for p in out.replace("\t", "\n").splitlines() if p]


def test_the_compare_expression_reports_both_sides_of_a_rename():
    """A rename reports the NEW path in `filename` and the OLD one in
    `previous_filename`. Taking only the former loses the gated name when an
    eslint config or a workflow is renamed away from it — and classify() would
    then report ci_review=false for a PR that really did touch one."""
    paths = _run_compare_jq({"files": [
        {"filename": "docs/notes.md", "previous_filename": "eslint.config.mjs"},
        {"filename": "README.md"},
    ]})
    assert "eslint.config.mjs" in paths and "docs/notes.md" in paths
    assert classify(paths)["ci_review"] is True


def test_the_compare_expression_emits_one_row_per_file():
    """The row count is the FILE count, which is what the truncation check
    counts — so a file without a previous_filename must not emit a blank."""
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq not installed")
    rows = subprocess.run(
        [jq, "-r", _compare_jq()], input=json.dumps({"files": [{"filename": "a.py"}] * 3}),
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert len(rows) == 3


def test_a_truncated_compare_is_treated_as_an_unreadable_diff():
    """The endpoint caps `files` at 300 whatever the pagination, and a
    truncated list can omit the one sensitive path a gate exists to catch."""
    script = _classify_step()["run"]
    assert '[ "${FILE_COUNT:-0}" -ge 300 ]' in script, "the cap check must survive"
    assert script.count("gh api") == 1, "the cap must not cost a second API call"
    # An unreadable diff runs every gate, the MCP catalog review included.
    assert classify([])["mcp_catalog"] is True


@pytest.mark.parametrize("value", [
    "${{ secrets.AUTOFIX_BOT_PAT }}",                      # the original bug
    "${{ secrets.AUTOFIX_BOT_PAT }} || ${{ github.token }}",  # literal text, not a fallback
    "${{ secrets.AUTOFIX_BOT_PAT || secrets.SOME_OTHER_PAT }}",  # ends in another optional secret
    "",
    "${{ inputs.github-token }}",
])
def test_the_credential_guard_rejects_emptyable_expressions(value):
    """The guard has to fail on the shapes that look right and are not."""
    with pytest.raises(AssertionError):
        _assert_cannot_resolve_empty(value, "fixture")


@pytest.mark.parametrize("value", [
    "${{ inputs.github-token || github.token }}",
    "${{ secrets.AUTOFIX_BOT_PAT || secrets.GITHUB_TOKEN }}",
    "${{ github.token }}",
])
def test_the_credential_guard_accepts_a_real_fallback(value):
    _assert_cannot_resolve_empty(value, "fixture")
