"""Fork merge-survival guard: auxiliary-provider unhealthy-cache TTL.

The fork reduced the unhealthy-provider cache TTL from upstream's 600s to 120s
(commit 90b061ac1) so a briefly-unhealthy aux provider recovers ~5x sooner.
The value lives in an interleaved file (auxiliary_client.py) and no test pins
it — the one TTL-expiry test passes an explicit ttl override, so a silent
revert to 600 would go unnoticed. See [audit: routing/infra cluster, item 5].
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from agent.auxiliary_client import _AUX_UNHEALTHY_TTL_SECONDS


def test_aux_unhealthy_ttl_is_120s():
    assert _AUX_UNHEALTHY_TTL_SECONDS == 120
