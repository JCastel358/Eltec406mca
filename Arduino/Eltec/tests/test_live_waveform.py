"""Hardware-free stream boundaries, compatibility and viewer controls."""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_waveform as viewer
from matplotlib.backend_bases import KeyEvent, MouseEvent


class SerialStub:
    """Serial byte stream; uses the real Esp32Rig command/reply parser."""

    def __init__(self):
        self.pending = bytearray()
        self.commands = []
        self.start_reply = None

    def write(self, payload):
        command = payload.decode("ascii").strip()
        self.commands.append(command)
        if command.startswith("STREAM,START"):
            label = command.split(",")[2] if command.count(",") == 2 else "SENSOR"
            reply = self.start_reply or f"STREAM,BEGIN,1000,{label}"
        elif command == "STREAM,STOP":
            reply = "STREAM,END,123,0"
        else:
            reply = f"OK,{command}"
        self.pending.extend((reply + "\n").encode("ascii"))

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, size):
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def readline(self):
        end = self.pending.find(b"\n")
        return self.read(end + 1 if end >= 0 else len(self.pending))


def make_rig(version=(3, 4)):
    rig = viewer.Esp32Rig("TEST-NO-HARDWARE")
    rig.firmware_version = version
    rig.ser = SerialStub()
    return rig


class ChannelTests(unittest.TestCase):
    def test_cli_aliases_and_case(self):
        for value, expected in (("sensor", "AIN0"), ("REF", "AIN1"),
                                ("ain7", "AIN7"), ("AIN4", "AIN4")):
            with self.subTest(value=value):
                self.assertEqual(viewer.normalize_channel(value), expected)
        for invalid in ("AIN8", "AIN-1", "AIN10", "BOTH", ""):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                viewer.normalize_channel(invalid)

    def test_every_input_starts_with_matching_acknowledgement(self):
        for channel in viewer.CHANNELS:
            with self.subTest(channel=channel):
                rig = make_rig()
                reader = viewer.StreamReader(rig, channel, 1)
                reader._start_stream(channel)
                expected = {"AIN0": "STREAM,START", "AIN1": "STREAM,START,REF"}.get(
                    channel, f"STREAM,START,{channel}")
                self.assertEqual(rig.ser.commands, [expected])
                self.assertEqual(reader.snapshot(), (channel, [], 0, 0))

    def test_legacy_channels_remain_available(self):
        self.assertEqual(viewer.stream_command("sensor", (1, 9)), "STREAM,START")
        self.assertEqual(viewer.stream_command("ref", (3, 2)), "STREAM,START,REF")
        self.assertEqual(viewer.stream_command("AIN3", (3, 3)), "STREAM,START,AIN3")

    def test_unsupported_selection_leaves_current_stream_untouched(self):
        for version, channel in (((3, 2), "AIN2"), ((3, 3), "AIN4"),
                                 ((3, 3), "AIN7"), (None, "AIN2")):
            with self.subTest(version=version, channel=channel):
                rig = make_rig(version)
                reader = viewer.StreamReader(rig, "sensor", 1)
                reader.data.append((1, 0.7, 0))
                with self.assertRaisesRegex(ValueError, "needs firmware"):
                    reader.select_channel(channel)
                self.assertEqual(reader.snapshot()[1], [(1, 0.7, 0)])
                self.assertTrue(reader._commands.empty())
                self.assertEqual(rig.ser.commands, [])

    def test_switch_drains_backlog_and_discards_partial_old_channel(self):
        rig = make_rig()
        reader = viewer.StreamReader(rig, "AIN0", 1)
        reader.data.append((1, 0.7, 0))
        reader.total, reader.torn, reader.lag_s = 100, 2, 0.5
        # The first half of a line was already read in bulk; its tail and
        # another complete old-channel sample are still in the serial driver.
        buf = bytearray(b"D,123,")
        rig.ser.pending.extend(b"0,0.7,0\nD,124,0,0.7,0\n")
        reader.select_channel("AIN7")
        reader._flush_commands(buf)
        self.assertEqual(rig.ser.commands, ["STREAM,STOP", "STREAM,START,AIN7"])
        self.assertEqual(buf, b"")
        self.assertEqual(rig.ser.pending, b"")
        self.assertEqual(reader.snapshot(), ("AIN7", [], 0, 0))
        self.assertEqual(reader.lag_s, 0)
        # First new samples must survive without the old partial prefix.
        with patch.object(rig.ser, "read", side_effect=[b"D,125,0,1.7,1\n", b""]):
            with patch.object(reader._stop_event, "is_set",
                              side_effect=[False, False, False, True]):
                reader._read_stream(buf)
        self.assertEqual(reader.snapshot()[1], [(125, 1.7, 1)])

    def test_failed_ack_does_not_relabel_old_samples(self):
        for reply in ("STREAM,BEGIN,1000,AIN2", "STREAM,BEGIN,500,AIN7",
                      "ERR,ADS1256 channel select/calibration timeout"):
            with self.subTest(reply=reply):
                rig = make_rig()
                rig.ser.start_reply = reply
                reader = viewer.StreamReader(rig, "AIN0", 1)
                reader.data.append((1, 0.7, 0))
                reader.select_channel("AIN7")
                with self.assertRaises(RuntimeError):
                    reader._flush_commands(bytearray())
                self.assertEqual(reader.snapshot()[0:2], ("AIN0", [(1, 0.7, 0)]))

    def test_reselecting_same_input_does_not_restart(self):
        rig = make_rig()
        reader = viewer.StreamReader(rig, "AIN0", 1)
        reader.select_channel("sensor")
        reader._flush_commands(bytearray())
        self.assertEqual(rig.ser.commands, [])

    def test_pwm_commands_keep_their_order_across_channel_switches(self):
        rig = make_rig()
        reader = viewer.StreamReader(rig, "AIN0", 1)
        reader.send("PWM,ON")
        reader.select_channel("AIN6")
        reader.send("PWM,OFF")
        reader._flush_commands(bytearray())
        self.assertEqual(rig.ser.commands,
                         ["PWM,ON", "STREAM,STOP", "STREAM,START,AIN6", "PWM,OFF"])

    def test_thread_joins_and_shuts_down_after_startup_error(self):
        rig = make_rig()
        rig.ser.start_reply = "ERR,ADS1256 timeout"
        reader = viewer.StreamReader(rig, "AIN7", 1)
        reader.start()
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertIn("ADS1256 timeout", reader.command_error)
        self.assertEqual(rig.ser.commands[-2:], ["PWM,OFF", "STREAM,STOP"])

    def test_normal_stop_skips_pending_switch_and_joins(self):
        rig = make_rig()
        reader = viewer.StreamReader(rig, "AIN0", 1)
        reader.select_channel("AIN7")
        reader.stop()
        reader.start()
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertIsNone(reader.command_error)
        self.assertNotIn("STREAM,START,AIN7", rig.ser.commands)
        self.assertEqual(rig.ser.commands[-2:], ["PWM,OFF", "STREAM,STOP"])

    def test_plot_buttons_keys_and_empty_history_clear_all_panels(self):
        rig = make_rig()
        reader = viewer.StreamReader(rig, "AIN0", 1)
        reader.data.extend((i * 1000, 0.7 + (i % 10) * 0.001, (i // 5) % 2)
                           for i in range(100))
        update = None

        def animation(_fig, callback, **_kwargs):
            nonlocal update
            update = callback

        def show():
            fig = viewer.plt.gcf()
            update(0)
            fig.canvas.draw()
            self.assertGreater(len(fig.axes[2].lines[0].get_xdata()), 0)
            inputs = [ax for ax in fig.axes
                      if ax.texts and ax.texts[0].get_text() in viewer.CHANNELS]
            self.assertEqual(len(inputs), 8)
            for index, ax in enumerate(inputs):
                x, y = ax.transAxes.transform((0.5, 0.5))
                for event_name in ("button_press_event", "button_release_event"):
                    event = MouseEvent(event_name, fig.canvas, x, y, button=1)
                    fig.canvas.callbacks.process(event_name, event)
                reader._flush_commands(bytearray())
                update(index)
                self.assertIn(f"AIN{index}", fig.axes[0].get_title())
            for ax in fig.axes[:3]:
                self.assertEqual(len(ax.lines[0].get_xdata()), 0)
            for index in range(8):
                event = KeyEvent("key_press_event", fig.canvas, key=str(index))
                fig.canvas.callbacks.process("key_press_event", event)
                reader._flush_commands(bytearray())
                update(index)
                self.assertEqual(reader.channel, f"AIN{index}")
            viewer.plt.close(fig)

        args = SimpleNamespace(channel="AIN0", window=1, max_window=1,
                               fold_cycles=4, fe=None)
        with patch.object(viewer, "StreamReader", return_value=reader), \
                patch.object(reader, "start"), patch.object(reader, "join"), \
                patch.object(reader, "is_alive", return_value=True), \
                patch.object(viewer, "FuncAnimation", side_effect=animation), \
                patch.object(viewer.plt, "show", side_effect=show), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(viewer.run_viewer(rig, args, emitter_on=False))


if __name__ == "__main__":
    unittest.main()
