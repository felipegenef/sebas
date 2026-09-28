"""The CWD is sacred: no test artifact may ever land in it.

Two suite paths wrote through code that, under a Windows platform signal on
a POSIX host, resolved into the working directory itself:

  * the installing-state fixture wraps venv_python's PURE path for host
    writes (`Path(str(...))`); a Windows-flavour pure path wraps to a
    CWD-RELATIVE name, so every write materialized a file literally NAMED
    with backslashes — and an embedded machine path — in the CWD;
  * the Windows card's capability-token store falls back to the OS temp dir
    (`plugin/notify/windows.py::_token_dir`); when the temp base IS the CWD
    — the windows-latest condition the simulation drivers reproduce — the
    store directory appeared there.

The guard runs that artifact-producing code in a FRESH interpreter (no
ambient simulation can leak in: the windows simulation drivers monkeypatch
pathlib/os in-process) with the CWD set to a scratch directory, the temp
base pointed at that same scratch directory (the leaked condition) and the
Windows platform signal pinned (os.name == "nt" with Path construction kept
host-native — the "where safe" carve-out of the simulation drivers, so host
writes stay possible and the leak is observable). The scratch directory
must be EMPTY afterwards: any suite code that writes outside a temporary
directory fails here, on every platform flavour. The guarded tests keep all
of their own assertions; the guard only adds the emptiness check.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))

from _support import mcp_root


@contextlib.contextmanager
def windows_flavour():
    """A faithful Windows-platform simulation: os.name == "nt" (the platform
    predicates take the Windows branch) with pathlib.Path construction kept
    host-native (a WindowsPath cannot be born on a POSIX build). The path
    STRING flavour is pinned to the stock pathlib for the block, whatever an
    ambient simulation does: a real Windows pathlib hands the OS strings it
    knows how to resolve, while backslash strings reach a POSIX kernel as
    literal names — composing with an ambient shim would test the shim, not
    the code under test. Run inside a fresh interpreter (see _inner_main)."""
    original_new = pathlib.Path.__new__

    def host_native(cls, *args, **kwargs):
        return original_new(pathlib.PosixPath, *args, **kwargs)

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(os, "name", "nt"))
        stack.enter_context(mock.patch.object(
            pathlib.Path, "__new__", staticmethod(host_native)))
        stack.enter_context(mock.patch.object(
            pathlib.PosixPath, "__str__", pathlib.PurePath.__str__,
            create=True))
        stack.enter_context(mock.patch.object(
            pathlib.PosixPath, "__fspath__", pathlib.PurePath.__fspath__,
            create=True))
        yield


def _inner_main(argv):
    """Guarded run, one scratch directory: chdir there, make the temp base
    the same directory (the leaked condition), flip the platform signal and
    run the named suites. Reports what happened and what stayed behind as
    one JSON line after a sentinel — the guarded tests may print anything."""
    scratch, names = argv[1], argv[2:]
    os.chdir(scratch)
    tempfile.tempdir = scratch
    with windows_flavour():
        loader = unittest.TestLoader()
        suite = unittest.TestSuite(loader.loadTestsFromName(n) for n in names)
        result = suite.run(unittest.TestResult())
        leftovers = sorted(os.listdir(scratch))
    report = {"failures": [f"{case.id()}\n{tb}" for case, tb in result.failures],
              "errors": [f"{case.id()}\n{tb}" for case, tb in result.errors],
              "leftovers": leftovers}
    print("\nCWD-GUARD-REPORT " + json.dumps(report))


class CwdArtifactGuard(unittest.TestCase):
    def _assert_cwd_stays_empty(self, *guarded):
        with tempfile.TemporaryDirectory() as scratch:
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--inner", scratch, *guarded],
                capture_output=True, text=True, timeout=120)
        marker = "CWD-GUARD-REPORT "
        line = next((l for l in reversed(proc.stdout.splitlines())
                     if l.startswith(marker)), None)
        self.assertIsNotNone(
            line, f"guarded run reported nothing (rc={proc.returncode})\n"
                  f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        report = json.loads(line[len(marker):])
        self.assertEqual(report["failures"], [],
                         "guarded tests failed while the guard ran them")
        self.assertEqual(report["errors"], [],
                         "guarded tests errored while the guard ran them")
        self.assertEqual(
            report["leftovers"], [],
            f"guarded tests left artifacts in the CWD: {report['leftovers']!r}"
            " — test artifacts belong in a TemporaryDirectory, never in the "
            "CWD")

    def test_installing_state_fixture_leaves_the_cwd_untouched(self):
        if mcp_root() is None:
            self.skipTest("set SEBAS_MCP_ROOT to the voice MCP checkout")
        self._assert_cwd_stays_empty("test_installing_state")

    def test_windows_card_and_adapter_tests_leave_the_cwd_untouched(self):
        self._assert_cwd_stays_empty("test_adapters")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--inner":
        _inner_main(sys.argv[1:])
    else:
        unittest.main()
