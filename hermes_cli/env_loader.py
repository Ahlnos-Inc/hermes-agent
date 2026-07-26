"""Helpers for loading Hermes .env files consistently across entrypoints."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from utils import atomic_replace, env_var_enabled, fast_safe_load


# Env var name suffixes that indicate credential values.  These are the
# only env vars whose values we sanitize on load — we must not silently
# alter arbitrary user env vars, but credentials are known to require
# pure ASCII (they become HTTP header values).
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")

# Names we've already warned about during this process, so repeated
# load_hermes_dotenv() calls (user env + project env, gateway hot-reload,
# tests) don't spam the same warning multiple times.
_WARNED_KEYS: set[str] = set()

# Map of env-var name → source label ("bitwarden", etc.) for credentials
# that were injected by an external secret source during load_hermes_dotenv().
# Used by setup / `hermes model` flows to label detected credentials so
# users understand WHERE a key came from when their .env doesn't contain it
# directly (otherwise the "credentials detected ✓" line looks identical to
# the .env case and they don't know Bitwarden is wired up).
_SECRET_SOURCES: dict[str, str] = {}

# HERMES_HOME paths we've already pulled external secrets for during this
# process.  ``load_hermes_dotenv()`` is called at module-import time from
# several hot modules (cli.py, hermes_cli/main.py, run_agent.py,
# trajectory_compressor.py, gateway/run.py, ...), so without this guard the
# Bitwarden status line gets printed 3-5x per startup.  Bitwarden's own
# in-process cache prevents redundant network calls, but the print, the
# config re-parse, and the ASCII sanitization sweep still ran every time.
_APPLIED_HOMES: set[str] = set()


def _stderr_note(message: str) -> None:
    """Best-effort diagnostic to stderr.

    Daemon/cron processes can end up with an orphaned stderr pipe (reader
    gone); a plain print() then raises BrokenPipeError and aborts whatever
    triggered the dotenv load (2026-07-07 midnight cron failures). Env
    loading must never fail over a diagnostic message.
    """
    try:
        print(message, file=sys.stderr)
    except OSError:
        pass


def get_secret_source(env_var: str) -> str | None:
    """Return the label of the secret source that supplied ``env_var``, if any.

    Returns ``"bitwarden"`` for keys pulled from Bitwarden Secrets Manager
    during the current process's ``load_hermes_dotenv()`` call.  Returns
    ``None`` for keys that came from ``.env``, the shell environment, or
    aren't tracked.  The returned label is metadata only: credential-pool
    persistence may store it to explain the origin of a borrowed secret, but
    must never treat it as authorization to persist the raw value.
    """
    return _SECRET_SOURCES.get(env_var)


def externally_sourced_env_names() -> frozenset[str]:
    """Return every env var this process pulled from an external secret vault.

    The provenance map is already maintained for display purposes; BUILD-681
    reads it as a *control*: it is the only place in the process that knows
    which of the several hundred names in ``os.environ`` arrived from
    Bitwarden (or another configured backend) rather than from the shell,
    ``.env``, or the OS. A dispatcher worker inherits the gateway's
    environment, so this set is exactly what must not cross that boundary
    unless a manifest capability grants it.

    Empty when this process never loaded a vault — in which case the values
    are not in ``os.environ`` either, so there is nothing to strip. Profile
    ``.env`` and ``auth.json`` are deliberately NOT included: they are a
    separate, profile-owned control plane.
    """
    return frozenset(_SECRET_SOURCES)


def reset_secret_source_cache() -> None:
    """Forget which HERMES_HOME paths have already had external secrets applied.

    The first call to ``_apply_external_secret_sources(home_path)`` in a
    process pulls from Bitwarden (or other configured backend), records the
    applied keys in ``_SECRET_SOURCES``, and remembers ``home_path`` so
    subsequent calls in the same process are no-ops.  Call this to force the
    next call to re-pull — useful for tests, and for long-running processes
    that want to refresh after a config change.
    """
    _APPLIED_HOMES.clear()
    _SECRET_SKIPS.clear()


def format_secret_source_suffix(env_var: str) -> str:
    """Return a human-readable suffix like ``" (from Bitwarden)"`` or ``""``.

    Use this when printing a detected credential so the user can see where
    it came from.  Empty string when the credential came from ``.env`` or
    the shell — those are the implicit / "default" cases users already
    understand.
    """
    source = get_secret_source(env_var)
    if not source:
        return ""
    if source == "bitwarden":
        return " (from Bitwarden)"
    # Ask the registry for the source's human label (e.g. "1Password").
    # Fall back to the raw source name for labels the registry doesn't
    # know (stale provenance from an uninstalled plugin, tests).
    try:
        from agent.secret_sources.registry import get_source

        registered = get_source(source)
        if registered is not None and registered.label:
            return f" (from {registered.label})"
    except Exception:  # noqa: BLE001 — label lookup must never raise
        pass
    return f" (from {source})"


def _format_offending_chars(value: str, limit: int = 3) -> str:
    """Return a compact 'U+XXXX ('c'), ...' summary of non-ASCII codepoints."""
    seen: list[str] = []
    for ch in value:
        if ord(ch) > 127:
            label = f"U+{ord(ch):04X}"
            if ch.isprintable():
                label += f" ({ch!r})"
            if label not in seen:
                seen.append(label)
            if len(seen) >= limit:
                break
    return ", ".join(seen)


def _sanitize_loaded_credentials() -> None:
    """Strip non-ASCII characters from credential env vars in os.environ.

    Called after dotenv loads so the rest of the codebase never sees
    non-ASCII API keys.  Only touches env vars whose names end with
    known credential suffixes (``_API_KEY``, ``_TOKEN``, etc.).

    Emits a one-line warning to stderr when characters are stripped.
    Silent stripping would mask copy-paste corruption (Unicode lookalike
    glyphs from PDFs / rich-text editors, ZWSP from web pages) as opaque
    provider-side "invalid API key" errors (see #6843).
    """
    for key, value in list(os.environ.items()):
        if not any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            continue
        try:
            value.encode("ascii")
            continue
        except UnicodeEncodeError:
            pass
        cleaned = value.encode("ascii", errors="ignore").decode("ascii")
        os.environ[key] = cleaned
        if key in _WARNED_KEYS:
            continue
        _WARNED_KEYS.add(key)
        stripped = len(value) - len(cleaned)
        detail = _format_offending_chars(value) or "non-printable"
        _stderr_note(
            f"  Warning: {key} contained {stripped} non-ASCII character"
            f"{'s' if stripped != 1 else ''} ({detail}) — stripped so the "
            f"key can be sent as an HTTP header."
        )
        _stderr_note(
            "  This usually means the key was copy-pasted from a PDF, "
            "rich-text editor, or web page that substituted lookalike\n"
            "  Unicode glyphs for ASCII letters. If authentication fails "
            "(e.g. \"API key not valid\"), re-copy the key from the\n"
            "  provider's dashboard and run `hermes setup` (or edit the "
            ".env file in a plain-text editor)."
        )


def _load_dotenv_with_fallback(path: Path, *, override: bool) -> None:
    try:
        load_dotenv(dotenv_path=path, override=override, encoding="utf-8")
    except UnicodeDecodeError:
        load_dotenv(dotenv_path=path, override=override, encoding="latin-1")
    # Strip non-ASCII characters from credential env vars that were just
    # loaded.  API keys must be pure ASCII since they're sent as HTTP
    # header values (httpx encodes headers as ASCII).  Non-ASCII chars
    # typically come from copy-pasting keys from PDFs or rich-text editors
    # that substitute Unicode lookalike glyphs (e.g. ʋ U+028B for v).
    _sanitize_loaded_credentials()


def _sanitize_env_file_if_needed(path: Path) -> None:
    """Pre-sanitize a .env file before python-dotenv reads it.

    python-dotenv does not handle corrupted lines where multiple
    KEY=VALUE pairs are concatenated on a single line (missing newline).
    This produces mangled values — e.g. a bot token duplicated 8×
    (see #8908).

    Also strips embedded null bytes which crash ``os.environ[k] = v``
    with ``ValueError: embedded null byte`` — typically introduced by
    copy-pasting API keys from terminals or rich-text editors.

    We delegate to ``hermes_cli.config._sanitize_env_lines`` which
    already knows all valid Hermes env-var names and can split
    concatenated lines correctly.
    """
    if not path.exists():
        return
    try:
        from hermes_cli.config import _sanitize_env_lines
    except ImportError:
        return  # early bootstrap — config module not available yet

    read_kw = {"encoding": "utf-8-sig", "errors": "replace"}
    try:
        with open(path, **read_kw) as f:
            original = f.readlines()
        # Strip null bytes before _sanitize_env_lines so they never
        # reach python-dotenv (which passes them to os.environ and
        # crashes with ValueError).
        stripped = [line.replace("\x00", "") for line in original]
        sanitized = _sanitize_env_lines(stripped)
        if sanitized != original:
            import tempfile
            fd, tmp = tempfile.mkstemp(
                dir=str(path.parent), suffix=".tmp", prefix=".env_"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.writelines(sanitized)
                    f.flush()
                    os.fsync(f.fileno())
                atomic_replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception:
        pass  # best-effort — don't block gateway startup


def load_hermes_dotenv(
    *,
    hermes_home: str | os.PathLike | None = None,
    project_env: str | os.PathLike | None = None,
) -> list[Path]:
    """Load Hermes environment files with user config taking precedence.

    Behavior:
    - `~/.hermes/.env` overrides stale shell-exported values when present.
    - project `.env` acts as a dev fallback and only fills missing values when
      the user env exists.
    - if no user env exists, the project `.env` also overrides stale shell vars.
    """
    loaded: list[Path] = []

    home_path = Path(hermes_home or os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    user_env = home_path / ".env"
    project_env_path = Path(project_env) if project_env else None

    # Fix corrupted .env files before python-dotenv parses them (#8908).
    if user_env.exists():
        _sanitize_env_file_if_needed(user_env)
    if project_env_path and project_env_path.exists():
        _sanitize_env_file_if_needed(project_env_path)

    if user_env.exists():
        _load_dotenv_with_fallback(user_env, override=True)
        loaded.append(user_env)

    # Load .op.env AFTER .env so that .env values win, but the bootstrap
    # token (OP_SERVICE_ACCOUNT_TOKEN) becomes available for
    # apply_onepassword_secrets() even in cron / subprocess environments
    # that inherit no shell state (no systemd EnvironmentFile, no op run).
    # .op.env is gitignored — the service-account token never enters the
    # committed .env file.
    # Users on systemd can alternatively use:
    #   EnvironmentFile=-/path/to/.hermes/.op.env
    # in their gateway unit, which takes precedence (override=False below
    # ensures .op.env never clobbers a token already in the environment).
    op_env = home_path / ".op.env"
    if op_env.exists() and not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
        _load_dotenv_with_fallback(op_env, override=False)

    if project_env_path and project_env_path.exists():
        _load_dotenv_with_fallback(project_env_path, override=not loaded)
        loaded.append(project_env_path)

    _apply_external_secret_sources(home_path)
    _apply_managed_env()

    return loaded


def _apply_managed_env() -> None:
    """Apply the managed-scope .env last, with override, so it beats user/shell.

    Managed scope is machine-global (independent of HERMES_HOME / profile). v1
    enforcement is "applied last with override=True" — at the end of startup load
    ``os.environ`` holds the managed value for every managed key, beating both the
    user ``.env`` and any pre-existing shell export. This deliberately inverts the
    usual env-over-config precedence for the pinned keys (see
    ``docs/design/managed-scope.md`` §4.1).

    This does NOT prevent the agent from later mutating ``os.environ`` in-process
    or ``export``-ing in a subprocess shell; that hard boundary is a documented
    v2 item (design §8.1). v1 relies on filesystem permissions only.

    Fail-open: a missing managed dir or .env is the common case and a no-op; any
    error here is swallowed so managed scope can never block startup.
    """
    try:
        from hermes_cli import managed_scope

        managed_dir = managed_scope.get_managed_dir()
    except Exception:  # noqa: BLE001 — managed scope must never block startup
        return
    if managed_dir is None:
        return
    managed_env = managed_dir / ".env"
    if not managed_env.exists():
        return
    _sanitize_env_file_if_needed(managed_env)
    _load_dotenv_with_fallback(managed_env, override=True)


def _worker_vault_pull_is_ungranted() -> bool:
    """True when this process is a dispatcher worker with no vault grant.

    BUILD-681 stopped a worker INHERITING the controller's vault-sourced
    environment, but a worker whose profile home can reach a vault access token
    just re-pulls the whole project itself at import time — same secrets, a
    different door, and one the credential manifest neither records nor can
    revoke (BUILD-789).

    A dispatcher worker is identified by ``HERMES_KANBAN_TASK``, which the
    dispatcher sets before the process starts. Nothing else is affected: the
    controller, cron agents, and interactive CLI runs have no task id and pull
    exactly as before. ``bws_bootstrap`` is the existing manifest capability
    for a full-process vault grant, so a worker that legitimately needs one
    already has an enumerable, revocable way to say so.

    Fails closed: a worker that cannot name its profile, or whose manifest
    cannot be read, gets nothing.
    """
    if not str(os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    profile = str(os.environ.get("HERMES_PROFILE") or "").strip()
    if not profile:
        return True
    try:
        from hermes_cli.worker_credentials import load_manifest

        return "bws_bootstrap" not in load_manifest().actions_for(profile)
    except Exception:  # noqa: BLE001 — an unreadable manifest grants nothing
        return True


def _apply_external_secret_sources(home_path: Path) -> None:
    """Pull secrets from every enabled external source into env.

    Runs AFTER dotenv loads so .env values are visible (sources use them
    to locate bootstrap tokens) but BEFORE the rest of Hermes reads
    ``os.environ`` for credentials.  Any failure here is logged and
    swallowed — external secret sources must never block startup.

    The heavy lifting (source ordering, mapped-beats-bulk precedence,
    first-claim-wins conflict handling, override semantics, provenance)
    lives in ``agent.secret_sources.registry.apply_all``; this wrapper
    owns the once-per-HERMES_HOME guard, the post-apply ASCII
    sanitization sweep, the ``_SECRET_SOURCES`` provenance map that
    UI surfaces read, and the startup status lines.

    Idempotent within a process: subsequent calls for the same
    ``home_path`` are no-ops.  ``load_hermes_dotenv()`` runs at import
    time from several hot modules (cli.py, hermes_cli/main.py,
    run_agent.py, trajectory_compressor.py, ...), so without this guard
    the status lines would print 3-5x per CLI startup.  Use
    ``reset_secret_source_cache()`` if you need to force a re-pull
    (tests, long-running processes after a config change).
    """
    home_key = str(Path(home_path).resolve())
    if home_key in _APPLIED_HOMES:
        return
    _APPLIED_HOMES.add(home_key)

    if _worker_vault_pull_is_ungranted():
        return

    try:
        cfg = _load_secrets_config(home_path)
    except Exception:  # noqa: BLE001 — config errors must not block startup
        return
    if not cfg:
        return

    try:
        from agent.secret_sources.registry import apply_all
    except ImportError:
        return

    try:
        report = apply_all(cfg, home_path)
    except Exception:  # noqa: BLE001 — belt-and-braces; apply_all shouldn't raise
        return

    if report.applied_any:
        # Re-run the ASCII sanitization pass: vault values are
        # user-supplied and might have the same copy-paste corruption as
        # a manually edited .env (see #6843).
        _sanitize_loaded_credentials()
        # Remember where each var came from so setup / `hermes model`
        # flows can label detected credentials with "(from Bitwarden)" /
        # "(from 1Password)" — otherwise users see "credentials ✓" with
        # no hint the value came from a vault rather than .env.
        for name, applied in report.provenance.items():
            _SECRET_SOURCES[name] = applied.source

    for src in report.sources:
        if src.applied:
            print(
                f"  {src.label}: applied {len(src.applied)} "
                f"secret{'s' if len(src.applied) != 1 else ''} "
                f"({', '.join(sorted(src.applied))})",
                file=sys.stderr,
            )
        for line in _render_skipped(src):
            print(line, file=sys.stderr)
        if src.result.error:
            print(f"  {src.label}: {src.result.error}", file=sys.stderr)
        for warn in src.result.warnings:
            print(f"  {src.label}: {warn}", file=sys.stderr)
    for conflict in report.conflicts:
        print(f"  Secret sources: {conflict}", file=sys.stderr)


# Why each skip bucket is worth a line, in the order they matter to an
# operator.  ``invalid`` is the only one that is always a DEFECT: the secret
# exists in the vault, the startup banner reports a healthy "applied N", and
# the value silently never arrives, because the name is not a legal
# environment variable name (hyphens are the usual cause).  A capability that
# resolves by env-var name then keeps using whatever it used before, with no
# signal anywhere connecting the two — BUILD-793, found the hard way while
# wiring BUILD-603's GitHub tokens.  The other three are ordinary precedence
# outcomes, reported so "my secret isn't here" is answerable without a diff.
_SKIP_BUCKETS = (
    ("skipped_invalid", "NOT APPLIED — name is not a valid environment variable name"),
    ("skipped_protected", "not applied — protected bootstrap-auth name"),
    ("skipped_claimed", "not applied — an earlier source already supplied it"),
    ("skipped_existing", "not applied — an existing .env/shell value won"),
)

# name → reason, for every secret a source declined to apply this process.
# Read by the credential audit so the state is inspectable after startup and
# not only in whatever scrollback the gateway happened to write it to.
_SECRET_SKIPS: dict[str, str] = {}


def skipped_secret_names() -> dict[str, str]:
    """Return ``{secret name: reason}`` for secrets a source did not apply."""
    return dict(_SECRET_SKIPS)


def _render_skipped(src) -> list[str]:
    """Render one line per non-empty skip bucket. Names only, never values."""
    lines = []
    for attr, reason in _SKIP_BUCKETS:
        names = sorted(getattr(src, attr, ()) or ())
        if not names:
            continue
        for name in names:
            _SECRET_SKIPS[name] = reason
        lines.append(
            f"  {src.label}: {len(names)} {reason}: {', '.join(names)}"
        )
    return lines


def _load_secrets_config(home_path: Path) -> dict:
    """Read just the ``secrets:`` section out of config.yaml.

    Imported lazily and isolated from the main config loader so a
    malformed config can't take down dotenv loading entirely.
    """
    config_path = home_path / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = fast_safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data.get("secrets") or {}
