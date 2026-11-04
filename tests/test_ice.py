"""Canary on the patched aioice internals (AS4): if an upgrade changes them, this fails before a session does."""

import inspect

import aioice
import aioice.ice as ice
import aiortc
import pytest
from aioice.candidate import Candidate

from iassistpro import ice as iice
from iassistpro.ice import patch_aioice


def test_pinned_versions():
    assert (aioice.__version__, aiortc.__version__) == ("0.12.1", "1.15.0")
    assert iice.EXPECTED_VERSIONS == {"aioice": "0.12.1", "aiortc": "1.15.0"}


def test_patched_internals_still_exist():
    original = iice.ORIGINAL_GET_COMPONENT_CANDIDATES
    assert list(inspect.signature(original).parameters) == ["self", "component", "addresses", "timeout"]
    assert hasattr(ice, "StunProtocol") and "_protocols" in vars(ice.Connection(ice_controlling=True))
    assert {"foundation", "component", "transport", "priority", "host", "port", "type"} <= set(inspect.signature(Candidate).parameters)


async def test_patch_binds_inside_range_and_advertises_host():
    patch_aioice(40100, 40101, "127.0.0.1")
    assert iice.PATCHED == (40100, 40101, "127.0.0.1")
    a, b, c = (ice.Connection(ice_controlling=True) for _ in range(3))
    await a.gather_candidates()
    await b.gather_candidates()
    assert {(x.host, x.port) for x in a.local_candidates + b.local_candidates} == {("127.0.0.1", 40100), ("127.0.0.1", 40101)}
    with pytest.raises(OSError, match="no free UDP port"):
        await c.gather_candidates()
    await a.close()
    await b.close()


def test_patch_is_idempotent():
    patch_aioice(40100, 40101, "127.0.0.1")
    first = ice.Connection.get_component_candidates
    patch_aioice(40100, 40101, "127.0.0.1")
    assert ice.Connection.get_component_candidates is first
