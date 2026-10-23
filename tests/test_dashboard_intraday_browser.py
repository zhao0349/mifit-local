"""Offline, synthetic-only checks for heart samples and approximate sleep sequences."""

import copy
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from dashboard_audit import AUDIT_JS
from playwright.sync_api import expect
from test_dashboard_browser import browser as browser  # noqa: PLC0414
from test_dashboard_browser import open_page as open_page  # noqa: PLC0414
from test_dashboard_browser import raw_import_file as raw_import_file  # noqa: PLC0414
from test_dashboard_browser import render_summary as render_summary  # noqa: PLC0414
from test_dashboard_browser import tiny_summary as tiny_summary  # noqa: PLC0414

HOUR = 3_600_000
INTRADAY_AUDIT_JS = AUDIT_JS.replace(
    "document.body.querySelectorAll('*')",
    "document.getElementById('intraday-panel').querySelectorAll('*')",
)


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


@pytest.fixture
def intraday_summary(tiny_summary):
    """All dates, records, samples and identifiers in this module are invented."""
    tiny_summary["intraday"] = {
        "version": 1,
        "heart": {
            "2026-01-01": [[0, 62, 1], [HOUR, 70, 0], [12 * HOUR, 90, 0], [24 * HOUR - 1, 68, 0]],
            "2026-01-03": [[8 * HOUR, 66, 1], [8.5 * HOUR, 96, 0], [23 * HOUR, 75, 0]],
        },
        "sleep": {
            "2026-01-01": [{
                "start": epoch("2025-12-31T23:30:00+08:00"),
                "end": epoch("2026-01-01T07:30:00+08:00"),
                "nap": False, "asleep": 420, "score": 90,
                "stages": [["deep", 90], ["light", 270], ["rem", 60], ["awake", 30]],
                "timing": "sequence",
            }],
            "2026-01-03": [{
                "start": epoch("2026-01-02T23:00:00+08:00"),
                "end": epoch("2026-01-03T05:00:00+08:00"),
                "nap": False, "asleep": 340, "score": 80,
                "stages": [["deep", 180], ["light", 240]],
                "timing": "sequence",
            }, {
                "start": epoch("2026-01-03T13:00:00+08:00"),
                "end": epoch("2026-01-03T13:30:00+08:00"),
                "nap": True, "asleep": 30, "score": None,
                "stages": [["light", 30]], "timing": "sequence",
            }],
        },
    }
    return tiny_summary


def open_intraday(page, category, date="2026-01-01"):
    page.evaluate(
        "([category,date])=>window.MiFitDetails.open(category,undefined,date)",
        [category, date],
    )
    expect(page.locator("#detail-dialog")).to_be_visible()
    panel = page.locator(f"#intraday-{category}")
    expect(panel).to_be_visible()
    panel.scroll_into_view_if_needed()
    return panel


def choose_date(page, value):
    page.locator("#intraday-date").fill(value)
    page.locator("#intraday-date").dispatch_event("change")
    expect(page.locator("#intraday-date")).to_have_value(value)


def assert_reading(page, time, value):
    expect(page.locator("#intraday-reading")).to_have_attribute("data-time", str(int(time)))
    expect(page.locator("#intraday-reading")).to_have_attribute("data-value", str(value))


def embedded(page):
    return json.loads(page.locator("#dashboard-data").text_content())


def test_heart_sample_keyboard_controls_return_original_values(
    open_page, render_summary, intraday_summary
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "heart")
    slider = page.locator("#intraday-sample-range")
    slider.focus()
    page.keyboard.press("Home")
    assert_reading(page, 0, 62)
    page.keyboard.press("ArrowRight")
    assert_reading(page, HOUR, 70)
    page.keyboard.press("End")
    assert_reading(page, 24 * HOUR - 1, 68)
    expect(page.locator("#intraday-heart-records")).to_contain_text("23:59")


def test_simultaneous_heart_samples_keep_selected_index_after_resize_and_theme(
    open_page, render_summary, intraday_summary
):
    intraday_summary["intraday"]["heart"] = {
        "2026-01-01": [[0, 60, 0], [0, 70, 0], [HOUR, 80, 0]],
    }
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "heart")
    page.locator("#intraday-sample-range").focus()
    page.keyboard.press("Home")
    page.keyboard.press("ArrowRight")
    assert_reading(page, 0, 70)
    expect(page.locator("#intraday-sample-range")).to_have_value("1")
    page.evaluate("window.__syntheticOriginalChart=document.getElementById('intraday-heart-chart')")
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_function(
        "()=>document.getElementById('intraday-heart-chart')!==window.__syntheticOriginalChart"
    )
    assert_reading(page, 0, 70)
    expect(page.locator("#intraday-sample-range")).to_have_value("1")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert_reading(page, 0, 70)
    expect(page.locator("#intraday-sample-range")).to_have_value("1")


def test_heart_sample_filter_and_time_windows_do_not_invent_or_average_readings(
    open_page, render_summary, intraday_summary
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "heart")
    page.locator("#intraday-heart-filter").select_option("resting")
    expect(page.locator("#intraday-sample-range")).to_have_attribute("max", "0")
    assert_reading(page, 0, 62)
    page.locator("#intraday-heart-filter").select_option("regular")
    expect(page.locator("#intraday-sample-range")).to_have_attribute("max", "2")
    page.locator("#intraday-sample-range").focus()
    page.keyboard.press("Home")
    assert_reading(page, HOUR, 70)
    page.locator('[data-intraday-window="afternoon"]').click()
    expect(page.locator("#intraday-sample-range")).to_have_attribute("max", "0")
    assert_reading(page, 12 * HOUR, 90)
    page.locator('[data-intraday-window="morning"]').click()
    expect(page.locator("#intraday-empty")).to_be_visible()
    page.locator('[data-intraday-window="all"]').click()
    page.locator("#intraday-heart-filter").select_option("all")
    expect(page.locator("#intraday-sample-range")).to_have_attribute("max", "3")


@pytest.mark.parametrize("width", [320, 390])
def test_touching_heart_chart_selects_real_sample_without_mouse_hover(
    open_page, render_summary, intraday_summary, width
):
    page = open_page(render_summary(intraday_summary), width=width, height=844, theme="dark")
    open_intraday(page, "heart")
    chart = page.locator("#intraday-heart-chart")
    chart.scroll_into_view_if_needed()
    box = chart.bounding_box()
    chart.tap(position={"x": box["width"] - 8, "y": box["height"] / 2})
    assert_reading(page, 24 * HOUR - 1, 68)
    chart.tap(position={"x": 8, "y": box["height"] / 2})
    assert_reading(page, 0, 62)
    expect(page.locator("#intraday-reading")).to_be_visible()


def test_dense_heart_chart_keeps_extrema_gaps_and_all_raw_readings_in_csv(
    open_page, render_summary, intraday_summary, tmp_path
):
    samples = []
    for index in range(3000):
        time = index * 20_000 + (2 * HOUR if index >= 1400 else 0)
        bpm = 180 if index == 1203 else 42 if index == 2204 else 70 + index % 17
        samples.append([time, bpm, int(index % 101 == 0)])
    intraday_summary["intraday"]["heart"] = {"2026-01-01": samples}
    page = open_page(render_summary(intraday_summary), width=390, height=844, theme="dark")
    open_intraday(page, "heart")
    chart = page.locator("#intraday-heart-chart")
    expect(chart).to_have_attribute("data-samples", "3000")
    assert 0 < int(chart.get_attribute("data-drawn-samples")) <= 2000
    expect(chart).to_have_attribute("data-drawn-min", "42")
    expect(chart).to_have_attribute("data-drawn-max", "180")
    expect(chart).to_have_attribute("data-segments", "2")
    assert chart.locator("path.intraday-heart-trace").get_attribute("d").count("M") == 2
    slider = page.locator("#intraday-sample-range")
    expect(slider).to_have_attribute("max", "2999")
    slider.focus()
    page.keyboard.press("Home")
    assert_reading(page, samples[0][0], samples[0][1])
    page.keyboard.press("ArrowRight")
    assert_reading(page, samples[1][0], samples[1][1])
    page.keyboard.press("End")
    assert_reading(page, samples[-1][0], samples[-1][1])
    for index in (1203, 2204):
        slider.evaluate(
            "(el,index)=>{el.value=String(index);el.dispatchEvent(new Event('input',{bubbles:true}));}",
            index,
        )
        assert_reading(page, samples[index][0], samples[index][1])
    chart.scroll_into_view_if_needed()
    output = Path(__file__).resolve().parents[1] / "dashboard" / "qa"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / "synthetic-intraday-dense-heart-390-dark.png"))
    page.locator("#intraday-heart-records summary").click()
    with page.expect_download() as pending:
        page.locator("#intraday-heart-export").click()
    saved = tmp_path / "synthetic-dense-heart.csv"
    pending.value.save_as(saved)
    with saved.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert len(rows) == 3001
    assert [float(row[2]) for row in rows[1:]] == [sample[1] for sample in samples]
    assert rows[1][1] == "00:00:00"
    assert rows[-1][1] == "18:39:40"
    assert {row[0] for row in rows[1:]} == {"2026-01-01"}


@pytest.mark.parametrize("category", ["heart", "sleep"])
def test_date_navigation_changes_intraday_and_detail_date_together(
    open_page, render_summary, intraday_summary, category
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, category)
    choose_date(page, "2026-01-03")
    expect(page.locator("#detail-date")).to_have_value("2026-01-03")
    if category == "heart":
        page.locator("#intraday-sample-range").focus()
        page.keyboard.press("Home")
        assert_reading(page, 8 * HOUR, 66)
    else:
        expect(page.locator("#intraday-sleep-session option")).to_have_count(2)
    page.locator("#intraday-prev-day").click()
    expect(page.locator("#intraday-date")).to_have_value("2026-01-02")
    expect(page.locator("#detail-date")).to_have_value("2026-01-02")
    expect(page.locator("#intraday-empty")).to_be_visible()
    page.locator("#intraday-next-day").click()
    expect(page.locator("#intraday-date")).to_have_value("2026-01-03")
    expect(page.locator("#intraday-empty")).to_have_count(0)


@pytest.mark.parametrize("category", ["heart", "sleep"])
def test_existing_daily_selector_updates_intraday(
    open_page, render_summary, intraday_summary, category
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, category)
    page.locator("#detail-date").fill("2026-01-03")
    page.locator("#detail-date").dispatch_event("change")
    expect(page.locator("#intraday-date")).to_have_value("2026-01-03")
    if category == "heart":
        expect(page.locator("#intraday-sample-range")).to_have_attribute("max", "2")
    else:
        expect(page.locator("#intraday-sleep-session option")).to_have_count(2)


def test_sleep_crosses_midnight_and_explains_approximate_unplaced_time(
    open_page, render_summary, intraday_summary
):
    page = open_page(render_summary(intraday_summary))
    panel = open_intraday(page, "sleep")
    note = page.locator("#intraday-sleep-note")
    expect(note).to_have_attribute("data-mode", "approximate")
    expect(note).to_contain_text(re.compile("近似|估计|推算"))
    expect(panel).to_contain_text("2025")
    expect(panel).to_contain_text("2026")
    expect(panel).to_contain_text("23:30")
    expect(panel).to_contain_text("07:30")
    expect(page.locator("#intraday-sleep-unplaced")).to_contain_text("30")
    expect(page.locator("#intraday-sleep-unplaced")).to_contain_text(re.compile("未定位|无法定位"))
    expect(page.locator("#intraday-sleep-chart")).to_have_attribute("data-window-minutes", "480")
    for stage, value in (("deep", 90), ("light", 270), ("rem", 60), ("awake", 30)):
        expect(page.locator(f'#intraday-sleep-chart [data-intraday-stage="{stage}"]')).to_have_attribute(
            "data-duration-minutes", str(value)
        )
    # A 90-minute stage must not be stretched to absorb the unlocated 30 minutes.
    widths = page.evaluate("""() => {
      const chart=document.getElementById('intraday-sleep-chart');
      const width=stage=>chart.querySelector('[data-intraday-stage="'+stage+'"]').getBoundingClientRect().width;
      return {deep:width('deep'),light:width('light')};
    }""")
    assert widths["light"] / widths["deep"] == pytest.approx(3, rel=0.05)


def test_sleep_duration_mismatch_uses_sequence_and_nap_keeps_own_minutes(
    open_page, render_summary, intraday_summary
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "sleep", "2026-01-03")
    note = page.locator("#intraday-sleep-note")
    expect(note).to_have_attribute("data-mode", "sequence")
    expect(note).to_contain_text(re.compile("不一致|超出|超过"))
    expect(note).to_contain_text("顺序")
    expect(page.locator('#intraday-sleep-chart [data-intraday-stage="deep"]')).to_have_attribute(
        "data-duration-minutes", "180"
    )
    expect(page.locator('#intraday-sleep-chart [data-intraday-stage="light"]')).to_have_attribute(
        "data-duration-minutes", "240"
    )
    options = page.locator("#intraday-sleep-session option")
    nap = next(
        options.nth(index).get_attribute("value") for index in range(options.count())
        if "小睡" in options.nth(index).inner_text()
    )
    page.locator("#intraday-sleep-session").select_option(nap)
    expect(page.locator("#intraday-sleep-note")).to_have_attribute("data-mode", "approximate")
    expect(page.locator("#intraday-sleep-chart")).to_have_attribute("data-window-minutes", "30")
    expect(page.locator('#intraday-sleep-chart [data-intraday-stage="light"]')).to_have_attribute(
        "data-duration-minutes", "30"
    )


def test_invalid_sleep_sequence_does_not_claim_approximate_clock_positions(
    open_page, render_summary, intraday_summary
):
    intraday_summary["intraday"]["sleep"]["2026-01-01"][0]["sequenceValid"] = False
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "sleep")
    note = page.locator("#intraday-sleep-note")
    expect(note).to_have_attribute("data-mode", "sequence")
    expect(note).to_contain_text(re.compile("不完整|无效|无法|缺失"))
    expect(note).to_contain_text("顺序")


@pytest.mark.parametrize("category", ["heart", "sleep"])
def test_detail_shortcut_brings_intraday_into_view_and_moves_focus(
    open_page, render_summary, intraday_summary, category
):
    page = open_page(render_summary(intraday_summary), width=390, height=844)
    page.evaluate(
        "category=>window.MiFitDetails.open(category,undefined,'2026-01-01')", category
    )
    page.locator("#detail-jump-intraday").click()
    panel = page.locator("#intraday-panel")
    expect(panel).to_be_in_viewport()
    assert panel.evaluate("(el)=>el===document.activeElement||el.contains(document.activeElement)")


def test_intraday_svg_follows_system_theme_without_losing_sample_controls(
    open_page, render_summary, intraday_summary
):
    page = open_page(render_summary(intraday_summary), theme="light")
    open_intraday(page, "heart")
    page.locator("#intraday-heart-filter").select_option("regular")
    chart_label = page.locator("#intraday-heart-chart text").first
    expect(chart_label).to_be_visible()
    initial_fill = chart_label.evaluate("(el)=>getComputedStyle(el).fill")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    dark_fill = chart_label.evaluate("(el)=>getComputedStyle(el).fill")
    assert dark_fill != initial_fill
    expect(page.locator("#intraday-heart-filter")).to_have_value("regular")
    expect(page.locator("#intraday-date")).to_have_value("2026-01-01")
    assert not page.evaluate(INTRADAY_AUDIT_JS)["contrasts"]
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    assert chart_label.evaluate("(el)=>getComputedStyle(el).fill") == initial_fill


@pytest.mark.parametrize("category", ["heart", "sleep"])
@pytest.mark.parametrize("archive", ["legacy", "missing-day"])
def test_missing_intraday_data_never_reconstructs_curve_from_daily_summary(
    open_page, render_summary, intraday_summary, category, archive
):
    if archive == "legacy":
        intraday_summary.pop("intraday")
        date = "2026-01-01"
    else:
        date = "2026-01-02"
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, category, date)
    expect(page.locator("#intraday-empty")).to_be_visible()
    expect(page.locator(f"#intraday-{category}-chart")).not_to_be_visible()
    if archive == "legacy":
        expect(page.locator("#intraday-empty")).to_contain_text(re.compile("导入|重新|原始"))


def test_saving_reopened_html_preserves_all_intraday_values_offline(
    open_page, render_summary, intraday_summary, tmp_path
):
    page = open_page(render_summary(intraday_summary))
    open_intraday(page, "heart")
    with page.expect_download() as pending:
        page.locator("#detail-save-html").click()
    path = tmp_path / "synthetic-intraday-saved.html"
    pending.value.save_as(path)
    reopened = open_page(path, width=390, height=844, theme="dark")
    assert embedded(reopened)["intraday"] == intraday_summary["intraday"]
    expect(reopened.locator("#detail-dialog")).not_to_be_visible()
    open_intraday(reopened, "heart")
    reopened.locator("#intraday-sample-range").focus()
    reopened.keyboard.press("Home")
    assert_reading(reopened, 0, 62)
    reopened.locator("#detail-close").click()
    open_intraday(reopened, "sleep")
    expect(reopened.locator("#intraday-sleep-note")).to_have_attribute("data-mode", "approximate")


def test_import_new_raw_file_replaces_intraday_and_preserves_source(
    open_page, render_summary, intraday_summary, raw_import_file
):
    fingerprint = hashlib.sha256(raw_import_file.read_bytes()).hexdigest()
    page = open_page(render_summary(intraday_summary))
    page.get_by_test_id("import-file").set_input_files(raw_import_file)
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    data = embedded(page)["intraday"]
    assert set(data["heart"]) == {"2026-01-02"}
    assert data["heart"]["2026-01-02"] == [[0, 60, 1]]
    assert not data["sleep"]
    open_intraday(page, "heart", "2026-01-02")
    assert_reading(page, 0, 60)
    assert hashlib.sha256(raw_import_file.read_bytes()).hexdigest() == fingerprint


@pytest.mark.parametrize("include_intraday", [False, True])
def test_import_summary_replaces_or_clears_old_intraday(
    open_page, render_summary, intraday_summary, tmp_path, include_intraday
):
    replacement = copy.deepcopy(intraday_summary)
    if include_intraday:
        replacement["intraday"] = {
            "version": 1, "heart": {"2026-01-03": [[9 * HOUR, 77, 0]]}, "sleep": {},
        }
    else:
        replacement.pop("intraday")
    path = tmp_path / "synthetic-replacement-summary.json"
    path.write_text(json.dumps(replacement), encoding="utf-8")
    page = open_page(render_summary(intraday_summary))
    page.get_by_test_id("import-file").set_input_files(path)
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    open_intraday(page, "heart", "2026-01-03")
    if include_intraday:
        assert_reading(page, 9 * HOUR, 77)
        assert embedded(page)["intraday"] == replacement["intraday"]
    else:
        expect(page.locator("#intraday-empty")).to_be_visible()
        assert not embedded(page).get("intraday", {}).get("heart")


@pytest.mark.parametrize("mode", ["embedded", "imported"])
def test_intraday_only_archive_can_select_its_real_dates_and_render_samples(
    open_page, render_summary, intraday_summary, tmp_path, mode
):
    intraday_only = copy.deepcopy(intraday_summary)
    intraday_only["days"] = []
    intraday_only["workouts"] = []
    intraday_only["meta"].pop("range", None)
    if mode == "embedded":
        page = open_page(render_summary(intraday_only))
    else:
        page = open_page(render_summary(intraday_summary))
        path = tmp_path / "synthetic-intraday-only.json"
        path.write_text(json.dumps(intraday_only), encoding="utf-8")
        page.get_by_test_id("import-file").set_input_files(path)
        expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    expect(page.locator("#range-start")).to_have_value("2026-01-01")
    expect(page.locator("#range-end")).to_have_value("2026-01-03")
    open_intraday(page, "heart", "2026-01-03")
    expect(page.locator("#intraday-date")).to_have_value("2026-01-03")
    expect(page.locator("#intraday-heart-chart")).to_have_attribute("data-samples", "3")
    choose_date(page, "2026-01-01")
    expect(page.locator("#intraday-heart-chart")).to_have_attribute("data-samples", "4")
    page.locator("#intraday-sample-range").focus()
    page.keyboard.press("Home")
    assert_reading(page, 0, 62)


@pytest.mark.parametrize("width,height", [(320, 740), (390, 844), (1440, 960)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_intraday_layout_contrast_and_synthetic_screenshots(
    open_page, render_summary, intraday_summary, width, height, theme
):
    page = open_page(render_summary(intraday_summary), width=width, height=height, theme=theme)
    output = Path(__file__).resolve().parents[1] / "dashboard" / "qa"
    output.mkdir(parents=True, exist_ok=True)
    for category in ("heart", "sleep"):
        panel = open_intraday(page, category)
        panel.scroll_into_view_if_needed()
        box = panel.bounding_box()
        assert 0 <= box["x"] < width
        assert box["x"] + box["width"] <= width + 1
        audit = page.evaluate(INTRADAY_AUDIT_JS)
        assert not audit["overflow"], (category, audit["overflow"])
        assert not audit["contrasts"], (category, audit["contrasts"])
        page.screenshot(path=str(output / f"synthetic-intraday-{category}-{width}-{theme}.png"))
        page.locator("#detail-close").click()
