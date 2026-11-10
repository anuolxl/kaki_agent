"""Create a disposable single-Git fixture without touching an existing directory."""

import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/private/tmp/kaki-e2e-workspace")
if root.exists():
    raise SystemExit("Refusing to overwrite existing directory; choose a new disposable path")
root.mkdir(parents=True)
files = {
    "AGENTS.md": "# Fixture rules\nOnly change pricing.py. Run `python3 -m unittest -v`. Never modify tests. This is a disposable single-Git validation project.\n",
    "pricing.py": "def discounted_price(amount, percent):\n    return round(amount * percent / 100, 2)\n",
    "test_pricing.py": """import unittest
from pricing import discounted_price
class PricingTest(unittest.TestCase):
    def test_ten_percent_discount(self):
        self.assertEqual(discounted_price(100, 10), 90)
    def test_zero_discount(self):
        self.assertEqual(discounted_price(100, 0), 100)
    def test_invalid_percent(self):
        for percent in (-1, 101):
            with self.assertRaises(ValueError):
                discounted_price(100, percent)
""",
}
for name, body in files.items():
    (root / name).write_text(body)
subprocess.run(["git", "init", "-q", str(root)], check=True)
subprocess.run(["git", "-C", str(root), "add", *files], check=True)
subprocess.run(
    [
        "git",
        "-C",
        str(root),
        "-c",
        "user.name=Kaki Validation",
        "-c",
        "user.email=kaki@local",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "test: initial pricing fixture",
    ],
    check=True,
)
print(root)
