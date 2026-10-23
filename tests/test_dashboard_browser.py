"""Offline browser QA using synthetic dashboard data only.

On this Windows host, Edge must run outside the execution sandbox. Every test
uses a fresh temporary browser profile; no installed browser profile is opened.
"""

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
from dashboard_audit import AUDIT_JS
from playwright.sync_api import expect, sync_playwright

from dashboard_builder import render_html
from dashboard_data import demo_summary

ROOT = Path(__file__).resolve().parents[1]
DEMO_HTML = ROOT / "dashboard" / "demo" / "健康仪表盘.html"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(
            channel="msedge", headless=True, chromium_sandbox=True
        )
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def render_summary(tmp_path):
    number = 0

    def render(summary=None):
        nonlocal number
        number += 1
        payload = copy.deepcopy(summary) if summary is not None else demo_summary()
        payload["meta"]["synthetic"] = True
        path = tmp_path / f"synthetic-dashboard-{number}.html"
        path.write_text(render_html(payload), encoding="utf-8")
        return path

    return render


@pytest.fixture
def tiny_summary():
    summary = demo_summary()
    template = summary["days"][0]
    rows = []
    for day, steps, sleep, resting, weight, stages in (
        ("2026-01-01", 1000, 300, 60, 70, {"deep": 60, "light": 180, "rem": 60, "awake": 20}),
        ("2026-01-02", None, None, None, None, None),
        ("2026-01-03", 9000, 540, 80, 72, {"deep": 120, "light": 300, "rem": 120, "awake": 20}),
    ):
        row = {key: None for key in template}
        row.update(
            date=day,
            steps=steps,
            sleep_minutes=sleep,
            resting_hr=resting,
            weight_kg=weight,
            sleep_stages=stages,
            distance_km=None if steps is None else steps / 1000,
            active_kcal=None if steps is None else steps / 10,
            heart_rate_avg=None if resting is None else resting + 10,
            sleep_score=None if resting is None else resting + 20,
            spo2_avg=None if resting is None else 98,
            stress_avg=None if resting is None else resting - 40,
        )
        rows.append(row)
    summary.pop("record_details", None)  # This fixture replaces the demo archive with three summary days.
    summary["days"] = rows
    summary["workouts"] = [
        {
            "date": "2026-01-03" if index % 2 else "2026-01-01",
            "start": f"10:{index:02}",
            "type": "running" if index < 9 else "cycling",
            "duration_minutes": 10 + index,
            "distance_km": (index + 1) / 10,
            "calories_kcal": 100 + index,
            "avg_heart_rate_bpm": 110 + index,
            "avg_pace_sec_per_km": None,
        }
        for index in range(14)
    ]
    summary["meta"]["range"] = {"start": "2026-01-01", "end": "2026-01-03"}
    summary["meta"]["source_counts"] = {"synthetic": 17}
    summary["meta"]["notes"] = ["Synthetic browser test data only"]
    return summary


@pytest.fixture
def open_page(browser, tmp_path, render_summary):
    """Restrict navigation to synthetic demo or this test's temporary artifacts."""
    contexts = []
    remote_requests = []
    errors = []
    dialogs = []

    def create(
        path=None, *, width=1440, height=960, theme="light", init_script=None
    ):
        path = Path(path if path is not None else render_summary()).resolve()
        assert path == DEMO_HTML.resolve() or path.is_relative_to(tmp_path.resolve())
        assert path.is_file(), f"Synthetic dashboard not generated: {path}"
        context = browser.new_context(
            viewport={"width": width, "height": height},
            color_scheme=theme,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            reduced_motion="reduce",
            is_mobile=width <= 690,
            has_touch=width <= 690,
            device_scale_factor=2 if width <= 690 else 1,
            offline=True,
        )
        contexts.append(context)
        if init_script is not None:
            context.add_init_script(init_script)

        def route(request_route):
            url = request_route.request.url
            if url.startswith(("file:", "data:", "blob:", "about:")):
                request_route.continue_()
            else:
                remote_requests.append(url)
                request_route.abort()

        context.route("**/*", route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: (
            remote_requests.append(request.url)
            if request.url.startswith(("http:", "https:", "ws:", "wss:"))
            else None
        ))
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.goto(path.as_uri(), wait_until="load")
        page.locator('html[data-ready="true"]').wait_for(state="attached")
        return page

    yield create
    for context in contexts:
        context.close()
    assert not remote_requests, f"Offline dashboard attempted external requests: {remote_requests}"
    assert not errors, f"Dashboard JavaScript errors: {errors}"
    assert not dialogs, f"Unexpected script dialogs: {dialogs}"


@pytest.mark.parametrize(
    "width,height,theme",
    [
        (320, 740, "light"),
        (390, 844, "dark"),
        (1440, 960, "light"),
        (1920, 1080, "dark"),
    ],
)
def test_layout_fits_viewport_without_horizontal_scroll(open_page, width, height, theme):
    page = open_page(width=width, height=height, theme=theme)
    dimensions = page.evaluate("""() => ({
        width: innerWidth,
        body: document.body.scrollWidth,
        document: document.documentElement.scrollWidth
    })""")
    assert max(dimensions["body"], dimensions["document"]) <= width + 1
    assert page.locator("h1").inner_text().strip()
    assert page.locator("svg, canvas").count() > 0


def test_offline_document_is_self_contained(open_page):
    page = open_page()
    assert page.locator("script[src]").count() == 0
    assert page.locator('link[rel="stylesheet"]').count() == 0
    external = page.locator(
        '[src^="http:"], [src^="https:"], link[href^="http:"], link[href^="https:"]'
    )
    assert external.count() == 0
    assert page.locator('meta[name="viewport"]').count() == 1


def test_system_theme_changes_without_reloading(open_page):
    page = open_page(theme="light")
    initial = page.evaluate("""() => [
        getComputedStyle(document.documentElement).backgroundColor,
        getComputedStyle(document.body).backgroundColor,
        getComputedStyle(document.body).color
    ]""")
    page.evaluate("window.__syntheticThemeProbe = 'same-document'")
    page.emulate_media(color_scheme="dark")
    page.wait_for_function(
        """initial => JSON.stringify([
            getComputedStyle(document.documentElement).backgroundColor,
            getComputedStyle(document.body).backgroundColor,
            getComputedStyle(document.body).color
        ]) !== JSON.stringify(initial)""",
        arg=initial,
    )
    assert page.evaluate("window.__syntheticThemeProbe") == "same-document"
    page.emulate_media(color_scheme="light")
    page.wait_for_function(
        """initial => JSON.stringify([
            getComputedStyle(document.documentElement).backgroundColor,
            getComputedStyle(document.body).backgroundColor,
            getComputedStyle(document.body).color
        ]) === JSON.stringify(initial)""",
        arg=initial,
    )


def test_restricted_local_storage_does_not_break_dashboard(open_page):
    page = open_page(init_script="""
        Object.defineProperty(window, 'localStorage', {
            configurable: true,
            get() { throw new DOMException('Synthetic storage restriction', 'SecurityError'); }
        });
    """)
    assert page.locator("h1").inner_text().strip()
    assert page.locator("svg, canvas").count() > 0


def set_range(page, start, end):
    page.get_by_test_id("range-start").fill(start)
    page.get_by_test_id("range-end").fill(end)
    page.get_by_test_id("range-apply").click()


def test_theme_override_persists_and_system_choice_resumes_live_updates(open_page):
    page = open_page(theme="light")
    html = page.locator("html")
    page.get_by_test_id("theme-dark").click()
    expect(html).to_have_attribute("data-theme", "dark")
    expect(html).to_have_attribute("data-preference", "dark")
    page.reload()
    expect(html).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="dark")
    page.get_by_test_id("theme-light").click()
    page.reload()
    expect(html).to_have_attribute("data-theme", "light")
    page.get_by_test_id("theme-system").click()
    expect(html).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(html).to_have_attribute("data-theme", "light")
    expect(page.get_by_test_id("theme-system")).to_have_attribute("aria-pressed", "true")


def test_numeric_summary_excludes_missing_days_and_uses_latest_weight(
    open_page, render_summary, tiny_summary
):
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#kpi-steps")).to_have_text("5,000步")
    expect(page.locator("#kpi-steps-note")).to_have_text("2 天有记录")
    expect(page.locator("#kpi-sleep")).to_have_text("7小时0分")
    expect(page.locator("#kpi-heart")).to_have_text("70次/分")
    expect(page.locator("#sleep-score")).to_have_text("90")
    expect(page.locator("#sleep-total")).to_have_text("7小时0分")
    expect(page.locator("#stages .stage-value")).to_have_text(
        ["1小时30分", "4小时", "1小时30分", "20分"]
    )
    expect(page.locator("#body-metrics .body-item").first.locator("strong")).to_have_text("72.0")
    expect(page.locator("#total-distance")).to_have_text("10.0km")
    expect(page.locator("#kpi-workouts")).to_have_text("14次")
    expect(page.locator("#kpi-workouts-note")).to_have_text("累计 3小时51分")


def test_inclusive_date_filter_and_missing_day_are_not_reported_as_zero(
    open_page, render_summary, tiny_summary
):
    page = open_page(render_summary(tiny_summary))
    set_range(page, "2026-01-01", "2026-01-01")
    expect(page.locator("#range-error")).to_be_empty()
    expect(page.locator("#kpi-steps")).to_have_text("1,000步")
    expect(page.locator("#kpi-sleep")).to_have_text("5小时0分")
    expect(page.locator("#kpi-workouts")).to_have_text("7次")
    expect(page.locator("#activity-span")).to_have_text("1 天")
    set_range(page, "2026-01-02", "2026-01-02")
    expect(page.locator("#kpi-steps")).to_have_text("—步")
    expect(page.locator("#kpi-sleep")).to_have_text("—")
    expect(page.locator("#sleep-score")).to_have_text("—")
    expect(page.locator("#activity-chart")).to_have_text("所选日期暂无这类记录")
    expect(page.locator("#sleep-chart")).to_have_text("所选日期暂无这类记录")
    expect(page.locator("#total-distance")).to_have_text("—km")
    assert not re.search(r"\b(?:NaN|Infinity|undefined)\b", page.locator("body").inner_text())


def test_invalid_range_preserves_last_valid_results(open_page, render_summary, tiny_summary):
    page = open_page(render_summary(tiny_summary))
    set_range(page, "2026-01-03", "2026-01-01")
    expect(page.locator("#range-error")).to_contain_text("开始日期不能晚于结束日期")
    expect(page.locator("#kpi-steps")).to_have_text("5,000步")
    set_range(page, "2000-01-01", "2026-01-03")
    expect(page.locator("#range-error")).to_contain_text("最多查看 10 年")
    expect(page.locator("#kpi-steps")).to_have_text("5,000步")


def test_chart_tabs_and_keyboard_date_inspector(open_page, render_summary, tiny_summary):
    page = open_page(render_summary(tiny_summary))
    first_tab = page.get_by_test_id("chart-tab-steps")
    first_tab.focus()
    page.keyboard.press("ArrowRight")
    expect(page.get_by_test_id("chart-tab-distance_km")).to_be_focused()
    expect(page.get_by_test_id("chart-tab-distance_km")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#activity-chart svg")).to_have_attribute(
        "aria-label", "每日活动距离，2026-01-01 至 2026-01-03"
    )
    for name, label in (
        ("active_kcal", "每日活动消耗"),
        ("heart_rate_avg", "每日平均心率"),
        ("steps", "每日步数"),
    ):
        page.get_by_test_id(f"chart-tab-{name}").click()
        expect(page.locator("#chart-legend")).to_have_text(label)
        expect(page.locator("#activity-chart svg")).to_have_attribute(
            "aria-label", f"{label}，2026-01-01 至 2026-01-03"
        )
    slider = page.locator("#day-slider")
    slider.focus()
    page.keyboard.press("Home")
    expect(page.locator("#selected-day")).to_have_text("2026.01.01")
    expect(page.locator("#day-grid strong").first).to_have_text("1,000 步")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#selected-day")).to_have_text("2026.01.02")
    expect(page.locator("#day-grid strong")).to_have_text(["—", "—", "—", "—"])


def test_workout_filter_pagination_and_range_reset(open_page, render_summary, tiny_summary):
    page = open_page(render_summary(tiny_summary))
    rows = page.get_by_test_id("workout-row")
    expect(rows).to_have_count(6)
    expect(page.locator("#workout-page")).to_have_text("1 / 3")
    expect(page.get_by_test_id("workout-prev")).to_be_disabled()
    first_rows = rows.all_text_contents()
    page.get_by_test_id("workout-next").click()
    expect(page.locator("#workout-page")).to_have_text("2 / 3")
    assert rows.all_text_contents() != first_rows
    page.get_by_test_id("workout-next").click()
    expect(rows).to_have_count(2)
    expect(page.get_by_test_id("workout-next")).to_be_disabled()
    page.get_by_test_id("workout-filter").select_option("running")
    expect(page.locator("#workout-count")).to_have_text("共 9 条记录")
    expect(page.locator("#workout-page")).to_have_text("1 / 2")
    assert all("跑步" in text for text in rows.all_text_contents())
    page.get_by_test_id("workout-next").click()
    expect(rows).to_have_count(3)
    page.get_by_test_id("workout-filter").select_option("cycling")
    expect(page.locator("#workout-count")).to_have_text("共 5 条记录")
    expect(page.locator("#workout-page")).to_have_text("1 / 1")
    set_range(page, "2026-01-02", "2026-01-02")
    expect(rows).to_have_count(0)
    expect(page.locator("#workout-empty")).to_be_visible()
    expect(page.get_by_test_id("workout-filter")).to_have_value("all")
    expect(page.get_by_test_id("workout-next")).to_be_disabled()


def test_empty_payload_shows_explicit_missing_values(open_page, render_summary, tiny_summary):
    tiny_summary["days"] = []
    tiny_summary["workouts"] = []
    tiny_summary["meta"]["range"] = {"start": None, "end": None}
    tiny_summary["meta"]["source_counts"] = {}
    page = open_page(render_summary(tiny_summary), width=390, height=844)
    expect(page.locator("#archive-range")).to_have_text("尚无可用记录")
    expect(page.locator("#kpi-steps")).to_have_text("—步")
    expect(page.locator("#kpi-heart")).to_have_text("—次/分")
    expect(page.locator("#kpi-sleep")).to_have_text("—")
    expect(page.locator("#workout-empty")).to_be_visible()
    expect(page.locator("#total-distance")).to_have_text("—km")
    assert not re.search(r"\b(?:NaN|Infinity|undefined)\b", page.locator("body").inner_text())


def test_untrusted_text_cannot_escape_data_script_or_create_elements(
    open_page, render_summary, tiny_summary
):
    hostile = '</script><script>window.__dashboardInjected=true</script><img src=x onerror=alert("bad")>'
    tiny_summary["workouts"] = [{**tiny_summary["workouts"][0], "type": hostile}]
    tiny_summary["meta"]["notes"] = [hostile]
    page = open_page(render_summary(tiny_summary))
    assert page.evaluate("window.__dashboardInjected") is None
    assert page.locator("img").count() == 0
    expect(page.locator("#workout-body .workout-type")).to_have_text(hostile)
    page.locator("#info-button").click()
    expect(page.locator("#method-notes li")).to_have_text(hostile)
    page.keyboard.press("Escape")
    expect(page.locator("#info-dialog")).not_to_be_visible()


def test_long_untrusted_workout_name_does_not_overflow_mobile(
    open_page, render_summary, tiny_summary
):
    tiny_summary["workouts"] = [
        {**tiny_summary["workouts"][0], "type": "SyntheticLongWorkout" * 40}
    ]
    page = open_page(render_summary(tiny_summary), width=320, height=740)
    assert page.evaluate("document.documentElement.scrollWidth") <= 321


def test_keyboard_tab_order_reaches_workout_filter_without_hundreds_of_stops(open_page):
    page = open_page()
    page.locator("#day-slider").focus()
    reached = False
    for _ in range(16):
        page.keyboard.press("Tab")
        if page.locator("#workout-filter").evaluate("(el) => document.activeElement === el"):
            reached = True
            break
    assert reached, "The calendar should not add hundreds of individual Tab stops"


def test_calendar_keyboard_navigation_has_one_tab_stop_and_updates_inspector(open_page):
    page = open_page()
    current = page.locator('#heatmap button[tabindex="0"]')
    expect(current).to_have_count(1)
    current.focus()
    initial_label = current.get_attribute("aria-label")
    page.keyboard.press("ArrowLeft")
    expect(page.locator('#heatmap button[tabindex="0"]')).to_have_count(1)
    active_label = page.evaluate("document.activeElement.getAttribute('aria-label')")
    assert active_label != initial_label
    expect(page.locator("#selected-day")).to_have_text(active_label[:10])


@pytest.mark.parametrize("theme,width", [("light", 1440), ("dark", 1440), ("light", 390), ("dark", 390)])
def test_visible_text_meets_minimum_contrast(open_page, theme, width):
    page = open_page(theme=theme, width=width)
    audit = page.evaluate(AUDIT_JS)
    assert not audit["contrasts"], audit["contrasts"]


def test_mobile_primary_chart_controls_are_large_enough_to_target(open_page):
    page = open_page(width=390, height=844)
    for selector in ("#day-slider", '[data-testid="chart-tab-steps"]', '[data-testid="chart-tab-distance_km"]'):
        box = page.locator(selector).bounding_box()
        assert box["width"] >= 24 and box["height"] >= 24, (selector, box)


def test_saved_html_reopens_offline_with_same_synthetic_summary(
    open_page, render_summary, tiny_summary, tmp_path
):
    page = open_page(render_summary(tiny_summary))
    with page.expect_download() as pending:
        page.locator("#download-html").click()
    path = tmp_path / "synthetic-saved-dashboard.html"
    pending.value.save_as(path)
    reopened = open_page(path, width=390, height=844, theme="dark")
    expect(reopened.locator("#kpi-steps")).to_have_text("5,000步")
    expect(reopened.locator("#kpi-workouts")).to_have_text("14次")
    expect(reopened.locator("html")).to_have_attribute("data-theme", "dark")
    expect(reopened.locator("#demo-banner")).to_be_visible()


@pytest.fixture
def raw_import_file(tmp_path):
    path = tmp_path / "synthetic-mi-fitness-export.json"
    payload = {
        "schema_version": "1.0",
        "source": "mi_fitness_data_bridge",
        "generated_at": "2026-01-03T00:00:00Z",
        "records": {
            "daily_activity": [{
                "date": "2026-01-01", "steps": 1000, "distance_m": 1000,
                "active_kcal": 100, "active_minutes": 20,
                "user_id": "SYNTHETIC_PRIVATE_ACCOUNT",
                "device_id": "SYNTHETIC_PRIVATE_DEVICE",
            }],
            "heart_rate": [{
                "timestamp": "2026-01-01T16:00:00Z", "bpm": 60,
                "sample_type": "resting",
            }],
            "workout_gps": [{
                "latitude": 1, "longitude": 2,
                "id": "SYNTHETIC_PRIVATE_GPS_RECORD",
            }],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def large_import_file(tmp_path):
    """A 16+ MB synthetic source, written in blocks without a huge Python list."""
    path = tmp_path / "synthetic-large-export.json"
    record = json.dumps({
        "timestamp": "2026-01-01T00:00:00+08:00", "bpm": 60,
        "sample_type": "resting", "user_id": "SYNTHETIC_PRIVATE_ACCOUNT",
    }, separators=(",", ":")).encode()
    with path.open("wb") as handle:
        handle.write(b'{"schema_version":"1.0","records":{"heart_rate":[')
        # Retained intraday samples now have a separate per-day limit. Keep this
        # throughput/responsiveness fixture large while distributing its samples
        # across days; limit rejection is covered by the model/worker tests.
        for index in range(36):
            dated_record = record.replace(
                b"2026-01-01", f"2026-01-{index // 2 + 1:02d}".encode()
            )
            handle.write((dated_record + b",") * 4096)
        handle.write(record)
        handle.write(b"]}}")
    return path


def test_import_raw_json_uses_shanghai_day_and_saved_copy_omits_private_fields(
    open_page, raw_import_file, tmp_path
):
    before = hashlib.sha256(raw_import_file.read_bytes()).hexdigest()
    page = open_page(width=390, height=844)
    page.get_by_test_id("import-file").set_input_files(raw_import_file)
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    expect(page.locator("#import-state")).not_to_be_visible()
    expect(page.locator("#import-button")).to_be_enabled()
    expect(page.locator("#kpi-steps")).to_have_text("1,000步")
    expect(page.locator("#kpi-heart")).to_have_text("60次/分")
    expect(page.locator("#selected-day")).to_have_text("2026.01.02")
    expect(page.locator("#demo-banner")).not_to_be_visible()
    expect(page.locator("#activity-span")).to_have_text("2 天")
    set_range(page, "2026-01-01", "2026-01-01")
    expect(page.locator("#kpi-heart")).to_have_text("—次/分")
    set_range(page, "2026-01-02", "2026-01-02")
    expect(page.locator("#kpi-heart")).to_have_text("60次/分")
    expect(page.locator("#kpi-steps")).to_have_text("—步")
    with page.expect_download() as pending:
        page.locator("#download-html").click()
    saved = tmp_path / "synthetic-imported-dashboard.html"
    pending.value.save_as(saved)
    text = saved.read_text(encoding="utf-8")
    assert "SYNTHETIC_PRIVATE_ACCOUNT" not in text
    assert "SYNTHETIC_PRIVATE_DEVICE" not in text
    assert "SYNTHETIC_PRIVATE_GPS_RECORD" not in text
    reopened = open_page(saved, width=390, height=844, theme="dark")
    expect(reopened.locator("#kpi-steps")).to_have_text("1,000步")
    expect(reopened.locator("#kpi-heart")).to_have_text("60次/分")
    expect(reopened.locator("#demo-banner")).not_to_be_visible()
    assert hashlib.sha256(raw_import_file.read_bytes()).hexdigest() == before


def test_import_cancel_preserves_existing_data_and_allows_next_import(
    open_page, large_import_file, raw_import_file
):
    page = open_page()
    original = page.locator("#kpi-steps").inner_text()
    page.get_by_test_id("import-file").set_input_files(large_import_file)
    expect(page.locator("#import-state")).to_be_visible()
    expect(page.locator("#import-button")).to_be_disabled()
    page.locator("#import-cancel").click()
    expect(page.locator("#toast")).to_contain_text("已取消导入")
    expect(page.locator("#import-state")).not_to_be_visible()
    expect(page.locator("#import-button")).to_be_enabled()
    expect(page.locator("#kpi-steps")).to_have_text(original)
    expect(page.get_by_test_id("import-file")).to_have_value("")
    page.get_by_test_id("theme-dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_test_id("import-file").set_input_files(raw_import_file)
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=15000)
    expect(page.locator("#kpi-steps")).to_have_text("1,000步")


def test_import_large_source_reports_progress_while_theme_remains_interactive(
    open_page, large_import_file
):
    page = open_page(init_script="""
        window.__syntheticTicks = 0;
        setInterval(() => window.__syntheticTicks++, 10);
    """)
    page.evaluate("""() => {
      window.__syntheticProgress = [];
      new MutationObserver(() => {
        const value = document.getElementById('import-percent').textContent;
        window.__syntheticProgress.push(value);
      }).observe(document.getElementById('import-percent'), {childList:true,subtree:true});
    }""")
    page.get_by_test_id("import-file").set_input_files(large_import_file)
    page.wait_for_function(
        "() => +document.getElementById('import-progress').value > 0",
        timeout=15000,
    )
    expect(page.locator("#import-state")).to_be_visible()
    ticks_before = page.evaluate("window.__syntheticTicks")
    page.get_by_test_id("theme-dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(page.locator("#import-state")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("导入完成", timeout=30000)
    assert page.evaluate("window.__syntheticTicks") > ticks_before + 5
    percentages = page.evaluate("window.__syntheticProgress")
    assert any(0 < int(value.rstrip("%")) < 100 for value in percentages)
    expect(page.locator("#kpi-heart")).to_have_text("60次/分")
    expect(page.locator("#kpi-steps")).to_have_text("—步")


def test_import_invalid_json_leaves_existing_dashboard_usable(open_page, tmp_path):
    page = open_page()
    original = page.locator("#kpi-steps").inner_text()
    path = tmp_path / "synthetic-invalid.json"
    path.write_text('{"schema_version":"1.0","records":{"daily_activity":[', encoding="utf-8")
    page.get_by_test_id("import-file").set_input_files(path)
    expect(page.locator("#toast")).to_contain_text(
        re.compile(r"JSON.*(?:不完整|不正确|不支持)|导入未完成"), timeout=15000
    )
    expect(page.locator("#import-state")).not_to_be_visible()
    expect(page.locator("#import-button")).to_be_enabled()
    expect(page.locator("#kpi-steps")).to_have_text(original)
    page.get_by_test_id("theme-dark").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


@pytest.mark.parametrize("duration,expected", [(None, "累计 —"), (0, "累计 0分")])
def test_workout_duration_distinguishes_missing_from_real_zero(
    open_page, render_summary, tiny_summary, duration, expected
):
    for workout in tiny_summary["workouts"]:
        workout["duration_minutes"] = duration
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#kpi-workouts")).to_have_text("14次")
    expect(page.locator("#kpi-workouts-note")).to_have_text(expected)


def test_sleep_zero_score_is_included_in_mean(open_page, render_summary, tiny_summary):
    tiny_summary["days"][0]["sleep_score"] = 0
    tiny_summary["days"][2]["sleep_score"] = 100
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#sleep-score")).to_have_text("50")
    expect(page.locator("#sleep-note")).to_contain_text("2 晚有有效评分")


def test_sleep_stage_means_use_complete_common_nights_and_report_sample_count(
    open_page, render_summary, tiny_summary
):
    tiny_summary["days"][1]["sleep_minutes"] = 900
    tiny_summary["days"][1]["sleep_stages"] = {
        "deep": 900, "light": None, "rem": None, "awake": None,
    }
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#stages .stage-value")).to_have_text(
        ["1小时30分", "4小时", "1小时30分", "20分"]
    )
    note = page.locator("#sleep-note")
    expect(note).to_contain_text("3 晚有主睡眠记录")
    expect(note).to_contain_text(re.compile(r"2\s*晚[^。]*(?:完整|阶段)|(?:完整|阶段)[^。]*2\s*晚"))
    set_range(page, "2026-01-02", "2026-01-02")
    expect(page.locator("#stages .stage-value")).to_have_text(["—", "—", "—", "—"])


def test_complementary_incomplete_sleep_nights_are_not_combined_into_one_distribution(
    open_page, render_summary, tiny_summary
):
    tiny_summary["days"][0]["sleep_stages"] = {
        "deep": 60, "light": None, "rem": None, "awake": None,
    }
    tiny_summary["days"][2]["sleep_stages"] = {
        "deep": None, "light": 300, "rem": 120, "awake": 20,
    }
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#stages .stage-value")).to_have_text(["—", "—", "—", "—"])
    expect(page.locator("#sleep-note")).to_contain_text("0 晚四阶段完整记录")
    assert page.locator("#stage-bar span").evaluate_all(
        "(elements) => elements.every(element => element.style.width === '0%')"
    )


def test_long_archive_initializes_and_saved_copy_keeps_old_history(
    open_page, render_summary, tiny_summary, tmp_path
):
    tiny_summary["days"][0]["date"] = "2010-01-01"
    tiny_summary["meta"]["range"]["start"] = "2010-01-01"
    page = open_page(render_summary(tiny_summary))
    expect(page.locator("#range-error")).to_be_empty()
    expect(page.get_by_test_id("range-start")).to_have_value("2025-01-04")
    expect(page.get_by_test_id("range-end")).to_have_value("2026-01-03")
    expect(page.locator("#kpi-steps")).to_have_text("9,000步")
    expect(page.locator("#activity-chart svg")).to_be_visible()
    expect(page.locator("#archive-range")).to_contain_text("2010.01.01")
    set_range(page, "2010-01-01", "2010-01-01")
    expect(page.locator("#kpi-steps")).to_have_text("1,000步")
    with page.expect_download() as pending:
        page.locator("#download-html").click()
    saved = tmp_path / "synthetic-long-history.html"
    pending.value.save_as(saved)
    reopened = open_page(saved, width=390, height=844)
    expect(reopened.locator("#range-error")).to_be_empty()
    expect(reopened.locator("#kpi-steps")).to_have_text("9,000步")
    expect(reopened.locator("#activity-chart svg")).to_be_visible()
    set_range(reopened, "2010-01-01", "2010-01-01")
    expect(reopened.locator("#kpi-steps")).to_have_text("1,000步")


def test_saved_copy_during_import_reopens_with_enabled_import_controls(
    open_page, large_import_file, tmp_path
):
    page = open_page()
    previous = page.locator("#kpi-steps").inner_text()
    page.get_by_test_id("import-file").set_input_files(large_import_file)
    expect(page.locator("#import-state")).to_be_visible()
    with page.expect_download() as pending:
        page.locator("#download-html").click()
    saved = tmp_path / "synthetic-during-import.html"
    pending.value.save_as(saved)
    page.locator("#import-cancel").click()
    reopened = open_page(saved, width=390, height=844)
    expect(reopened.locator("#import-state")).not_to_be_visible()
    expect(reopened.locator("#import-button")).to_be_enabled()
    expect(reopened.locator("#kpi-steps")).to_have_text(previous)


def test_empty_archive_default_date_is_shanghai_date_after_utc_midnight_boundary(
    open_page, render_summary, tiny_summary
):
    tiny_summary["days"] = []
    tiny_summary["workouts"] = []
    page = open_page(render_summary(tiny_summary), init_script="""
        const RealDate = Date;
        const fixed = RealDate.parse('2026-10-20T17:00:00Z');
        window.Date = class extends RealDate {
            constructor(...args) { super(...(args.length ? args : [fixed])); }
            static now() { return fixed; }
        };
    """)
    expect(page.get_by_test_id("range-start")).to_have_value("2026-10-21")
    expect(page.get_by_test_id("range-end")).to_have_value("2026-10-21")
