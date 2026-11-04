"""Probe a BLUETTI station's encrypted key exchange: read-only, nothing written.

For when a key exchange stalls. It logs every key-exchange message per connection, sent in plain text (the normal start)
or inside the encrypted channel (which bluetti-bt-lib ignores; see its PR #99), across two tests: 8 connections back to
back, and two processes overlapping. See docs/history.md for what it found on an Elite 10.

Stop the monitor first, then from a real Terminal window:
    python scripts/bluetti_handshake_probe.py ADDRESS
About 3 minutes. Details go to bluetti_handshake_probe.log; the summary prints at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time

from bleak import BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.const import NOTIFY_UUID
from bluetti_bt_lib.registers import ReadableRegisters
from bluetti_bt_lib.utils.device_builder import build_device

_LOG = "bluetti_handshake_probe.log"
_KEX_TYPES = {1: "challenge", 3: "challenge accepted", 4: "device public key", 6: "public key accepted"}


class _ProbeReader(DeviceReader):
    """Records, with times, every key-exchange message the device sends and whether it came in plain text."""

    def __init__(self, *args, tag: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tag = tag
        self.t0 = time.monotonic()
        self.events: list[str] = []
        decrypt = self.encryption.aes_decrypt

        def logged_decrypt(data, key, iv):
            out = decrypt(data, key, iv)
            if out[:2] == b"**" and len(out) > 2:
                self._note(f"inside the encrypted channel: {_KEX_TYPES.get(out[2], out[2])}"
                           f"{' (after the session key was set: the #99 case)' if self.encryption.secure_aes_key else ''}")
            return out

        self.encryption.aes_decrypt = logged_decrypt

    def _note(self, what: str) -> None:
        entry = f"{time.monotonic() - self.t0:5.1f}s {what}"
        self.events.append(entry)
        logging.info("[%s] %s", self.tag, entry)

    async def _notification_handler(self, sender, data: bytearray):
        if bytes(data[:2]) == b"**" and len(data) > 2:
            self._note(f"plain text: {_KEX_TYPES.get(data[2], data[2])}")
        await super()._notification_handler(sender, data)


async def _session(mac: str, tag: str, hold_seconds: float, handshake_seconds: float = 20.0) -> dict:
    """One connection: key exchange, a battery read every 2 s for `hold_seconds`, disconnect."""
    start = time.monotonic()
    found = await BleakScanner.find_device_by_address(mac, timeout=20)
    if found is None:
        return {"tag": tag, "outcome": "not found in scan"}
    client = await establish_connection(BleakClientWithServiceCache, found, found.name or mac, max_attempts=3)
    connected = time.monotonic() - start
    reader = _ProbeReader(mac, build_device("EL1000000000"), asyncio.Future, DeviceReaderConfig(use_encryption=True),
                          lock=asyncio.Lock(), ble_client=client, tag=tag)
    reader.client = client
    result = {"tag": tag, "connected_after_s": round(connected, 1)}
    try:
        await client.start_notify(NOTIFY_UUID, reader._notification_handler)
        reader.has_notifier = True
        try:
            async with asyncio.timeout(handshake_seconds):
                while not reader.encryption.is_ready_for_commands:
                    await asyncio.sleep(0.2)
            result["handshake_s"] = round(time.monotonic() - reader.t0, 1)
        except TimeoutError:
            result["outcome"] = f"key exchange not finished in {handshake_seconds:.0f} s"
            return result
        reads = ok = 0
        until = time.monotonic() + hold_seconds
        while True:
            reads += 1
            register = ReadableRegisters(102, 1)
            ok += bool(await reader._async_send_command(register))
            if time.monotonic() >= until:
                break
            await asyncio.sleep(2)
        result["outcome"] = f"ok, {ok}/{reads} reads answered"
        return result
    finally:
        result["events"] = reader.events
        try:
            await client.stop_notify(NOTIFY_UUID)
        except Exception:
            pass
        await client.disconnect()


def _child(mac: str, tag: str, hold: float) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "scripts.bluetti_handshake_probe", "--session", tag,
                             "--hold", str(hold)], stdout=subprocess.PIPE, text=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("address", help="the station's address, from `ups-sentry scan`")
    parser.add_argument("--session", help=argparse.SUPPRESS)
    parser.add_argument("--hold", type=float, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    logging.basicConfig(filename=_LOG, level=logging.INFO, format="%(asctime)s %(process)d %(message)s")
    for name in ("bluetti_bt_lib", "bleak", "bleak_retry_connector"):
        logging.getLogger(name).setLevel(logging.WARNING)  # the library logs session keys at INFO
    mac = args.address

    if args.session:
        print(json.dumps(asyncio.run(_session(mac, args.session, args.hold))), flush=True)
        return

    results = []
    print("Test 1: 8 connections back to back (gaps 0, 1, 3 s)...", flush=True)
    for i, gap in enumerate((0, 0, 1, 1, 3, 3, 0, 1), start=1):
        results.append(asyncio.run(_session(mac, f"seq{i} (gap {gap} s)", hold_seconds=0)))
        print(f"  {results[-1]['tag']}: {results[-1].get('outcome')}", flush=True)
        time.sleep(gap)
    print("Test 2: two processes overlapping (B connects while A holds its session), twice...", flush=True)
    for round_ in (1, 2):
        a = _child(mac, f"overlap{round_}-A", hold=30)
        time.sleep(8)
        b = _child(mac, f"overlap{round_}-B", hold=5)
        for proc in (a, b):
            out, _ = proc.communicate(timeout=180)
            results.append(json.loads(out.strip().splitlines()[-1]) if out.strip() else {"outcome": "no output"})
            print(f"  {results[-1].get('tag')}: {results[-1].get('outcome')}", flush=True)
        time.sleep(5)

    logging.info("SUMMARY %s", json.dumps(results))
    print("\nKey-exchange messages per connection:")
    for result in results:
        print(f"{result.get('tag')}: {result.get('outcome')} (handshake {result.get('handshake_s', '-')} s)")
        for event in result.get("events", []):
            print(f"    {event}")
    print(f"\nFull log: {_LOG}. You can restart the monitor now.")


if __name__ == "__main__":
    main()
