from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_slicer.backends import _find


class BackendDiscoveryTests(unittest.TestCase):
    def test_configured_uvtools_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = root / "configured" / "UVtoolsCmd"
            known = root / "known" / "UVtoolsCmd"
            configured.parent.mkdir()
            known.parent.mkdir()
            configured.write_bytes(b"configured")
            known.write_bytes(b"known")
            with patch.dict(os.environ, {"TEST_UVTOOLS": str(configured)}):
                found = _find(
                    "TEST_UVTOOLS",
                    "not-on-path",
                    str(known),
                    prefer_known=True,
                )
            self.assertEqual(found, str(configured.resolve()))

    def test_verified_known_path_precedes_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            known = Path(temporary) / "UVtoolsCmd"
            known.write_bytes(b"known")
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("agent_slicer.backends.shutil.which", return_value="/usr/bin/UVtoolsCmd"),
            ):
                found = _find(
                    "TEST_UVTOOLS",
                    "UVtoolsCmd",
                    str(known),
                    prefer_known=True,
                )
            self.assertEqual(found, str(known.resolve()))


if __name__ == "__main__":
    unittest.main()
