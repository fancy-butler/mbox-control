"""Test setup: a fake Mbox (real control dump, fake amixer) and an offscreen Qt app.

Everything runs without hardware, PipeWire or a display:  pytest -q
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
_TMP = Path(tempfile.mkdtemp(prefix="mbox-tests-"))

# Must be set before mbox_control is imported: it reads them at import time.
os.environ.update(
    QT_QPA_PLATFORM="offscreen",
    HOME=str(_TMP / "home"),
    XDG_CONFIG_HOME=str(_TMP / "config"),
    MBOX_AMIXER=str(HERE / "fake_amixer.py"),
    MBOX_ALSACTL="nonexistent-alsactl",
    FAKE_DUMP=str(HERE / "fixtures" / "mbox-pro3-controls.txt"),
    FAKE_STATE=str(_TMP / "state.json"),
    FAKE_LOG=str(_TMP / "amixer.log"),
)
(_TMP / "home").mkdir(parents=True, exist_ok=True)
os.chmod(HERE / "fake_amixer.py", 0o755)
sys.path.insert(0, str(ROOT))

import mbox_control as mc  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402


@pytest.fixture(scope="session")
def app():
    a = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    mc.apply_theme(a)
    return a


def spin(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        QtCore.QThread.msleep(15)


@pytest.fixture
def spinner(app):
    return lambda s=0.2: spin(app, s)


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    """A fresh main window on a fresh fake Mbox, with no saved settings."""
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "amixer.log"))
    shutil.rmtree(mc.CONF_DIR, ignore_errors=True)
    shutil.rmtree(mc.PW_CONF_DIR, ignore_errors=True)
    monkeypatch.setattr(mc.MainWindow, "run_setup_wizard", lambda self: None)
    w = mc.MainWindow(mc.Backend(2))
    w.resize(1400, 950)
    w.show()
    spin(app, 0.6)
    yield w
    if getattr(w, "remote", None) and w.remote.running():
        w.stop_remote()
    w.quitting = True
    w.close()
    spin(app, 0.2)
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


@pytest.fixture
def amixer_log(tmp_path):
    def read():
        p = tmp_path / "amixer.log"
        return p.read_text().splitlines() if p.exists() else []
    return read
