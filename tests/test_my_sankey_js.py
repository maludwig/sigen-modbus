"""The daily page's Sankey (src/sigen/static/my_sankey.js): its layout tests run in Node (tests/js), skipped without
Node."""
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent / "js" / "my_sankey.test.js"


def test_my_sankey_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed (the page itself doesn't need it)")
    r = subprocess.run([node, "--test", str(TESTS)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
