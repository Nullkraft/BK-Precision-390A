#!/usr/bin/env python3
# /// script
# dependencies = [
#   "mcp[cli]",
#   "pyserial",
# ]
# ///

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import glob
import importlib.util
from pathlib import Path
import threading
import time
from typing import Any

import serial
from mcp.server.fastmcp import FastMCP


DEFAULT_PORT = "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0"

DEFAULT_GLOB_PATTERNS = (
    "/dev/ttyUSB*",
    "/dev/ttyACM*",
    "/dev/serial/by-id/*",
)


def load_parser():
    parser_path = Path(__file__).resolve().with_name("bk390a_parser.py")
    spec = importlib.util.spec_from_file_location("bk390a_parser", parser_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load parser module from %s" % parser_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


parser = load_parser()
mcp = FastMCP("bk390a", json_response=True)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_candidate_ports() -> list[str]:
    ports: list[str] = []
    for pattern in DEFAULT_GLOB_PATTERNS:
        ports.extend(glob.glob(pattern))
    return sorted(set(ports))


class FrameCache:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._frames: deque[dict[str, Any]] = deque(maxlen=16)
        self._active_port: str | None = None
        self._thread: threading.Thread | None = None
        self._seq = 0
        self._last_error: str | None = None

    def ensure_port(self, port: str) -> None:
        with self._condition:
            if self._active_port != port:
                self._active_port = port
                self._frames.clear()
                self._last_error = None
                self._condition.notify_all()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._reader_loop, daemon=True)
                self._thread.start()

    def latest(self, port: str, timeout_s: float) -> dict[str, Any]:
        self.ensure_port(port)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                frame = self._latest_for_port_locked(port)
                if frame is not None:
                    return dict(frame)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._last_error is not None:
                        raise TimeoutError(self._last_error)
                    raise TimeoutError("timed out waiting for data from %s" % port)
                self._condition.wait(timeout=remaining)

    def next_after(self, port: str, after_seq: int, timeout_s: float) -> dict[str, Any]:
        self.ensure_port(port)
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while True:
                frame = self._latest_for_port_locked(port)
                if frame is not None and frame["seq"] > after_seq:
                    return dict(frame)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._last_error is not None:
                        raise TimeoutError(self._last_error)
                    raise TimeoutError("timed out waiting for data from %s" % port)
                self._condition.wait(timeout=remaining)

    def _latest_for_port_locked(self, port: str) -> dict[str, Any] | None:
        for frame in reversed(self._frames):
            if frame["port"] == port:
                return frame
        return None

    def _reader_loop(self) -> None:
        handle: serial.Serial | None = None
        current_port: str | None = None
        while True:
            with self._condition:
                target_port = self._active_port
            if target_port is None:
                time.sleep(0.1)
                continue

            if current_port != target_port:
                if handle is not None:
                    handle.close()
                    handle = None
                try:
                    handle = serial.Serial(
                        port=target_port,
                        baudrate=2400,
                        bytesize=serial.SEVENBITS,
                        parity=serial.PARITY_ODD,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=1.0,
                    )
                    current_port = target_port
                    with self._condition:
                        self._last_error = None
                        self._condition.notify_all()
                except Exception as exc:
                    current_port = None
                    with self._condition:
                        self._last_error = str(exc)
                        self._condition.notify_all()
                    time.sleep(0.5)
                    continue

            try:
                assert handle is not None
                raw = handle.readline()
                if not raw:
                    continue
                text = raw.decode("ascii", errors="strict").strip()
                if not text:
                    continue
                parsed = parser.parse_frame(text)
            except (serial.SerialException, OSError, UnicodeError) as exc:
                with self._condition:
                    self._last_error = str(exc)
                    self._condition.notify_all()
                if handle is not None:
                    handle.close()
                    handle = None
                current_port = None
                time.sleep(0.5)
                continue
            except Exception as exc:
                with self._condition:
                    self._last_error = str(exc)
                    self._condition.notify_all()
                continue

            frame = {
                "seq": self._seq + 1,
                "port": current_port,
                "arrival_timestamp": utc_timestamp(),
                "raw_frame": text,
                "measurement": parsed,
            }
            self._seq += 1
            with self._condition:
                self._frames.append(frame)
                self._condition.notify_all()


frame_cache = FrameCache()


def read_one_frame(port: str, timeout_s: float) -> tuple[str, dict[str, Any], dict[str, Any]]:
    frame = frame_cache.latest(port, timeout_s)
    return frame["raw_frame"], frame["measurement"], frame


@mcp.tool()
def bk390a_list_ports() -> dict[str, Any]:
    """List likely serial devices for the BK Precision 390A."""
    return {
        "timestamp": utc_timestamp(),
        "ports": list_candidate_ports(),
        "patterns": list(DEFAULT_GLOB_PATTERNS),
    }


@mcp.tool()
def bk390a_read(
    port: str = DEFAULT_PORT,
    timeout_s: float = 2.0,
    require_stable: bool = True,
    max_frames: int = 6,
) -> dict[str, Any]:
    """Read and decode a measurement frame from the BK Precision 390A."""
    raw_frame, parsed, frame = read_one_frame(port, timeout_s)
    frames_seen = 1

    if not require_stable:
        now = datetime.now(timezone.utc)
        arrival = datetime.fromisoformat(frame["arrival_timestamp"])
        return {
            "timestamp": utc_timestamp(),
            "port": port,
            "stable": False,
            "frames_seen": frames_seen,
            "raw_frame": raw_frame,
            "measurement": parsed,
            "arrival_timestamp": frame["arrival_timestamp"],
            "age_s": (now - arrival).total_seconds(),
        }

    previous_seq = frame["seq"]
    previous_raw = raw_frame

    for _ in range(1, max_frames):
        next_frame = frame_cache.next_after(port, previous_seq, timeout_s)
        frames_seen += 1
        if next_frame["raw_frame"] == previous_raw:
            now = datetime.now(timezone.utc)
            arrival = datetime.fromisoformat(next_frame["arrival_timestamp"])
            return {
                "timestamp": utc_timestamp(),
                "port": port,
                "stable": True,
                "frames_seen": frames_seen,
                "raw_frame": next_frame["raw_frame"],
                "measurement": next_frame["measurement"],
                "arrival_timestamp": next_frame["arrival_timestamp"],
                "age_s": (now - arrival).total_seconds(),
            }
        previous_seq = next_frame["seq"]
        previous_raw = next_frame["raw_frame"]

    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(frame["arrival_timestamp"])
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "stable": False,
        "frames_seen": frames_seen,
        "raw_frame": raw_frame,
        "measurement": parsed,
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": (now - arrival).total_seconds(),
    }


@mcp.tool()
def bk390a_read_raw_frame(port: str = DEFAULT_PORT, timeout_s: float = 2.0) -> dict[str, Any]:
    """Read one raw meter frame and decode it."""
    raw_frame, parsed, frame = read_one_frame(port, timeout_s)
    now = datetime.now(timezone.utc)
    arrival = datetime.fromisoformat(frame["arrival_timestamp"])
    return {
        "timestamp": utc_timestamp(),
        "port": port,
        "raw_frame": raw_frame,
        "measurement": parsed,
        "arrival_timestamp": frame["arrival_timestamp"],
        "age_s": (now - arrival).total_seconds(),
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
