"""The credential audit enumerates profile-local .env NAMES, never values.

BUILD-789: four profiles' ``.env`` was a symlink to the machine-global secrets
file, so every worker spawned for them held the vault access token. No audit
surface showed it — the boundary report only covered what a worker inherits.
"""

from hermes_cli.worker_credentials import render_profile_local_env_audit

SECRET = "sk-do-not-print-me"


def _profile(root, name, body, *, symlink_to=None):
    d = root / "profiles" / name
    d.mkdir(parents=True)
    if symlink_to is not None:
        (d / ".env").symlink_to(symlink_to)
    else:
        (d / ".env").write_text(body)
    return d


def test_lists_names_and_never_values(tmp_path):
    _profile(tmp_path, "coder", f"MNEMOSYNE_DATA_DIR=/x\nANTHROPIC_TOKEN={SECRET}\n")
    out = "\n".join(render_profile_local_env_audit(root=tmp_path))
    assert "ANTHROPIC_TOKEN" in out
    assert SECRET not in out
    # Not a credential name — noise, and printing it invites printing values.
    assert "MNEMOSYNE_DATA_DIR" not in out


def test_symlinked_env_and_vault_token_are_called_out(tmp_path):
    shared = tmp_path / "secrets" / "hermes.env"
    shared.parent.mkdir(parents=True)
    shared.write_text(f"BWS_ACCESS_TOKEN={SECRET}\nGH_TOKEN={SECRET}\n")
    _profile(tmp_path, "vault-v2-curator", "", symlink_to=shared)
    out = "\n".join(render_profile_local_env_audit(root=tmp_path))
    assert "symlink ->" in out
    assert "HOLDS A VAULT ACCESS TOKEN" in out
    assert "BWS_ACCESS_TOKEN" in out and "GH_TOKEN" in out
    assert SECRET not in out


def test_a_clean_profile_reports_zero(tmp_path):
    _profile(tmp_path, "verifier", "TERMINAL_ENV=local\n")
    out = "\n".join(render_profile_local_env_audit(root=tmp_path))
    assert "verifier" in out
    assert "HOLDS A VAULT ACCESS TOKEN" not in out
