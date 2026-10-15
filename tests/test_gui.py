"""Window behaviour: dropping only scans; nothing is written until Convert is clicked.

Drives the real window (hidden) without a mouse; skipped when no display is available.
"""

import glob
import os
import shutil
import tempfile
import time
import unittest

from ebl_to_wav import engine

from test_conversion import write_bank

try:
    from tkinterdnd2 import TkinterDnD
    from ebl_to_wav import gui

    _root = TkinterDnD.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # no display / Tk
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class ScanThenConvert(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="e2w_gui_")
        self.out = os.path.join(self.tmp, "out")
        self.exb = write_bank(self.tmp)
        self._settings = gui.SETTINGS
        gui.SETTINGS = os.path.join(self.tmp, "settings.json")  # keep the user's settings untouched
        self.root = TkinterDnD.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root, [])
        self.app.destination = self.out
        self.app.format.set("wav")

    def tearDown(self) -> None:
        self.app._quit()
        gui.SETTINGS = self._settings
        engine.cleanup_temp()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, until, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while not until():
            self.assertLess(time.monotonic(), end, "timed out")
            self.root.update()
            time.sleep(0.02)

    def outputs(self) -> list[str]:
        return glob.glob(os.path.join(self.out, "**", "*.wav"), recursive=True)

    def states(self) -> list[str]:
        return [self.app.tree.set(i, "state") for i in self.app.tree.get_children()]

    def test_drop_scans_and_waits_for_convert(self):
        self.app.add([self.exb])
        self.pump(lambda: self.app.pending)
        self.assertEqual(len(self.app.pending), 3)
        self.assertEqual(self.states(), ["New"] * 3)
        self.assertIn("Scanned: 3 sample(s) into 1 folder(s)", self.app.status.cget("text"))
        self.assertFalse(self.app.convert_btn.instate(["disabled"]))
        time.sleep(0.5)
        self.root.update()
        self.assertEqual(self.outputs(), [])  # nothing written before confirmation

        self.app.convert_btn.invoke()
        self.assertTrue(self.app.dest_btn.instate(["disabled"]))
        self.pump(lambda: self.app.done == 3 and not self.app.runner.running)
        self.pump(lambda: self.app.cancel_btn.instate(["disabled"]))
        self.assertEqual(len(self.outputs()), 3)
        self.assertEqual(self.states(), ["Converted"] * 3)
        self.assertTrue(self.app.convert_btn.instate(["disabled"]))
        self.assertFalse(self.app.dest_btn.instate(["disabled"]))

        # A second scan of the same bank announces the replacement
        self.app.add([self.exb])
        self.pump(lambda: self.app.pending)
        self.assertEqual(self.states(), ["Replace"] * 3)
        self.assertIn("3 existing file(s) will be replaced", self.app.status.cget("text"))

    def test_scan_shows_progress(self):
        archive = shutil.make_archive(os.path.join(self.tmp, "My Bank.exb"), "zip", self.tmp, "My Bank.exb")
        texts = []

        def done() -> bool:
            texts.append(self.app.status.cget("text"))
            return bool(self.app.pending)

        self.app.add([archive])
        self.assertTrue(self.app.status.cget("text").startswith("Scanning"))  # feedback right away
        self.pump(done)
        self.assertTrue(any(t.startswith("Scanning") for t in texts))
        self.assertIn("Scanned: 3 sample(s)", self.app.status.cget("text"))
        self.assertIsNone(self.app.scan)

    def test_format_change_rescans(self):
        self.app.add([self.exb])
        self.pump(lambda: self.app.pending)
        self.app.format.set("flac")
        self.app._format_changed()
        self.pump(lambda: self.app.pending and not self.app._collecting)
        self.assertTrue(all(dst.endswith(".flac") for dst in self.app.pending))
        self.assertIn("not kept in FLAC", self.app.status.cget("text"))

    def test_clear_forgets_the_scan(self):
        self.app.add([self.exb])
        self.pump(lambda: self.app.pending)
        self.app.clear_btn.invoke()
        self.assertEqual(self.app.pending, {})
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertTrue(self.app.convert_btn.instate(["disabled"]))
        self.app._convert()  # nothing to do
        self.root.update()
        self.assertEqual(self.outputs(), [])


if __name__ == "__main__":
    unittest.main()
