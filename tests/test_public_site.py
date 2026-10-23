"""Check the publication boundary without reading personal files."""

import json

import pytest

import dashboard_builder
import dashboard_data
from scripts.build_pages import build_site


def test_public_build_never_resolves_or_reads_personal_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Public builds must not access personal storage")

    for module in (dashboard_builder, dashboard_data):
        monkeypatch.setattr(module, "resolve_current_database", forbidden)
        monkeypatch.setattr(module, "build_summary", forbidden)
    output = build_site(tmp_path / "site")
    assert {p.name for p in output.iterdir()} == {
        "index.html", "LICENSE", "THIRD_PARTY_NOTICES.md", ".nojekyll"
    }
    html = (output / "index.html").read_text(encoding="utf-8")
    encoded = html.split('<script id="dashboard-data" type="application/json">', 1)[1]
    payload = json.loads(encoded.split("</script>", 1)[0])
    expected = dashboard_data.demo_summary()
    expected["meta"]["synthetic"] = True
    assert payload == expected
    assert "https://github.com/zhao0349/mifit-local" in html


def test_public_build_rejects_directory_with_existing_files(tmp_path):
    private = tmp_path / "private.html"
    private.write_text("synthetic sentinel", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        build_site(tmp_path)
    assert private.read_text(encoding="utf-8") == "synthetic sentinel"
    assert not (tmp_path / "index.html").exists()
