"""Tests for src/ups_sentry/drivers/bluetti.py. pytest tests/test_bluetti.py -v"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from bluetti_bt_lib.fields import FieldName

from ups_sentry.device import DeviceReadError, DeviceWriteError
from ups_sentry.drivers.bluetti import BluettiDevice

EL10_ECO_REGISTERS = {"AC": 2017, "DC": 2014}


@pytest.fixture(autouse=True)
def ble(mocker):
    """The scan finds the device and the connection succeeds, unless a test says otherwise."""
    client = MagicMock()
    client.disconnect = AsyncMock()
    found = MagicMock()
    found.name = "EL10"
    scan = mocker.patch("ups_sentry.drivers.bluetti.BleakScanner.find_device_by_address", new=AsyncMock(return_value=found))
    connect = mocker.patch("ups_sentry.drivers.bluetti.establish_connection", new=AsyncMock(return_value=client))
    return MagicMock(client=client, scan=scan, connect=connect)


def _patch_reader(mocker, *, recognized_encrypted=False, built_device="_default", read_result):
    mocker.patch(
        "ups_sentry.drivers.bluetti.recognize_device",
        new=AsyncMock(return_value=MagicMock(encrypted=recognized_encrypted)),
    )
    if built_device == "_default":
        built_device = MagicMock()
    mocker.patch("ups_sentry.drivers.bluetti.build_device", return_value=built_device)
    reader_instance = MagicMock()
    if isinstance(read_result, list):
        reader_instance.read = AsyncMock(side_effect=read_result)
    else:
        reader_instance.read = AsyncMock(return_value=read_result)
    reader_class = mocker.patch("ups_sentry.drivers.bluetti.DeviceReader", return_value=reader_instance)
    return reader_class


GOOD = {FieldName.BATTERY_SOC.value: 80, FieldName.AC_INPUT_VOLTAGE.value: 120.0}


def station(*, eco_registers=EL10_ECO_REGISTERS, **kwargs):
    return BluettiDevice("AA:BB:CC:DD:EE:FF", "EL10", eco_registers=eco_registers, retry_wait_seconds=0, **kwargs)


class TestReadState:
    def test_successful_read_parses_battery_and_ac_input_present(self, mocker):
        _patch_reader(
            mocker,
            recognized_encrypted=True,
            read_result={FieldName.BATTERY_SOC.value: 55, FieldName.AC_INPUT_VOLTAGE.value: 116.6},
        )
        state = station().read_state()
        assert state.battery_pct == 55.0
        assert state.ac_input_present is True

    def test_ac_input_absent_when_voltage_at_or_below_threshold(self, mocker):
        """Regression guard: a naive `voltage != 0` check would still call
        this "present" on residual noise -- the real, tiny margin found
        during hardware testing is why this uses a >10V threshold."""
        _patch_reader(
            mocker,
            read_result={FieldName.BATTERY_SOC.value: 40, FieldName.AC_INPUT_VOLTAGE.value: 0.4},
        )
        state = station().read_state()
        assert state.ac_input_present is False

    def test_unrecognized_device_raises(self, mocker):
        mocker.patch(
            "ups_sentry.drivers.bluetti.recognize_device", new=AsyncMock(return_value=None)
        )
        with pytest.raises(DeviceReadError, match="recognize"):
            station().read_state()

    def test_unsupported_device_type_raises(self, mocker):
        _patch_reader(mocker, built_device=None, read_result={})
        with pytest.raises(DeviceReadError, match="no device class"):
            station().read_state()

    def test_none_read_result_raises(self, mocker):
        _patch_reader(mocker, read_result=None)
        with pytest.raises(DeviceReadError, match="BLE read failed"):
            station().read_state()

    def test_missing_expected_fields_raises(self, mocker):
        _patch_reader(mocker, read_result={FieldName.BATTERY_SOC.value: 10})
        with pytest.raises(DeviceReadError, match="missing expected fields"):
            station().read_state()

    def test_unexpected_exception_is_wrapped(self, mocker):
        mocker.patch(
            "ups_sentry.drivers.bluetti.recognize_device",
            new=AsyncMock(side_effect=RuntimeError("BLE adapter gone")),
        )
        with pytest.raises(DeviceReadError, match="BLE adapter gone"):
            station().read_state()


class TestConnections:
    """2026-10-11: one connection per read after a longer scan, recognition remembered, one retry, reasons kept."""

    def test_recognizes_once_across_reads(self, mocker):
        _patch_reader(mocker, read_result=GOOD)
        power_station = station()
        for _ in range(3):
            power_station.read_state()
        assert power_station_recognize_calls(mocker) == 1

    def test_reader_uses_our_connection_and_it_is_closed(self, mocker, ble):
        reader_class = _patch_reader(mocker, recognized_encrypted=True, read_result=GOOD)
        station(scan_seconds=20).read_state()
        assert ble.scan.await_args.kwargs["timeout"] == 20
        assert reader_class.call_args.kwargs["ble_client"] is ble.client
        assert reader_class.call_args.args[3].use_encryption is True
        ble.client.disconnect.assert_awaited()

    def test_connection_closed_when_the_read_raises(self, mocker, ble):
        reader_class = _patch_reader(mocker, read_result=GOOD)
        reader_class.return_value.read = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(DeviceReadError):
            station().read_state()
        assert ble.client.disconnect.await_count == 2  # once per attempt

    def test_one_failed_attempt_is_retried(self, mocker):
        _patch_reader(mocker, read_result=[None, GOOD])
        assert station().read_state().battery_pct == 80.0

    def test_not_seen_in_scan(self, mocker, ble):
        _patch_reader(mocker, read_result=GOOD)
        ble.scan.return_value = None
        with pytest.raises(DeviceReadError, match="not seen in a 20 s scan"):
            station().read_state()
        ble.connect.assert_not_awaited()

    def test_library_reason_is_in_the_error(self, mocker):
        reader_class = _patch_reader(mocker, read_result=None)

        async def read_logging_a_timeout():
            logging.getLogger("bluetti_bt_lib.bluetooth.device_reader.x").warning("Timeout")
            return None

        reader_class.return_value.read = AsyncMock(side_effect=read_logging_a_timeout)
        with pytest.raises(DeviceReadError, match=r"BLE read failed .*\(Timeout\)"):
            station().read_state()
        assert not logging.getLogger("bluetti_bt_lib").handlers  # the capture handler is removed

    def test_recognized_again_after_five_failed_reads(self, mocker):
        reader_class = _patch_reader(mocker, read_result=None)
        power_station = station()
        for _ in range(5):
            with pytest.raises(DeviceReadError):
                power_station.read_state()
        reader_class.return_value.read = AsyncMock(return_value=GOOD)
        power_station.read_state()
        assert power_station_recognize_calls(mocker) == 2


def power_station_recognize_calls(mocker) -> int:
    import ups_sentry.drivers.bluetti as module

    return module.recognize_device.await_count


class TestFields:
    def test_every_field_is_returned_json_friendly_with_the_eco_modes(self, mocker):
        from decimal import Decimal
        from enum import Enum

        class Mode(Enum):
            STANDARD = 1

        read = {**GOOD, "time_remaining": Decimal("1.5"), "ctrl_eco_ac": True, "ctrl_eco_dc": False,
                "ctrl_charging_mode": Mode.STANDARD}
        _patch_reader(mocker, read_result=read)
        state = station().read_state()
        assert state.auto_off == frozenset({"AC"})
        assert state.fields["time_remaining"] == 1.5 and state.fields["ctrl_charging_mode"] == "STANDARD"
        assert state.fields[FieldName.BATTERY_SOC.value] == 80

    def test_output_watts_add_ac_and_dc(self, mocker):
        _patch_reader(mocker, read_result={**GOOD, FieldName.AC_OUTPUT_POWER.value: 12,
                                           FieldName.DC_OUTPUT_POWER.value: 3})
        assert station().read_state().output_watts == 15.0

    def test_output_watts_missing_is_none(self, mocker):
        _patch_reader(mocker, read_result=GOOD)
        assert station().read_state().output_watts is None

    def test_eco_settings_missing_is_none(self, mocker):
        _patch_reader(mocker, read_result=GOOD)
        assert station().read_state().auto_off is None

    def test_the_stations_own_estimate_is_kept_on_battery_only(self, mocker):
        _patch_reader(mocker, read_result={FieldName.BATTERY_SOC.value: 80, FieldName.AC_INPUT_VOLTAGE.value: 0.0,
                                           FieldName.TIME_REMAINING.value: 1.5})
        assert station().read_state().vendor_minutes_left == 90.0

    def test_on_wall_power_time_remaining_is_time_to_full_so_it_is_dropped(self, mocker):
        _patch_reader(mocker, read_result={**GOOD, FieldName.TIME_REMAINING.value: 1.5})
        assert station().read_state().vendor_minutes_left is None

    def test_both_eco_modes_off_is_empty(self, mocker):
        _patch_reader(mocker, read_result={**GOOD, "ctrl_eco_ac": False, "ctrl_eco_dc": False})
        assert station().read_state().auto_off == frozenset()


def _frame(value: int) -> bytes:
    """The device's reply to reading one register holding `value`."""
    import crcmod.predefined

    body = bytes([1, 3, 2, 0, value])
    return body + crcmod.predefined.mkCrcFun("modbus")(body).to_bytes(2, "little")


def _patch_writer(mocker, *, write_replies=None, eco_after=(), opens=None, built_device="_real"):
    """The real EL10 map from bluetti-bt-lib unless a test swaps it. Writes are echoed and the ECO switches read back
    as `eco_after` (outputs reading on) unless a test says otherwise; `opens` scripts each key exchange."""
    from bluetti_bt_lib.registers import WriteableRegister
    from bluetti_bt_lib.utils.device_builder import build_device

    _patch_reader(mocker, read_result=GOOD,
                  built_device=build_device("EL1000000000") if built_device == "_real" else built_device)
    replies = iter(write_replies or ())

    def send(register):
        if isinstance(register, WriteableRegister):
            return next(replies, bytes(register))
        return _frame(1 if {2017: "AC", 2014: "DC"}[register.starting_address] in eco_after else 0)

    writer = MagicMock()
    writer.open = AsyncMock(side_effect=opens)
    writer.send = AsyncMock(side_effect=send)
    writer.close = AsyncMock()
    writer_class = mocker.patch("ups_sentry.drivers.bluetti._RegisterWriter", return_value=writer)
    mocker.patch("ups_sentry.drivers.bluetti.asyncio.sleep", new=AsyncMock())
    return writer, writer_class


def _writes(writer) -> list[tuple[int, int]]:
    from bluetti_bt_lib.registers import WriteableRegister

    return [(call.args[0].address, call.args[0].value) for call in writer.send.await_args_list
            if isinstance(call.args[0], WriteableRegister)]


class TestSwitchEcoOff:
    def test_writes_zero_to_the_two_eco_switches_only_and_reads_both_back(self, mocker, ble):
        writer, writer_class = _patch_writer(mocker)
        assert station().switch_eco_off({"AC", "DC"}) == frozenset()
        assert _writes(writer) == [(2017, 0), (2014, 0)]
        assert writer.send.await_count == 4  # two writes, two reads, one session
        assert writer_class.call_count == 1 and writer_class.call_args.args[3].use_encryption is False
        writer.close.assert_awaited_once()
        ble.client.disconnect.assert_awaited()

    def test_unknown_output_raises_before_connecting(self, mocker, ble):
        _patch_writer(mocker)
        with pytest.raises(DeviceWriteError, match="No ECO switch"):
            station().switch_eco_off({"AC", "USB"})
        ble.connect.assert_not_awaited()

    def test_library_map_disagreeing_writes_nothing(self, mocker):
        """If bluetti-bt-lib ever moved ctrl_eco_ac, writing to its new address could hit another switch."""
        device = MagicMock()
        device.fields = [MagicMock(address=2011), MagicMock(address=2014)]
        device.fields[0].name, device.fields[1].name = "ctrl_eco_ac", "ctrl_eco_dc"
        writer, _ = _patch_writer(mocker, built_device=device)
        with pytest.raises(DeviceWriteError, match="not writing"):
            station().switch_eco_off({"AC"})
        writer.send.assert_not_awaited()

    @pytest.mark.parametrize("reply, match", [
        (b"", "No reply"),
        (bytes.fromhex("018602c3a1"), "refused"),
        (bytes.fromhex("010607e10001"), "Unexpected reply"),
    ])
    def test_a_bad_reply_raises_and_closes_the_connection(self, mocker, ble, reply, match):
        writer, _ = _patch_writer(mocker, write_replies=[reply])
        with pytest.raises(DeviceWriteError, match=match):
            station().switch_eco_off({"AC"})
        writer.close.assert_awaited_once()
        ble.client.disconnect.assert_awaited()

    def test_still_on_after_the_write_raises(self, mocker):
        _patch_writer(mocker, eco_after={"AC"})
        with pytest.raises(DeviceWriteError, match="still reads on"):
            station().switch_eco_off({"AC"})

    def test_another_output_on_is_returned_not_raised(self, mocker):
        _patch_writer(mocker, eco_after={"DC"})
        assert station().switch_eco_off({"AC"}) == frozenset({"DC"})

    def test_without_a_profile_the_librarys_map_decides(self, mocker):
        writer, _ = _patch_writer(mocker)
        assert station(eco_registers=None).switch_eco_off({"AC"}) == frozenset()
        assert _writes(writer) == [(2017, 0)]

    def test_a_model_without_the_switch_writes_nothing(self, mocker):
        device = MagicMock()
        device.fields = []
        writer, _ = _patch_writer(mocker, built_device=device)
        with pytest.raises(DeviceWriteError, match="no ctrl_eco_ac on EL10"):
            station(eco_registers=None).switch_eco_off({"AC"})
        writer.send.assert_not_awaited()

    def test_a_stalled_key_exchange_is_retried_on_a_new_connection(self, mocker, ble):
        """A live run timed out waiting for the key exchange; nothing may be sent before it."""
        writer, writer_class = _patch_writer(mocker, opens=[TimeoutError(), None])
        assert station().switch_eco_off({"AC"}) == frozenset()
        assert ble.connect.await_count == 2 and writer_class.call_count == 2
        assert _writes(writer) == [(2017, 0)]

    def test_key_exchange_never_finishing_writes_nothing(self, mocker, ble):
        writer, _ = _patch_writer(mocker, opens=[TimeoutError()] * 3)
        with pytest.raises(DeviceWriteError, match="nothing written"):
            station().switch_eco_off({"AC"})
        writer.send.assert_not_awaited()
        assert ble.connect.await_count == 3


class TestRegisterWriter:
    def test_waits_for_the_key_exchange_sends_and_stops_listening(self, mocker):
        from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig
        from bluetti_bt_lib.registers import WriteableRegister

        from ups_sentry.drivers.bluetti import _RegisterWriter

        client = MagicMock()
        client.start_notify, client.stop_notify = AsyncMock(), AsyncMock()
        writer = _RegisterWriter("AA", MagicMock(), asyncio.Future, DeviceReaderConfig(use_encryption=True),
                                 lock=asyncio.Lock(), ble_client=client)
        ready = iter([False, True])
        mocker.patch.object(type(writer.encryption), "is_ready_for_commands",
                            new=property(lambda self: next(ready, True)))
        mocker.patch("ups_sentry.drivers.bluetti.asyncio.sleep", new=AsyncMock())
        register = WriteableRegister(2017, 0)
        writer._async_send_command = AsyncMock(return_value=bytearray(bytes(register)))

        async def run():
            await writer.open(5)
            reply = await writer.send(register)
            await writer.close()
            return reply

        assert asyncio.run(run()) == bytes(register)
        writer._async_send_command.assert_awaited_once_with(register)
        client.stop_notify.assert_awaited_once()

    def test_open_times_out_when_the_key_exchange_never_finishes(self):
        from bluetti_bt_lib.bluetooth.device_reader import DeviceReaderConfig

        from ups_sentry.drivers.bluetti import _RegisterWriter

        client = MagicMock()
        client.start_notify = AsyncMock()
        writer = _RegisterWriter("AA", MagicMock(), asyncio.Future, DeviceReaderConfig(use_encryption=True),
                                 lock=asyncio.Lock(), ble_client=client)
        with pytest.raises(TimeoutError):
            asyncio.run(writer.open(0.01))
