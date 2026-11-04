"""aioice patch (AS4): bind WebRTC host candidates inside a fixed UDP range that Docker publishes 1:1, and
advertise ICE_HOST (127.0.0.1 under Docker Desktop) instead of the container's private address.

It replaces aioice's private Connection.get_component_candidates; aioice/aiortc are pinned exactly and
tests/test_ice.py is the canary on these internals.
"""

import asyncio

import aioice.ice as _ice
from aioice.candidate import Candidate, candidate_foundation, candidate_priority

from .logs import log_event

EXPECTED_VERSIONS = {"aioice": "0.12.1", "aiortc": "1.15.0"}
ORIGINAL_GET_COMPONENT_CANDIDATES = _ice.Connection.get_component_candidates
PATCHED: tuple[int, int, str] | None = None


def patch_aioice(lo: int, hi: int, host: str) -> None:
    global PATCHED
    if PATCHED == (lo, hi, host):
        return

    async def get_component_candidates(self, component, addresses, timeout=5):
        loop = asyncio.get_running_loop()
        for port in range(lo, hi + 1):
            try:
                _, protocol = await loop.create_datagram_endpoint(
                    lambda: _ice.StunProtocol(self), local_addr=("0.0.0.0", port)
                )
                break
            except OSError:
                continue
        else:
            log_event("ice_no_free_port", lo=lo, hi=hi)
            raise OSError(f"no free UDP port in {lo}-{hi}")
        protocol.local_candidate = Candidate(
            foundation=candidate_foundation("host", "udp", host),
            component=component,
            transport="udp",
            priority=candidate_priority(component, "host"),
            host=host,
            port=port,
            type="host",
        )
        self._protocols.append(protocol)
        log_event("ice_host_candidate", port=port)
        return [protocol.local_candidate]

    _ice.Connection.get_component_candidates = get_component_candidates
    PATCHED = (lo, hi, host)
