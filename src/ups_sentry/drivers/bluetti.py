"""BLUETTI power stations over Bluetooth Low Energy, through the third-party bluetti-bt-lib.

bluetti-bt-lib's own writer refuses encrypted models, but its reader already does the key exchange and encrypts what it
sends; `_RegisterWriter` reuses both. That means subclassing the reader's private methods, which is why pyproject.toml
pins the library's exact version.

Every read opens a fresh connection rather than keeping one open, which avoids a long-lived session's failure modes.
On macOS, only a process started from a real logged-in Terminal window can use Bluetooth.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from bleak import BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from bluetti_bt_lib.bluetooth.device_reader import DeviceReader, DeviceReaderConfig
from bluetti_bt_lib.bluetooth.device_recognizer import recognize_device
from bluetti_bt_lib.const import NOTIFY_UUID
from bluetti_bt_lib.fields import FieldName
from bluetti_bt_lib.registers import DeviceRegister, ReadableRegisters, WriteableRegister
from bluetti_bt_lib.utils.device_builder import build_device

from ups_sentry.device import DeviceReadError, DeviceState, DeviceWriteError


class _LastMessage(logging.Handler):
    """Keeps the last warning or error bluetti-bt-lib or bleak-retry-connector logged: they swallow their exceptions and
    return None, so this is the only place a failed read says why (e.g. "Device not found", "Timeout")."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.message: str | None = None

    def emit(self, record: logging.LogRecord) -> None:
        self.message = record.getMessage()


_LIBRARY_LOGGERS = ("bluetti_bt_lib", "bleak_retry_connector")


class _RegisterWriter(DeviceReader):
    """Sends registers over a connection we opened, encrypted when the device is: `open` (key exchange), `send`, `close`.

    Keeps one session for the writes and the read-back. The reply to a write echoes the request (Modbus); an exception
    reply means it was refused.
    """

    async def open(self, handshake_seconds: float) -> None:
        """Listen for the device's messages and wait for the key exchange; TimeoutError if it doesn't finish."""
        self.client = self.ble_client
        await self.client.start_notify(NOTIFY_UUID, self._notification_handler)
        self.has_notifier = True
        async with asyncio.timeout(handshake_seconds):
            while self.config.use_encryption and not self.encryption.is_ready_for_commands:
                await asyncio.sleep(0.5)

    async def send(self, register: DeviceRegister) -> bytes:
        """Send `register` and return the reply (empty if none came)."""
        return bytes(await self._async_send_command(register))

    async def close(self) -> None:
        if self.has_notifier:
            try:
                await self.client.stop_notify(NOTIFY_UUID)
            except Exception:
                pass
            self.has_notifier = False
        self.encryption.reset()
        self.encrypted_buffer.clear()


class BluettiDevice:
    """Reads a BLUETTI station's state and can switch its ECO modes off.

    The device is recognized (model, and whether it encrypts; up to four connections of the library's own) once and
    remembered, and recognized again only after `_FAILURES_TO_RECOGNIZE_AGAIN` failed reads in a row. Each read scans
    for the device for `scan_seconds` (the library's own scan gives up after 5 s, too short in practice), connects and
    reads; a failed read is tried once more after `retry_wait_seconds`.
    """

    # Some AC-input wattage readings hover just above zero from measurement noise even in a real outage, so presence
    # is decided from voltage (near zero only when mains is actually out) rather than power.
    _AC_INPUT_PRESENT_VOLTAGE_THRESHOLD: ClassVar[float] = 10.0
    _ATTEMPTS: ClassVar[int] = 2
    _FAILURES_TO_RECOGNIZE_AGAIN: ClassVar[int] = 5
    _HANDSHAKE_SECONDS: ClassVar[float] = 20.0
    _HANDSHAKE_ATTEMPTS: ClassVar[int] = 3
    _HANDSHAKE_RETRY_WAIT_SECONDS: ClassVar[float] = 10.0
    # The only fields `switch_eco_off` may write, and only ever 0.
    _ECO_SWITCHES: ClassVar[dict[str, FieldName]] = {"AC": FieldName.CTRL_ECO_AC, "DC": FieldName.CTRL_ECO_DC}

    def __init__(self, address: str, model: str, *, eco_registers: Mapping[str, int] | None = None,
                 scan_seconds: float = 20.0, retry_wait_seconds: float = 5.0) -> None:
        """Set which device to talk to and how patiently.

        Parameters
        ----------
        address : str
            BLE address (a MAC, or on macOS a UUID), as `ups-sentry scan` prints it.
        model : str
            bluetti-bt-lib's device type, e.g. "EL10".
        eco_registers : mapping, optional
            {"AC": register, "DC": register}, from the model's profile. When given, a write only goes ahead if the
            library's map agrees, so a library change can't turn it into a write to another register. Without it, the
            library's map decides alone.
        scan_seconds : float, optional
            How long each read looks for the device before connecting. Default 20.
        retry_wait_seconds : float, optional
            Pause before the second attempt of a failed read. Default 5.
        """
        self._address = address
        self._model = model
        self._eco_registers = dict(eco_registers or {})
        self._scan_seconds = scan_seconds
        self._retry_wait_seconds = retry_wait_seconds
        self._recognized = None
        self._failures_in_a_row = 0

    def read_state(self) -> DeviceState:
        """Connect and read.

        Raises
        ------
        DeviceReadError
            If the device can't be recognized, the library has no class for the model, or the read fails.
        """
        try:
            state = asyncio.run(self._read_with_retry())
        except Exception:
            self._failures_in_a_row += 1
            if self._failures_in_a_row >= self._FAILURES_TO_RECOGNIZE_AGAIN:
                self._recognized = None
            raise
        self._failures_in_a_row = 0
        return state

    def switch_eco_off(self, outputs: frozenset[str] | set[str]) -> frozenset[str]:
        """Switch the ECO mode of each of `outputs` ("AC", "DC") off, then read the ECO switches back.

        Writes 0 to the ECO switch registers only; never another register or value. The device's reply must echo each
        write. The writes and the read-back share one connection and one key exchange; a key exchange that stalls is
        retried on a new connection (nothing has been written by then).

        Returns
        -------
        frozenset of str
            The outputs whose ECO reads on after the writes (empty when all is well).

        Raises
        ------
        DeviceWriteError
            An unknown output, no such switch on this model, the library's map disagreeing with the profile, the key
            exchange never finishing, no reply, a refused or unexpected reply, an unreadable read-back, or one of
            `outputs` still on.
        DeviceReadError
            Connecting failed.
        """
        unknown = set(outputs) - set(self._ECO_SWITCHES)
        if unknown:
            raise DeviceWriteError(f"No ECO switch for {sorted(unknown)}; only {sorted(self._ECO_SWITCHES)}")
        on = asyncio.run(self._async_switch_eco_off(sorted(outputs)))
        if set(outputs) & on:
            raise DeviceWriteError(f"After the write, ECO still reads on for {sorted(set(outputs) & on)}")
        return on

    async def _async_switch_eco_off(self, outputs: list[str]) -> frozenset[str]:
        """Up to `_HANDSHAKE_ATTEMPTS` connections until a key exchange finishes; then the writes and the read-back."""
        for attempt in range(1, self._HANDSHAKE_ATTEMPTS + 1):
            device, client = await self._connect()
            writer = _RegisterWriter(
                self._address,
                device,
                asyncio.Future,
                DeviceReaderConfig(use_encryption=self._recognized.encrypted),
                lock=asyncio.Lock(),
                ble_client=client,
            )
            try:
                try:
                    await writer.open(self._HANDSHAKE_SECONDS)
                except TimeoutError:
                    if attempt == self._HANDSHAKE_ATTEMPTS:
                        raise DeviceWriteError(
                            f"The key exchange didn't finish in {self._HANDSHAKE_ATTEMPTS} connections of "
                            f"{self._HANDSHAKE_SECONDS:.0f} s; nothing written") from None
                else:
                    return await self._write_eco_off(device, writer, outputs)
            finally:
                await writer.close()
                await client.disconnect()
            await asyncio.sleep(self._HANDSHAKE_RETRY_WAIT_SECONDS)
        raise AssertionError("unreachable")

    def _eco_addresses(self, device) -> dict[str, int]:
        """{output: register} for the ECO switches the library's map has for this model."""
        addresses = {field.name: field.address for field in device.fields}
        return {output: addresses[name.value] for output, name in self._ECO_SWITCHES.items()
                if name.value in addresses}

    async def _write_eco_off(self, device, writer: _RegisterWriter, outputs: list[str]) -> frozenset[str]:
        """The checked writes, then the ECO switches read on the same session; returns the outputs reading on."""
        addresses = self._eco_addresses(device)
        for output in outputs:
            name = self._ECO_SWITCHES[output].value
            if output not in addresses:
                raise DeviceWriteError(f"bluetti-bt-lib has no {name} on {self._model}; not writing")
            expected = self._eco_registers.get(output)
            if expected is not None and addresses[output] != expected:
                raise DeviceWriteError(f"bluetti-bt-lib puts {name} at {addresses[output]}, not {expected}; not writing")
        for output in outputs:
            register = WriteableRegister(addresses[output], 0)
            reply = await writer.send(register)
            if not reply:
                raise DeviceWriteError(f"No reply to switching {output} ECO off")
            if register.is_exception_response(reply):
                raise DeviceWriteError(f"The station refused switching {output} ECO off (reply {reply.hex()})")
            if reply[:8] != bytes(register):
                raise DeviceWriteError(
                    f"Unexpected reply to switching {output} ECO off: {reply.hex()} (sent {bytes(register).hex()})")
        on = set()
        for output, address in addresses.items():
            register = ReadableRegisters(address, 1)
            reply = await writer.send(register)
            name = self._ECO_SWITCHES[output].value
            value = device.parse(address, register.parse_response(reply)).get(name) if reply else None
            if not isinstance(value, bool):
                raise DeviceWriteError(f"Couldn't read {output} ECO back after the write (reply {reply.hex()})")
            if value:
                on.add(output)
        return frozenset(on)

    async def _read_with_retry(self) -> DeviceState:
        """Up to `_ATTEMPTS` reads; the last failure is raised as a DeviceReadError, with the library's reason."""
        handler = _LastMessage()
        for name in _LIBRARY_LOGGERS:
            logging.getLogger(name).addHandler(handler)
        try:
            for attempt in range(1, self._ATTEMPTS + 1):
                handler.message = None
                try:
                    return await self._async_read_state()
                except Exception as exc:
                    reason = f" ({handler.message})" if handler.message else ""
                    if attempt == self._ATTEMPTS:
                        if isinstance(exc, DeviceReadError):
                            raise DeviceReadError(f"{exc}{reason}") from exc
                        raise DeviceReadError(f"BLE read of {self._address} failed: {exc}{reason}") from exc
                    await asyncio.sleep(self._retry_wait_seconds)
            raise AssertionError("unreachable")
        finally:
            for name in _LIBRARY_LOGGERS:
                logging.getLogger(name).removeHandler(handler)

    async def _connect(self):
        """Recognize the device (once), build its bluetti-bt-lib model, find it and connect; returns both.

        Raises
        ------
        DeviceReadError
            See `read_state`.
        """
        if self._recognized is None:
            recognized = await recognize_device(self._address, asyncio.Future)
            if recognized is None:
                raise DeviceReadError(f"Could not recognize BLUETTI device at {self._address}")
            self._recognized = recognized

        # build_device only inspects the name's model prefix; the trailing digits are the serial, irrelevant here.
        device = build_device(self._model + "00000000")
        if device is None:
            raise DeviceReadError(f"bluetti-bt-lib has no device class for '{self._model}'")

        found = await BleakScanner.find_device_by_address(self._address, timeout=self._scan_seconds)
        if found is None:
            raise DeviceReadError(f"BLUETTI device {self._address} not seen in a {self._scan_seconds:.0f} s scan")
        client = await establish_connection(
            BleakClientWithServiceCache, found, found.name or self._address, max_attempts=3
        )
        return device, client

    async def _async_read_state(self) -> DeviceState:
        """Recognize, connect to, and read the device.

        Raises
        ------
        DeviceReadError
            See `read_state`.
        """
        device, client = await self._connect()
        try:
            # Handing the reader our connection skips its own 5 s scan and connect.
            reader = DeviceReader(
                self._address,
                device,
                asyncio.Future,
                DeviceReaderConfig(use_encryption=self._recognized.encrypted),
                lock=asyncio.Lock(),
                ble_client=client,
            )
            data = await reader.read()
        finally:
            await client.disconnect()  # the reader disconnects too; a second disconnect is a no-op
        if data is None:
            raise DeviceReadError(f"BLE read failed for {self._address}")

        # parse() keys its result by plain field-name strings, not by the FieldName members (FieldName isn't a str
        # subclass), so looking these up by the bare member always misses.
        battery_pct = data.get(FieldName.BATTERY_SOC.value)
        ac_input_voltage = data.get(FieldName.AC_INPUT_VOLTAGE.value)
        if battery_pct is None or ac_input_voltage is None:
            raise DeviceReadError(f"BLUETTI read missing expected fields, got: {sorted(str(k) for k in data)}")

        outputs = [data.get(name.value) for name in (FieldName.AC_OUTPUT_POWER, FieldName.DC_OUTPUT_POWER)]
        watts = [float(w) for w in outputs if w is not None]
        eco = {output: data.get(name.value) for output, name in self._ECO_SWITCHES.items()}
        seen = {output: on for output, on in eco.items() if isinstance(on, bool)}
        hours_left = data.get(FieldName.TIME_REMAINING.value)
        ac_input_present = float(ac_input_voltage) > self._AC_INPUT_PRESENT_VOLTAGE_THRESHOLD
        return DeviceState(
            battery_pct=float(battery_pct),
            ac_input_present=ac_input_present,
            auto_off=frozenset(output for output, on in seen.items() if on) if seen else None,
            fields={str(key): _plain(value) for key, value in sorted(data.items(), key=lambda item: str(item[0]))},
            output_watts=sum(watts) if watts else None,
            # On wall power this field is the time to full, not the time left.
            vendor_minutes_left=(float(hours_left) * 60 if hours_left is not None and not ac_input_present else None),
        )


def _plain(value: object) -> object:
    """A JSON-friendly copy of one field's value: Decimal to float, an enum to its name."""
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.name
    return str(value)
