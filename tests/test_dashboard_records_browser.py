"""Browser acceptance for source details, with synthetic records only."""

import csv
import io
import json

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import browser as browser  # noqa: PLC0414
from test_dashboard_browser import open_page as open_page  # noqa: PLC0414
from test_dashboard_browser import render_summary as render_summary  # noqa: PLC0414
from test_dashboard_details_browser import open_detail
from test_dashboard_worker import document, run_document


def composition_summary():
    records = []
    for index in range(12):
        records.append({
            "timestamp": f"2026-10-21T08:{index:02}:00+08:00",
            "weight_kg": 70 + index / 10, "bmi": 22.8,
            "body_fat_pct": 20, "muscle_mass_kg": 51.12345,
            "water_pct": 56.5, "bone_mass_kg": 2.7, "visceral_fat_score": 0,
            "basal_metabolism_kcal": 1600, "metabolic_age": 30,
            "user_id": "PRIVATE_SENTINEL",
        })
    return run_document(document({
        "body_measurements": records,
        "workout_gps": [{"timestamp": "2026-10-21T08:00:00+08:00",
                         "latitude": -12.123456789, "longitude": -34.987654321, "altitude_m": -8}],
        "workout_detail": [{"timestamp": "2026-10-21T08:00:00+08:00",
                            "offset_seconds": -2, "cadence": 120, "speed_mps": 2.5}],
        "abnormal_heart_beat": [{"start_at": "invalid", "duration_seconds": 0}],
    }))


@pytest.mark.parametrize("width", [390, 1440])
def test_body_details_preserve_every_measurement_precision_and_pagination(
    open_page, render_summary, tmp_path, width
):
    summary = composition_summary()
    page = open_page(render_summary(summary), width=width)
    open_detail(page, "body", "muscle_mass_kg", via_api=True)
    expect(page.locator("#detail-day-muscle_mass_kg strong")).to_have_attribute("data-value", "51.12345")
    page.locator("#detail-jump-records").click()
    expect(page.locator("#detail-source-count")).to_contain_text("12 / 12")
    expect(page.locator(".detail-source-entry")).to_have_count(10)
    first = page.locator(".detail-source-entry").first
    first.locator("summary").click()
    expect(first.locator('[data-source-field="muscle_mass_kg"] dd')).to_have_text("51.12345")
    expect(first.locator('[data-source-field="visceral_fat_score"] dd')).to_have_text("0")
    assert page.locator("#detail-dialog").evaluate("(el) => el.scrollWidth <= el.clientWidth + 1")
    page.locator("#detail-source-next").click()
    expect(page.locator(".detail-source-entry")).to_have_count(2)
    with page.expect_download() as pending:
        page.locator("#detail-source-export").click()
    path = tmp_path / "synthetic-composition.csv"
    pending.value.save_as(path)
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert len(rows) == 12
    assert rows[0]["肌肉量(kg) [muscle_mass_kg]"] == "51.12345"
    assert "PRIVATE_SENTINEL" not in path.read_text(encoding="utf-8-sig")
    page.locator("#detail-close").click()
    with page.expect_download() as pending:
        page.evaluate("document.getElementById('download-html').click()")
    saved = tmp_path / "synthetic-details-offline.html"
    pending.value.save_as(saved)
    reopened = open_page(saved, width=width)
    open_detail(reopened, "body", via_api=True)
    expect(reopened.locator("#detail-source-count")).to_contain_text("12 / 12")
    payload = json.loads(reopened.locator("#dashboard-data").text_content())
    assert payload["records"] == summary["records"]


def test_gps_workout_samples_invalid_dates_and_literal_text(open_page, render_summary):
    summary = composition_summary()
    summary["records"]["datasets"]["workout_gps"]["fields"].append("collected_at")
    summary["records"]["datasets"]["workout_gps"]["rows"][0].append('<img src=x onerror=alert(1)>')
    page = open_page(render_summary(summary))
    open_detail(page, "workouts", via_api=True)
    page.locator("#detail-source-select").select_option("workout_gps")
    page.locator(".detail-source-entry summary").click()
    expect(page.locator('[data-source-field="latitude"] dd')).to_have_text("-12.123456789")
    expect(page.locator('[data-source-field="altitude_m"] dd')).to_have_text("-8")
    expect(page.locator('[data-source-field="collected_at"] dd')).to_have_text('<img src=x onerror=alert(1)>')
    expect(page.locator("#detail-source-records img")).to_have_count(0)
    page.locator("#detail-source-select").select_option("workout_detail")
    page.locator(".detail-source-entry summary").click()
    expect(page.locator('[data-source-field="offset_seconds"] dd')).to_have_text("-2")
    page.locator("#detail-close").click()
    open_detail(page, "heart", via_api=True)
    page.locator("#detail-source-select").select_option("abnormal_heart_beat")
    expect(page.locator(".detail-source-entry")).to_have_count(0)
    page.locator("#detail-source-invalid").check()
    expect(page.locator(".detail-source-entry")).to_have_count(1)
    page.locator(".detail-source-entry summary").click()
    expect(page.locator('[data-source-field="duration_seconds"] dd')).to_have_text("0")
