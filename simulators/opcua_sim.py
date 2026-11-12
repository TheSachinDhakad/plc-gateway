"""OPC UA simulator (asyncua Server). SECURITY POLICY NONE — development/testing only.

Variables (namespace "urn:industrial-plc-gateway:sim", string NodeIds):
``Sim.Temperature`` Float, ``Sim.Pressure`` Double, ``Sim.Counter`` Int32,
``Sim.Running`` Boolean, ``Sim.Setpoint`` Float (writable), ``Sim.Name`` String.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

from asyncua import Server, ua

NAMESPACE_URI = "urn:industrial-plc-gateway:sim"


class OpcUaSimulator:
    def __init__(self, port: int = 4841, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host
        self.endpoint = f"opc.tcp://{host}:{port}/gateway-sim/"
        self.server = Server()
        self.ns = 0
        self.nodes: dict[str, object] = {}
        self._animator: asyncio.Task[None] | None = None

    async def start(self) -> None:
        logging.getLogger("asyncua").setLevel(logging.WARNING)
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("industrial-plc-gateway simulator")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace(NAMESPACE_URI)
        folder = await self.server.nodes.objects.add_folder(ua.NodeId("Sim", self.ns), "Sim")
        spec = {
            "Temperature": (25.0, ua.VariantType.Float),
            "Pressure": (4.0, ua.VariantType.Double),
            "Counter": (0, ua.VariantType.Int32),
            "Running": (False, ua.VariantType.Boolean),
            "Setpoint": (50.0, ua.VariantType.Float),
            "Name": ("Line 1", ua.VariantType.String),
        }
        for name, (value, vt) in spec.items():
            node = await folder.add_variable(ua.NodeId(f"Sim.{name}", self.ns), name, ua.Variant(value, vt))
            if name == "Setpoint":
                await node.set_writable()
            self.nodes[name] = node
        await self.server.start()

    def node_id(self, name: str) -> str:
        return f"ns={self.ns};s=Sim.{name}"

    async def animate(self, period_s: float = 1.0) -> None:
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            await self.nodes["Temperature"].write_value(ua.Variant(25.0 + 3 * math.sin(t / 20), ua.VariantType.Float))
            await self.nodes["Pressure"].write_value(ua.Variant(4.0 + 0.2 * math.cos(t / 15), ua.VariantType.Double))
            await self.nodes["Counter"].write_value(ua.Variant(int(t), ua.VariantType.Int32))
            await self.nodes["Running"].write_value(ua.Variant(int(t / 20) % 2 == 0, ua.VariantType.Boolean))
            await asyncio.sleep(period_s)

    def start_animation(self) -> None:
        self._animator = asyncio.create_task(self.animate())

    async def stop(self) -> None:
        if self._animator:
            self._animator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._animator
        await self.server.stop()
