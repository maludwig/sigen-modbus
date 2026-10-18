"""The pages' JavaScript (src/sigen/static): the tests in tests/js run in Node, one pytest test per file; skipped
without Node."""

import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = sorted((Path(__file__).parent / "js").glob("*.test.js"))


@pytest.mark.parametrize("tests", TESTS, ids=[t.name for t in TESTS])
def test_js(tests):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed (the pages themselves don't need it)")
    r = subprocess.run([node, "--test", str(tests)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
