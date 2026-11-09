"""Render the app's own icon to an .ico for the Windows exe and installer."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from PySide6 import QtWidgets  # noqa: E402

import mbox_control  # noqa: E402

app = QtWidgets.QApplication([])
pm = mbox_control.app_icon().pixmap(256, 256)
out = sys.argv[1] if len(sys.argv) > 1 else "mbox-control.ico"
if not pm.save(out, "ICO"):
    pm.save(out.replace(".ico", ".png"), "PNG")
    sys.exit(f"couldn't write {out}")
print("wrote", out)
