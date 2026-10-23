import json
from pathlib import Path

import pytest

import dashboard_builder as builder


def summary():
    return {
        "schema_version": 1,
        "meta": {"notes": []},
        "days": [],
        "workouts": [{"date": "2026-10-20", "type": '</script><script>alert("test")</script>&'}],
    }


def test_json_embedding_cannot_end_script_element():
    data = summary()
    document = builder.render_html(data)
    section = document.split('<script id="dashboard-data" type="application/json">', 1)[1]
    encoded = section.split("</script>", 1)[0]
    assert "</script>" not in encoded
    assert "\\u003c/script\\u003e" in encoded
    assert json.loads(encoded) == data
    assert document.count('<script>alert("test")</script>') == 0


def test_generated_file_is_complete_and_replaced_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "demo_summary", summary)
    path = tmp_path / "portable.html"
    path.write_text("previous file", encoding="utf-8")
    builder.generate(path, demo=True)
    assert path.read_text(encoding="utf-8").endswith("</html>\n")
    assert list(tmp_path.iterdir()) == [path]
    assert '"synthetic":true' in path.read_text(encoding="utf-8")


def test_failure_preserves_previous_dashboard(tmp_path, monkeypatch):
    path = tmp_path / "portable.html"
    path.write_text("previous file", encoding="utf-8")
    monkeypatch.setattr(builder, "build_summary", lambda *_: (_ for _ in ()).throw(ValueError()))
    with pytest.raises(ValueError):
        builder.generate(path, db=Path("synthetic.db"))
    assert path.read_text(encoding="utf-8") == "previous file"


def test_rejects_nonfinite_numbers():
    data = summary()
    data["days"] = [{"steps": float("nan")}]
    with pytest.raises(ValueError):
        builder.render_html(data)


def test_embedding_preserves_literal_template_markers_in_user_text():
    data = summary()
    data["workouts"][0]["type"] = "__IMPORT_WORKER__ __DETAIL_SCRIPT__ __DETAIL_STYLES__"
    document = builder.render_html(data)
    encoded = document.split('<script id="dashboard-data" type="application/json">', 1)[1]
    assert json.loads(encoded.split("</script>", 1)[0]) == data
