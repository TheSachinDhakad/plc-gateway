"""Windows service host for the gateway (pywin32).

Install / control (elevated PowerShell, from the project directory, using the venv python):

    .venv\\Scripts\\python.exe deploy\\gateway_service.py --startup auto install
    .venv\\Scripts\\python.exe deploy\\gateway_service.py start
    .venv\\Scripts\\python.exe deploy\\gateway_service.py stop
    .venv\\Scripts\\python.exe deploy\\gateway_service.py remove

``deploy/install_windows.ps1`` performs these steps, registers the pywin32 DLLs and sets
restart-on-failure recovery actions. Stop requests set the gateway's stop event, so shutdown
is graceful: workers stop, the queue is flushed to the spool, connections close.

Optional environment (system or service): PLC_GATEWAY_CONFIG_DIR, PLC_GATEWAY_ENV_FILE.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import servicemanager  # noqa: E402  (pywin32, Windows only)
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


class PLCGatewayService(win32serviceutil.ServiceFramework):
    _svc_name_ = "PLCGateway"
    _svc_display_name_ = "Industrial PLC Gateway"
    _svc_description_ = "Acquires PLC data (S7, Logix, MC, Modbus, OPC UA/DA) and stores it in MySQL."

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self._win_stop = win32event.CreateEvent(None, 0, 0, None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60_000)
        if self._ready.wait(10) and self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        win32event.SetEvent(self._win_stop)

    def SvcDoRun(self) -> None:
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        os.chdir(ROOT)
        config_dir = Path(os.environ.get("PLC_GATEWAY_CONFIG_DIR", ROOT / "config"))
        env_file = Path(os.environ.get("PLC_GATEWAY_ENV_FILE", ROOT / ".env"))
        try:
            asyncio.run(self._main(config_dir, env_file))
        except Exception as exc:
            servicemanager.LogErrorMsg(f"{self._svc_name_} terminated: {type(exc).__name__}: {exc}")
            raise
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                              (self._svc_name_, ""))

    async def _main(self, config_dir: Path, env_file: Path) -> None:
        from main import run_gateway

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._ready.set()
        await run_gateway(config_dir, env_file, self._stop)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(PLCGatewayService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(PLCGatewayService)
