"""CronScheduler provider interface (Axis B — the trigger).

⚠️ EXPERIMENTAL — this interface is validated by exactly ONE consumer (the
built-in) until an external provider (Chronos, Phase 4) shakes it out. Until
then the module path, method signatures, and start() kwargs MAY change without
a deprecation cycle. Once a second provider validates the shape it becomes
stable. Any growth MUST be additive (new optional method with a default), never
a changed signature on start() or a new abstractmethod.

A CronScheduler decides *when* a due job fires. It does NOT decide what firing
means: execution + delivery stay in cron.scheduler.run_job / _deliver_result,
shared by all providers. Providers must never reimplement agent construction or
delivery.

The built-in InProcessCronScheduler runs the historical 60s daemon-thread
ticker. Alternative providers (e.g. Chronos, a NAS-mediated managed-cron
provider for scale-to-zero deployments) live under plugins/cron_providers/<name>/ and are
selected via the `cron.provider` config key (empty = built-in).
"""
from __future__ import annotations

import threading
import logging
from abc import ABC, abstractmethod
from typing import Any


def cron_scheduler_enabled() -> bool:
    """Return whether this profile is allowed to fire cron jobs.

    The default remains enabled for backward compatibility. Multi-profile
    installations can designate one scheduler authority by disabling this key
    everywhere else, without deleting the profiles' historical job stores.
    """
    try:
        from hermes_cli.config import get_config_path, load_config
        from utils import fast_safe_load

        config_path = get_config_path()
        if config_path.exists():
            try:
                with config_path.open(encoding="utf-8") as handle:
                    raw_config = fast_safe_load(handle) or {}
                if not isinstance(raw_config, dict):
                    raise ValueError("root must be a mapping")
            except Exception as exc:
                logging.getLogger("cron.scheduler_provider").error(
                    "Cron scheduler disabled: cannot parse profile config %s: %s",
                    config_path,
                    exc,
                )
                return False
        config = load_config() or {}
        cron_config = config.get("cron", {}) if isinstance(config, dict) else {}
        value = cron_config.get("enabled", True) if isinstance(cron_config, dict) else True
    except Exception as exc:
        logging.getLogger("cron.scheduler_provider").error(
            "Cron scheduler disabled: authority config could not be loaded: %s", exc
        )
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    logging.getLogger("cron.scheduler_provider").error(
        "Cron scheduler disabled: invalid cron.enabled value %r", value
    )
    return False


class CronScheduler(ABC):
    """Axis-B trigger provider. Decides WHEN a due cron job fires.

    Required surface is intentionally minimal: ``name`` + ``start``. ``stop``
    and ``is_available`` carry safe defaults. The three Phase-4 hooks
    (``on_jobs_changed`` / ``fire_due`` / ``reconcile``) are added later as
    NON-abstract methods so the built-in keeps satisfying the ABC without
    overriding them — see ``test_abc_growth_stays_additive``.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier, e.g. 'builtin', 'chronos'."""

    def is_available(self) -> bool:
        """Whether this provider can run in the current environment.

        MUST NOT make network calls. The built-in is always available; an
        external provider checks for configured endpoint/credentials. When a
        named provider returns False, the resolver falls back to the built-in.
        """
        return True

    @abstractmethod
    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
    ) -> None:
        """Begin firing due jobs.

        For the built-in this BLOCKS in the 60s loop until stop_event is set
        (it is run inside a daemon thread by the caller, exactly as today).
        An external provider may register a schedule/webhook and return
        immediately; in that case it must still honor stop_event for teardown.
        """

    def stop(self) -> None:
        """Optional eager teardown hook. Default no-op; setting the stop_event
        is the primary stop signal. Override for providers holding external
        resources (queue consumers, HTTP servers)."""
        return None

    # --- Optional hooks for external providers (added Phase 4). --------------
    # All default-safe so the built-in inherits working behavior without
    # overriding. Keep these NON-abstract — see test_abc_growth_stays_additive.

    def on_jobs_changed(self) -> None:
        """Called after a successful store mutation (create/update/remove/
        pause/resume). External providers reconcile their registry here (e.g.
        Chronos re-provisions/cancels the affected one-shot via NAS).
        Built-in: no-op (it re-reads jobs.json on every tick)."""
        return None

    def fire_due(self, job_id: str, *, adapters: Any = None, loop: Any = None) -> bool:
        """Run a single job NOW via the shared orchestrator. Called by the
        inbound fire webhook when an external scheduler signals a job is due.

        The default claims the job with a store-level compare-and-set
        (multi-machine at-most-once), then runs it via the shared
        ``run_one_job`` body. Built-in never calls this (it has its own tick
        loop); an external provider routes its inbound fire here.

        Returns True if THIS caller claimed and ran the job, False if the claim
        was lost (another machine/retry won it) or the job no longer exists.
        """
        if not cron_scheduler_enabled():
            return False

        from cron.jobs import claim_job_for_fire, get_job
        from cron.scheduler import run_one_job

        if not claim_job_for_fire(job_id):
            return False  # another machine already claimed this fire
        job = get_job(job_id)
        if job is None:
            return False  # job removed (e.g. repeat-N exhausted) between arm and fire
        return run_one_job(job, adapters=adapters, loop=loop)

    def reconcile(self) -> None:
        """Converge the external registry toward jobs.json (the desired state):
        arm missing one-shots, cancel orphaned ones, re-arm changed times.
        Built-in: no-op."""
        return None


class DisabledCronScheduler(CronScheduler):
    """No-op authority boundary for profiles that do not own cron firing."""

    @property
    def name(self) -> str:
        return "disabled"

    def start(
        self,
        stop_event: threading.Event,
        *,
        adapters: Any = None,
        loop: Any = None,
        interval: int = 60,
    ) -> None:
        return None

    def fire_due(self, job_id: str, *, adapters: Any = None, loop: Any = None) -> bool:
        return False


def resolve_cron_scheduler() -> "CronScheduler":
    """Return the active cron scheduler provider.

    Reads ``cron.provider`` from config. Empty/absent → built-in. A named
    provider that is missing, fails to load, or reports ``is_available() ==
    False`` falls back to the built-in with a warning — cron must never be left
    without a trigger.
    """
    import logging

    logger = logging.getLogger("cron.scheduler_provider")

    if not cron_scheduler_enabled():
        return DisabledCronScheduler()

    name = ""
    try:
        from hermes_cli.config import cfg_get, load_config
        name = (cfg_get(load_config(), "cron", "provider", default="") or "").strip()
    except Exception:
        pass

    if not name or name in ("builtin", "in-process", "inprocess"):
        return InProcessCronScheduler()

    try:
        from plugins.cron_providers import load_cron_scheduler
        provider = load_cron_scheduler(name)
        if provider is None:
            logger.warning("cron.provider '%s' not found; using built-in ticker", name)
            return InProcessCronScheduler()
        if not provider.is_available():
            logger.warning("cron.provider '%s' not available; using built-in ticker", name)
            return InProcessCronScheduler()
        logger.info("Using cron scheduler provider: %s", provider.name)
        return provider
    except Exception as e:
        logger.warning(
            "Failed to load cron.provider '%s' (%s); using built-in ticker", name, e
        )
        return InProcessCronScheduler()


class InProcessCronScheduler(CronScheduler):
    """Default provider: the historical in-process 60s ticker.

    ``start()`` blocks in the tick loop until ``stop_event`` is set, identical
    to the pre-refactor ``_start_cron_ticker`` core loop. The caller runs it in
    a daemon thread.
    """

    @property
    def name(self) -> str:
        return "builtin"

    def start(self, stop_event, *, adapters=None, loop=None, interval=60):
        import logging
        from cron.scheduler import tick as cron_tick
        from cron.jobs import record_ticker_heartbeat

        logger = logging.getLogger("cron.scheduler_provider")
        if not cron_scheduler_enabled():
            logger.info("Cron scheduler disabled for this profile")
            return
        logger.info("In-process cron scheduler started (interval=%ds)", interval)
        # Heartbeat once before the first sleep so `hermes cron status` sees a
        # live ticker immediately after startup, not only after the first tick.
        record_ticker_heartbeat()
        while not stop_event.is_set():
            ok = False
            try:
                cron_tick(verbose=False, adapters=adapters, loop=loop, sync=False)
                ok = True
            except BaseException as e:
                # Catch BaseException (not just Exception) so a SystemExit from
                # a misbehaving provider SDK / agent retry path does not kill
                # the ticker thread silently (#32612). KeyboardInterrupt is
                # intentionally caught here too — gateway shutdown is driven by
                # stop_event (set by the main thread's signal handler), not by
                # an exception in this daemon thread, so swallowing it and
                # re-checking stop_event keeps shutdown clean.
                logger.error("Cron tick error: %s", e, exc_info=True)
            # Record liveness every iteration; bump the success marker only on a
            # clean tick, so status can tell "alive but failing every tick" from
            # "actually firing jobs" (#32612, #32895).
            record_ticker_heartbeat(success=ok)
            stop_event.wait(interval)
