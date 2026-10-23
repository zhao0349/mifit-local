"""Synthetic-only integration checks for offline category detail dialogs."""

import csv
import io
import json
import re
from pathlib import Path

import pytest
from dashboard_audit import AUDIT_JS
from playwright.sync_api import expect
from test_dashboard_browser import browser as browser  # noqa: PLC0414
from test_dashboard_browser import open_page as open_page  # noqa: PLC0414
from test_dashboard_browser import raw_import_file as raw_import_file  # noqa: PLC0414
from test_dashboard_browser import render_summary as render_summary  # noqa: PLC0414
from test_dashboard_browser import tiny_summary as tiny_summary  # noqa: PLC0414

CATEGORIES = {
    "activity": ("steps", "distance_km", "active_kcal", "active_minutes"),
    "sleep": ("sleep_minutes", "sleep_score", "nap_minutes"),
    "heart": ("resting_hr", "heart_rate_avg", "heart_rate_min", "heart_rate_max"),
    "body": ("weight_kg", "body_fat_pct", "spo2_avg", "stress_avg"),
    "workouts": ("workout_count", "workout_minutes", "workout_distance_km", "workout_kcal"),
}
DETAIL_AUDIT_JS = AUDIT_JS.replace(
    "document.body.querySelectorAll('*')",
    "document.getElementById('detail-dialog').querySelectorAll('*')",
)


@pytest.fixture
def detail_summary(tiny_summary):
    """Preserve missing values while giving every supported metric real samples."""
    for index in (0, 2):
        row = tiny_summary["days"][index]
        row["active_minutes"] = 20 if index == 0 else 60
        row["nap_minutes"] = 0 if index == 0 else 30
        row["heart_rate_min"] = 50 if index == 0 else 60
        row["heart_rate_max"] = 110 if index == 0 else 130
        row["body_fat_pct"] = 20 if index == 0 else 18
    return tiny_summary


def open_detail(page, category="activity", metric=None, *, via_api=False):
    if via_api:
        page.evaluate(
            "([category,metric]) => window.MiFitDetails.open(category,metric)",
            [category, metric],
        )
    else:
        selector = f'[data-detail-open="{category}"]'
        if metric is not None:
            selector += f'[data-detail-metric="{metric}"]'
        trigger = page.locator(selector).filter(visible=True).first
        trigger.click()
    dialog = page.locator("#detail-dialog")
    expect(dialog).to_be_visible()
    return dialog


def detail_range(page, start, end):
    page.locator("#detail-start").fill(start)
    page.locator("#detail-end").fill(end)
    page.locator("#detail-apply-range").click()


def stat(page, key):
    return page.locator(f'#detail-dialog [data-detail-stat="{key}"]')


def assert_stat_number(page, key, expected):
    value = stat(page, key).locator("strong")
    raw = value.get_attribute("data-value")
    assert raw is not None and raw != "", (key, value.inner_text())
    assert float(raw) == pytest.approx(expected), (key, raw, value.inner_text())


def close_detail(page):
    page.locator("#detail-close").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()


@pytest.mark.parametrize("category", tuple(CATEGORIES))
def test_every_category_and_metric_can_open_without_changing_overview(
    open_page, render_summary, detail_summary, category
):
    page = open_page(render_summary(detail_summary))
    previous_steps = page.locator("#kpi-steps").inner_text()
    initial_range = (
        page.locator("#range-start").input_value(),
        page.locator("#range-end").input_value(),
    )
    open_detail(page, category, via_api=True)
    means = {
        "steps": 5000, "distance_km": 5, "active_kcal": 500, "active_minutes": 40,
        "sleep_minutes": 420, "sleep_score": 90, "nap_minutes": 15,
        "resting_hr": 70, "heart_rate_avg": 80, "heart_rate_min": 55,
        "heart_rate_max": 120, "weight_kg": 71, "body_fat_pct": 19,
        "spo2_avg": 98, "stress_avg": 30, "workout_count": 14 / 3,
        "workout_minutes": 115.5, "workout_distance_km": 5.25, "workout_kcal": 745.5,
    }
    for metric in CATEGORIES[category]:
        page.locator(f'#detail-dialog [data-detail-metric="{metric}"]').click()
        expect(page.locator("#detail-dialog")).to_be_visible()
        assert stat(page, "coverage").is_visible()
        assert_stat_number(page, "mean", means[metric])
    close_detail(page)
    expect(page.locator("#kpi-steps")).to_have_text(previous_steps)
    assert (
        page.locator("#range-start").input_value(),
        page.locator("#range-end").input_value(),
    ) == initial_range


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_kpi_keyboard_open_escape_and_focus_restore(
    open_page, render_summary, detail_summary, key
):
    page = open_page(render_summary(detail_summary))
    trigger = page.locator('[data-detail-open="activity"][data-detail-metric="steps"]').first
    trigger.scroll_into_view_if_needed()
    trigger.focus()
    before_scroll = page.evaluate("scrollY")
    page.keyboard.press(key)
    expect(page.locator("#detail-dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#detail-dialog")).not_to_be_visible()
    expect(trigger).to_be_focused()
    assert abs(page.evaluate("scrollY") - before_scroll) < 3


def test_existing_chart_filter_and_pagination_controls_do_not_open_details(
    open_page, render_summary, detail_summary
):
    page = open_page(render_summary(detail_summary))
    page.get_by_test_id("chart-tab-heart_rate_avg").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()
    page.locator("#day-slider").focus()
    page.keyboard.press("Home")
    expect(page.locator("#detail-dialog")).not_to_be_visible()
    page.get_by_test_id("workout-filter").select_option("running")
    page.get_by_test_id("workout-next").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()
    page.get_by_test_id("range-start").fill("2026-01-03")
    page.get_by_test_id("range-end").fill("2026-01-03")
    page.get_by_test_id("range-apply").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()


def test_activity_statistics_and_local_range_are_independent_of_overview(
    open_page, render_summary, detail_summary
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    for key, value in (("mean", 5000), ("total", 10000), ("min", 1000), ("max", 9000)):
        assert_stat_number(page, key, value)
    assert page.locator("#detail-day-steps strong").get_attribute("data-value") == "9000"
    expect(stat(page, "coverage")).to_contain_text(re.compile(r"2\s*(?:天)?\s*/\s*3"))
    detail_range(page, "2026-01-01", "2026-01-01")
    assert_stat_number(page, "mean", 1000)
    assert_stat_number(page, "total", 1000)
    close_detail(page)
    expect(page.locator("#kpi-steps")).to_have_text("5,000步")
    expect(page.locator("#range-start")).to_have_value("2026-01-01")
    expect(page.locator("#range-end")).to_have_value("2026-01-03")


def test_details_distinguish_real_zero_from_missing(open_page, render_summary, detail_summary):
    detail_summary["days"][0]["steps"] = 0
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    assert_stat_number(page, "mean", 4500)
    assert_stat_number(page, "min", 0)
    detail_range(page, "2026-01-02", "2026-01-02")
    for key in ("mean", "total", "min", "max"):
        expect(stat(page, key)).to_contain_text("—")
    expect(stat(page, "coverage")).to_contain_text(re.compile(r"0\s*(?:天)?\s*/\s*1"))


@pytest.mark.parametrize(
    "category,expected_column",
    [
        ("activity", "步数"), ("sleep", "主睡眠分钟"), ("heart", "静息心率"),
        ("body", "体重(kg)"), ("workouts", "运动类型"),
    ],
)
def test_category_csv_has_current_scope_and_expected_columns(
    open_page, render_summary, detail_summary, tmp_path, category, expected_column
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, category, via_api=True)
    with page.expect_download() as pending:
        page.locator("#detail-export-csv").click()
    saved = tmp_path / f"synthetic-details-{category}.csv"
    pending.value.save_as(saved)
    reader = csv.DictReader(io.StringIO(saved.read_text(encoding="utf-8-sig")))
    rows = list(reader)
    assert expected_column in reader.fieldnames
    assert len(rows) == (14 if category == "workouts" else 3)
    assert {row["日期"] for row in rows} <= {"2026-01-01", "2026-01-02", "2026-01-03"}
    if category == "activity":
        by_date = {row["日期"]: row for row in rows}
        assert float(by_date["2026-01-01"]["步数"]) == 1000
        assert by_date["2026-01-02"]["步数"] == ""
        assert float(by_date["2026-01-03"]["步数"]) == 9000
    detail_range(page, "2026-01-03", "2026-01-03")
    with page.expect_download() as filtered:
        page.locator("#detail-export-csv").click()
    filtered.value.save_as(saved)
    rows = list(csv.DictReader(io.StringIO(saved.read_text(encoding="utf-8-sig"))))
    assert len(rows) == (7 if category == "workouts" else 1)
    assert {row["日期"] for row in rows} == {"2026-01-03"}


def test_workout_type_filter_changes_statistics_records_and_export(
    open_page, render_summary, detail_summary, tmp_path
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "workouts", "workout_count")
    assert_stat_number(page, "total", 14)
    expect(page.locator("[data-detail-workout-index]")).to_have_count(10)
    page.locator("#detail-record-next").click()
    expect(page.locator("[data-detail-workout-index]")).to_have_count(4)
    page.locator("#detail-workout-type").select_option("running")
    assert_stat_number(page, "total", 9)
    expect(page.locator("[data-detail-workout-index]")).to_have_count(9)
    expect(page.locator("#detail-record-prev")).to_be_disabled()
    expect(page.locator("#detail-record-next")).to_be_disabled()
    with page.expect_download() as pending:
        page.locator("#detail-export-csv").click()
    saved = tmp_path / "synthetic-filtered-workouts.csv"
    pending.value.save_as(saved)
    rows = list(csv.DictReader(io.StringIO(saved.read_text(encoding="utf-8-sig"))))
    assert len(rows) == 9
    assert {row["运动类型"] for row in rows} <= {"running", "跑步"}
    page.locator("#detail-workout-type").select_option("cycling")
    assert_stat_number(page, "total", 5)
    expect(page.locator("[data-detail-workout-index]")).to_have_count(5)


def test_workout_csv_treats_formula_like_type_as_literal_text(
    open_page, render_summary, detail_summary, tmp_path
):
    formula = '=HYPERLINK("https://example.invalid","SYNTHETIC_ONLY")'
    detail_summary["workouts"] = [{**detail_summary["workouts"][0], "type": formula}]
    page = open_page(render_summary(detail_summary))
    open_detail(page, "workouts", via_api=True)
    with page.expect_download() as pending:
        page.locator("#detail-export-csv").click()
    saved = tmp_path / "synthetic-formula-type.csv"
    pending.value.save_as(saved)
    rows = list(csv.DictReader(io.StringIO(saved.read_text(encoding="utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["运动类型"] == "'" + formula


def test_imported_data_is_used_when_details_are_opened_again(
    open_page, render_summary, detail_summary, raw_import_file
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    assert_stat_number(page, "mean", 5000)
    close_detail(page)
    page.get_by_test_id("import-file").set_input_files(raw_import_file)
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    open_detail(page, "activity", "steps")
    assert_stat_number(page, "mean", 1000)
    expect(page.locator("#detail-end")).to_have_value("2026-01-02")
    close_detail(page)
    open_detail(page, "heart", "resting_hr")
    assert_stat_number(page, "mean", 60)


def test_saving_with_dialog_open_reopens_closed_and_binds_one_entry_handler(
    open_page, render_summary, detail_summary, tmp_path
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    with page.expect_download() as pending:
        page.evaluate("document.getElementById('download-html').click()")
    saved = tmp_path / "synthetic-details-saved.html"
    pending.value.save_as(saved)
    reopened = open_page(saved)
    expect(reopened.locator("#detail-dialog")).not_to_be_visible()
    embedded = json.loads(reopened.locator("#dashboard-data").text_content())
    assert [row["date"] for row in embedded["days"]] == [
        "2026-01-01", "2026-01-02", "2026-01-03",
    ]
    reopened.evaluate("""() => {
      window.__syntheticDetailOpens=0;
      const original=window.MiFitDetails.open;
      window.MiFitDetails.open=function(...args){
        window.__syntheticDetailOpens++;return original.apply(this,args);
      };
    }""")
    open_detail(reopened, "activity", "steps")
    assert reopened.evaluate("window.__syntheticDetailOpens") == 1
    expect(reopened.locator("#detail-dialog")).to_have_count(1)
    close_detail(reopened)
    open_detail(reopened, "activity", "steps")
    assert reopened.evaluate("window.__syntheticDetailOpens") == 2


@pytest.mark.parametrize("width,height", [(320, 740), (390, 844), (1440, 960)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_all_detail_categories_fit_viewport_and_meet_text_contrast(
    open_page, render_summary, detail_summary, width, height, theme
):
    page = open_page(render_summary(detail_summary), width=width, height=height, theme=theme)
    for category in CATEGORIES:
        dialog = open_detail(page, category, via_api=True)
        box = dialog.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (category, box)
        assert dialog.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), category
        audit = page.evaluate(DETAIL_AUDIT_JS)
        assert not audit["contrasts"], (category, audit["contrasts"])
        assert not audit["overflow"], (category, audit["overflow"])
        close_detail(page)


@pytest.mark.parametrize(
    "selector,category,metric",
    [
        ('#activity .panel-title', "activity", "steps"),
        ('[data-detail-card="activity"] .panel-title', "activity", "steps"),
        ('[data-detail-card="sleep"] .panel-title', "sleep", "sleep_minutes"),
        ('[data-detail-card="body"] .panel-title', "body", "weight_kg"),
        ('[data-detail-card="workouts"] .panel-title', "workouts", "workout_count"),
    ],
)
def test_overview_panel_content_opens_its_detail(
    open_page, render_summary, detail_summary, selector, category, metric
):
    page = open_page(render_summary(detail_summary))
    candidates = page.locator(selector)
    for index in range(candidates.count()):
        candidates.nth(index).click()
        expect(page.locator("#detail-dialog")).to_have_attribute("data-category", category)
        expect(page.locator(f"#detail-metric-{metric}")).to_have_attribute("aria-selected", "true")
        close_detail(page)


@pytest.mark.parametrize("metric", CATEGORIES["body"])
@pytest.mark.parametrize("width", [390, 1440])
def test_each_body_tile_opens_its_own_metric(
    open_page, render_summary, detail_summary, metric, width
):
    page = open_page(render_summary(detail_summary), width=width)
    page.locator(f'#body-metrics [data-detail-metric="{metric}"]').click()
    expect(page.locator("#detail-dialog")).to_have_attribute("data-category", "body")
    expect(page.locator(f"#detail-metric-{metric}")).to_have_attribute("aria-selected", "true")
    assert_stat_number(page, "latest", {
        "weight_kg": 72, "body_fat_pct": 18, "spo2_avg": 98, "stress_avg": 40,
    }[metric])


@pytest.mark.parametrize("category,metric,total", [
    ("activity", "steps", "10,000"), ("sleep", "sleep_minutes", "420"),
])
def test_chart_day_week_month_preserves_daily_statistics_and_uses_correct_aggregation(
    open_page, render_summary, detail_summary, category, metric, total
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, category, metric)
    daily_mean = stat(page, "mean").locator("strong").get_attribute("data-value")
    expect(page.locator("#detail-chart-svg .detail-point")).to_have_count(2)
    for grain in ("week", "month"):
        page.locator("#detail-granularity").select_option(grain)
        expect(page.locator("#detail-chart-svg .detail-point")).to_have_count(1)
        expect(page.locator("#detail-chart-svg .detail-point title")).to_contain_text(total)
        assert stat(page, "mean").locator("strong").get_attribute("data-value") == daily_mean
    page.locator("#detail-granularity").select_option("day")
    expect(page.locator("#detail-chart-svg .detail-point")).to_have_count(2)


def test_selected_day_can_be_changed_by_date_chart_keyboard_and_record(
    open_page, render_summary, detail_summary
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    page.locator("#detail-date").fill("2026-01-01")
    page.locator("#detail-date").dispatch_event("change")
    expect(page.locator("#detail-day-steps strong")).to_have_attribute("data-value", "1000")
    page.locator("#detail-chart-svg").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#detail-date")).to_have_value("2026-01-02")
    expect(page.locator("#detail-day-steps strong")).to_have_attribute("data-value", "")
    page.keyboard.press("End")
    expect(page.locator("#detail-date")).to_have_value("2026-01-03")
    expect(page.locator("#detail-day-steps strong")).to_have_attribute("data-value", "9000")
    page.locator('[data-detail-row-date="2026-01-01"]').click()
    expect(page.locator("#detail-date")).to_have_value("2026-01-01")
    expect(page.locator("#detail-day-steps strong")).to_have_attribute("data-value", "1000")


@pytest.mark.parametrize("width", [390, 1440])
def test_single_workout_opens_from_overview_and_from_detail_record(
    open_page, render_summary, detail_summary, width
):
    page = open_page(render_summary(detail_summary), width=width)
    page.locator("[data-workout-detail]").filter(visible=True).first.click()
    expect(page.locator("#detail-dialog")).to_have_attribute("data-category", "workouts")
    single = page.locator("#detail-single-workout")
    expect(single).to_be_visible()
    for metric, value in {
        "duration_minutes": "23", "distance_km": "1.4",
        "calories_kcal": "113", "avg_heart_rate_bpm": "123",
    }.items():
        expect(single.locator(f'[data-detail-workout-metric="{metric}"] strong')).to_have_attribute(
            "data-value", value
        )
    expect(single.locator('[data-detail-workout-metric="avg_pace_sec_per_km"]')).to_have_count(0)
    page.locator("#detail-workout-close").click()
    expect(single).to_have_count(0)
    page.locator('[data-detail-workout-index="0"]').click()
    expect(single).to_be_visible()
    expect(single).to_be_in_viewport()
    expect(page.locator("#detail-date")).to_have_value("2026-01-03")
    page.locator("#detail-date").fill("2026-01-01")
    page.locator("#detail-date").dispatch_event("change")
    expect(single).to_have_count(0)


def test_open_detail_follows_system_theme_without_losing_selection(
    open_page, render_summary, detail_summary
):
    page = open_page(render_summary(detail_summary), theme="light")
    open_detail(page, "sleep", "sleep_score")
    page.locator("#detail-granularity").select_option("week")
    page.locator("#detail-date").fill("2026-01-01")
    page.locator("#detail-date").dispatch_event("change")
    initial = page.locator("#detail-dialog").evaluate("(el)=>getComputedStyle(el).backgroundColor")
    for theme in ("dark", "light"):
        page.emulate_media(color_scheme=theme)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(page.locator("#detail-metric-sleep_score")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#detail-granularity")).to_have_value("week")
        expect(page.locator("#detail-date")).to_have_value("2026-01-01")
        background = page.locator("#detail-dialog").evaluate("(el)=>getComputedStyle(el).backgroundColor")
        assert (background == initial) is (theme == "light")
        assert not page.evaluate(DETAIL_AUDIT_JS)["contrasts"]


def test_invalid_detail_range_can_be_corrected_without_losing_dialog(
    open_page, render_summary, detail_summary
):
    page = open_page(render_summary(detail_summary))
    open_detail(page, "activity", "steps")
    for start, end in [("2026-01-03", "2026-01-01"), ("1900-01-01", "2026-01-03")]:
        detail_range(page, start, end)
        expect(page.locator("#detail-dialog [role=alert]")).to_be_visible()
        assert_stat_number(page, "total", 10000)
        detail_range(page, "2026-01-01", "2026-01-01")
        expect(page.locator("#detail-dialog [role=alert]")).to_have_count(0)
        assert_stat_number(page, "total", 1000)
        detail_range(page, "2026-01-01", "2026-01-03")


@pytest.mark.parametrize("width,height,theme", [
    (320, 740, "light"), (390, 844, "dark"), (1440, 960, "light"), (1440, 960, "dark"),
])
def test_synthetic_detail_visual_evidence(open_page, render_summary, width, height, theme):
    page = open_page(render_summary(), width=width, height=height, theme=theme)
    output = Path(__file__).resolve().parents[1] / "dashboard" / "qa"
    output.mkdir(parents=True, exist_ok=True)
    for category in CATEGORIES:
        open_detail(page, category, via_api=True)
        page.screenshot(path=str(output / f"synthetic-details-{category}-{width}-{theme}.png"))
        page.locator("#detail-chart").scroll_into_view_if_needed()
        page.screenshot(path=str(output / f"synthetic-details-chart-{category}-{width}-{theme}.png"))
        close_detail(page)
