"""Run all simulators for local development:

    python -m simulators                 # S7 :1102, Modbus :1502, MC :5000, OPC UA :4841
    python -m simulators --no-s7 --modbus-port 1503
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from simulators.mc_sim import MCSimulator
from simulators.modbus_sim import ModbusSimulator
from simulators.opcua_sim import OpcUaSimulator
from simulators.s7_sim import S7Simulator


async def main(args: argparse.Namespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    started = []
    if not args.no_s7:
        s7 = S7Simulator(args.s7_port)
        s7.start()
        s7.start_animation()
        started.append(s7)
        print(f"S7 simulator      127.0.0.1:{args.s7_port}  (DB100, rack 0 slot 1)")
    if not args.no_modbus:
        mb = ModbusSimulator(port=args.modbus_port, units=frozenset(int(u) for u in args.modbus_units.split(",")))
        await mb.start()
        mb.start_animation()
        started.append(mb)
        print(f"Modbus TCP sim    127.0.0.1:{args.modbus_port}  (units {args.modbus_units})")
    if not args.no_mc:
        mcs = MCSimulator(port=args.mc_port)
        await mcs.start()
        started.append(mcs)
        print(f"MC 3E/1E sim      127.0.0.1:{args.mc_port}")
    if not args.no_opcua:
        ua_sim = OpcUaSimulator(args.opcua_port)
        await ua_sim.start()
        ua_sim.start_animation()
        started.append(ua_sim)
        print(f"OPC UA simulator  {ua_sim.endpoint}  (namespace index {ua_sim.ns}, security None)")
    print("simulators running; Ctrl+C to stop")
    await stop.wait()
    for sim in started:
        await sim.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--s7-port", type=int, default=1102)
    p.add_argument("--modbus-port", type=int, default=1502)
    p.add_argument("--mc-port", type=int, default=5000)
    p.add_argument("--opcua-port", type=int, default=4841)
    p.add_argument("--modbus-units", default="1,2")
    for name in ("s7", "modbus", "mc", "opcua"):
        p.add_argument(f"--no-{name}", action="store_true")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main(p.parse_args()))
