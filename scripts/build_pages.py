"""Build the public site exclusively from deterministic synthetic records.

No account settings, credentials, exported files or personal databases are read.
The destination must be new or empty so stale private files cannot be deployed.
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard_builder import render_html  # noqa: E402
from dashboard_data import demo_summary  # noqa: E402


def build_site(destination: Path) -> Path:
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Public site destination must be empty; use a fresh directory.")
    summary = demo_summary()
    summary["meta"]["synthetic"] = True
    document = render_html(summary)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "index.html").write_text(document, encoding="utf-8")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, destination / name)
    (destination / ".nojekyll").touch()
    return destination


if __name__ == "__main__":
    print(build_site(ROOT / "_site"))
