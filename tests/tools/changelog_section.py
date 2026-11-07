"""Print one version's section of CHANGELOG.md (used for GitHub release notes)."""
import re
import sys
from pathlib import Path

ver = sys.argv[1]
text = Path(__file__).resolve().parents[2].joinpath("CHANGELOG.md").read_text()
m = re.search(rf"^## {re.escape(ver)}[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
if not m:
    sys.exit(f"CHANGELOG.md has no section for {ver}")
print(m[1].strip())
