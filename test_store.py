"""Store entitlement and tray session lifecycle regressions."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from store_license import StoreLicense

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from app import HubWorker, MainWindow
    from core import read_config, write_config
    from store_launcher import StoreWindow
except ImportError:
    if os.environ.get("LYSNAV_REQUIRE_QT_TESTS") == "1":
        raise
    QApplication = None


class StoreLicenseTests(TestCase):
    def test_trial_and_paid_licenses(self):
        now = datetime.now(timezone.utc)
        self.assertFalse(StoreLicense(False).allows_access())
        self.assertTrue(StoreLicense(True).allows_access())
        self.assertTrue(StoreLicense(True, now + timedelta(days=7)).allows_access())
        self.assertFalse(StoreLicense(True, now - timedelta(seconds=1)).allows_access())
        self.assertFalse(StoreLicense(False, now + timedelta(days=7)).allows_access())


@skipUnless(QApplication is not None, "Qt is required")
class StoreWindowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.settings = directory / "package" / "config.json"
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": str(directory)}))
        self.enterContext(patch("app.QSystemTrayIcon.show"))
        self.enterContext(patch("app.QSystemTrayIcon.showMessage"))
        self.enterContext(patch("app.TrayWheelHook.start"))
        self.enterContext(patch.object(StoreWindow, "_check_license"))
        self.begin_session = self.enterContext(patch.object(MainWindow, "begin_hub_session"))

    def create_window(self):
        window = StoreWindow(settings_file=self.settings)
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete)
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.quit_application)
        return window

    def test_requires_license_at_launch_and_pauses_already_running_tray(self):
        window = self.create_window()
        self.begin_session.assert_not_called()
        self.assertFalse(window.centralWidget().isEnabled())

        trial = StoreLicense(True, datetime.now(timezone.utc) + timedelta(days=7))
        window._on_license_checked(trial)
        self.assertTrue(window.centralWidget().isEnabled())
        self.begin_session.assert_called_once()
        self.assertIn("trial ends", window._license_message.text())

        window._on_license_checked(None)
        self.assertTrue(window.centralWidget().isEnabled())
        self.begin_session.assert_called_once()

        worker = MagicMock()
        worker.isFinished.return_value = False
        window.worker = worker
        window._slider_wheel_pending[("light",)] = 50
        window._on_license_checked(StoreLicense(False, trial.trial_until))
        worker.requestInterruption.assert_called_once()
        worker.stop.assert_called_once()
        self.assertIsNone(window.worker)
        self.assertFalse(window.centralWidget().isEnabled())
        self.assertEqual(window._slider_wheel_pending, {})
        self.assertIn("Hub controls are paused", window._license_message.text())

        window._on_license_checked(StoreLicense(True))
        self.begin_session.assert_called_once()
        worker.isRunning.return_value = False
        window._worker_stopped()
        self.assertEqual(self.begin_session.call_count, 2)
        self.assertTrue(window.centralWidget().isEnabled())

    def test_imports_an_existing_installation_once(self):
        original = dict(ip="192.168.1.20", token="sample", approved={"lamp": "room"},
                        approved_sensors={}, tray_room="room", tray_icon_path="")
        write_config(original)
        self.assertEqual(self.create_window().config, original)
        self.assertEqual(read_config(self.settings), original)
        write_config({**original, "token": "new standalone token"})
        self.assertEqual(self.create_window().config, original)

    def test_stopped_access_drops_already_queued_commands(self):
        access = MagicMock(side_effect=[True, False])
        worker = HubWorker("192.168.1.20", "sample", {"lamp": "room"}, can_access=access)
        worker.submit("control", ["lamp"], "power", True)
        with patch("dirigera.Hub") as hub, patch("app.apply") as apply:
            worker.run()
        hub.assert_called_once()
        apply.assert_not_called()
