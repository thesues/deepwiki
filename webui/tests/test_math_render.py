"""Exercise the shipped Markdown and KaTeX libraries together."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_math_render():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    subprocess.run(
        [node, str(Path(__file__).parent / "js" / "math_render.mjs")],
        check=True, capture_output=True, text=True, timeout=30,
    )
