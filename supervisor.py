"""
supervisor.py — one launcher for the whole two-source rig (feature/investing-source)

Manages three separate processes (design decision #1: isolation):
  1. probe.py              — FXStreet source
  2. investing_probe.py    — investing.com source
  3. merge_forwarder.py    — normalization + first-source-wins + Telegram

Any process that exits is restarted with backoff (max 60s). Ctrl+C kills all.

Run:  python supervisor.py
"""

import subprocess
import sys
import time
from datetime import datetime

PROCESSES = {
    "fxstreet": [sys.executable, "probe.py"],
    # "investing": [sys.executable, "investing_probe.py"],  # until IP is calm
    "mql5": [sys.executable, "mql5_probe.py"],
    "forwarder": [sys.executable, "merge_forwarder.py"],
}
RESTART_DELAY = 5
MAX_DELAY = 60


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def main():
    log("=== supervisor starting ===")
    running = {}
    backoff = {name: RESTART_DELAY for name in PROCESSES}

    for name, cmd in PROCESSES.items():
        log(f"starting {name}: {' '.join(cmd)}")
        running[name] = subprocess.Popen(cmd)
        backoff[name] = RESTART_DELAY

    try:
        while True:
            for name, proc in list(running.items()):
                rc = proc.poll()
                if rc is not None:
                    delay = backoff[name]
                    log(f"{name} exited (rc={rc}) — restarting in {delay}s")
                    time.sleep(delay)
                    running[name] = subprocess.Popen(PROCESSES[name])
                    backoff[name] = min(delay * 2, MAX_DELAY)
                else:
                    backoff[name] = RESTART_DELAY   # healthy: reset backoff
            time.sleep(2)
    except KeyboardInterrupt:
        log("shutting down all processes...")
        for name, proc in running.items():
            proc.terminate()
        for name, proc in running.items():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log("all processes stopped")


if __name__ == "__main__":
    main()
