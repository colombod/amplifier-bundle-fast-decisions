"""Optional bridge to trycua's Computer.interface, without a mandatory dependency.

The application supplies a scoped observer and its native approval/verification
callbacks. Coordinates come from that observer, never from Jev. This bridge does
not discover or transmit the user's desktop on its own.
"""

from __future__ import annotations

import asyncio
import copy

from .jev_cua import digest, snapshot


class TryCuaHost:
    """Click/wait adapter for Computer.interface.left_click(x=, y=).

    observe_controls() returns (snapshot, {control_id: (x, y)}). It must provide
    only visible, unobscured controls in the authorized surface and increment
    revision whenever layout or native target identity changes. Its coordinates
    are local to this host and never go into the model request. approve and verify
    are async host callbacks. Native computer approval remains mandatory.
    """

    def __init__(self, interface, *, observe_controls, approve, verify):
        self.interface = interface
        self.observe_controls = observe_controls
        self.approve = approve
        self.verify = verify
        self._points = {}

    async def observe(self):
        observed, points = await self.observe_controls()
        observed = snapshot(observed)
        allowed = {e["id"] for e in observed["elements"] if not e.get("sensitive")}
        if not isinstance(points, dict) or set(points) - allowed:
            raise ValueError("Native target map must match observed controls")
        for el in observed["elements"]:
            if el["operations"] != ["CLICK"]:
                raise ValueError("TryCuaHost currently supports click controls only")
            point = points.get(el["id"])
            if not el.get("sensitive") and (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(type(v) is not int or not -100000 <= v <= 100000 for v in point)
            ):
                raise ValueError("Expected host-observed integer coordinates")
        # Bind local coordinates to the revision too, so a moving target makes an
        # old proposal stale even when an observer forgets to bump its revision.
        observed["revision"] = digest(
            {"revision": observed["revision"], "points": points}
        )
        self._points = copy.deepcopy(points)
        return observed

    async def execute(self, action, observation):
        current = await self.observe()
        if digest(current) != digest(observation):
            return False
        if action == {"operation": "WAIT"}:
            await asyncio.sleep(0.25)
            return True
        if set(action) != {"operation", "target"} or action["operation"] != "CLICK":
            return False
        point = self._points.get(action["target"])
        if point is None:
            return False
        # Upstream returns None on success and raises on command failure.
        await self.interface.left_click(x=point[0], y=point[1])
        return True
