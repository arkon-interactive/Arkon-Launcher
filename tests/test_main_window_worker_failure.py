import types
from pathlib import Path

import pytest

from arkon_launcher.ui import main_window


class FakeServer:
    def __init__(self, raise_on_query=False):
        self.raise_on_query = raise_on_query
        self.recent_lines = []
        self.players = set()
        self.is_alive = True

    def query(self, *args, **kwargs):
        if self.raise_on_query:
            raise RuntimeError("Server is not running.")
        return []


class DummyTab:
    def set_groups(self, *a, **k):
        pass

    def set_catalogue(self, *a, **k):
        pass

    def set_tracks(self, *a, **k):
        pass


class DummyPermissionsPanel:
    def __init__(self):
        self.groups_tab = DummyTab()
        self.users_tab = DummyTab()
        self.tracks_tab = DummyTab()


class DummyConsole:
    def __init__(self):
        self.notices = []

    def append_notice(self, text, *args, **kwargs):
        self.notices.append(text)


def make_minimal_window(tmp_path, raise_on_query=False):
    # Create MainWindow instance without running __init__ GUI setup.
    w = main_window.MainWindow.__new__(main_window.MainWindow)
    # minimal attributes used by refresh_permissions
    w.instance = types.SimpleNamespace(directory=tmp_path)
    w.server = FakeServer(raise_on_query=raise_on_query)
    w._help_lines = None
    w._recorded_from = 0
    w._group_cache = []
    w.permissions_panel = DummyPermissionsPanel()
    w.console = DummyConsole()
    # _server_dir and _luckperms_ready control early exits; provide values
    w._server_dir = lambda: tmp_path
    w._luckperms_ready = lambda: True

    # Replace _run to execute work synchronously and call done immediately.
    def _run_sync(work, done):
        # collect report calls
        reports = []

        def report(msg):
            reports.append(msg)

        result = work(report)
        done(result)
        return reports

    w._run = types.MethodType(lambda self, work, done: _run_sync(work, done), w)
    return w


def test_refresh_permissions_handles_runtimeerror(tmp_path):
    w = make_minimal_window(tmp_path, raise_on_query=True)

    # Call the method; our patched _run will execute the work and done.
    w.refresh_permissions()

    # The console should have a notice set by the done callback or the worker's report
    # We expect the worker to have reported an abort message when query raised.
    notices = w.console.notices
    assert any("Server stopped" in n or "Could not reach" in n for n in notices)
