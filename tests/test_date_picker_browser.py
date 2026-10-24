"""Date controls with synthetic data: desktop calendar and mobile scroll wheels."""

import pytest
from playwright.sync_api import expect

from test_dashboard_browser import browser as browser
from test_dashboard_browser import open_page as open_page
from test_dashboard_browser import render_summary as render_summary


def test_desktop_calendar_commits_only_on_confirm_and_keeps_input_api(open_page):
    page = open_page()
    field = page.locator("#range-start")
    field.fill("2024-02-28")
    page.evaluate("""() => {
        window.dateEvents = [];
        const input = document.getElementById('range-start');
        for (const kind of ['input', 'change']) input.addEventListener(kind, () => dateEvents.push(kind));
    }""")
    field.click()
    picker = page.locator("#date-picker-dialog")
    expect(picker).to_be_visible()
    expect(picker).to_have_attribute("data-mode", "calendar")
    page.locator('[data-date="2024-02-29"]').click()
    expect(field).to_have_value("2024-02-28")
    page.locator("#date-picker-cancel").click()
    expect(field).to_have_value("2024-02-28")
    expect(field).to_be_focused()
    assert page.evaluate("dateEvents") == []
    field.press("Alt+ArrowDown")
    page.locator('[data-date="2024-02-29"]').click()
    page.locator("#date-picker-confirm").click()
    expect(field).to_have_value("2024-02-29")
    expect(field).to_be_focused()
    assert page.evaluate("dateEvents") == ["input", "change"]
    expect(picker).not_to_be_visible()


def test_calendar_keyboard_bounds_and_dynamic_modal_input(open_page):
    page = open_page()
    page.evaluate("""() => {
        const dialog = document.createElement('dialog');
        dialog.id = 'synthetic-date-dialog';
        dialog.innerHTML = '<label>同步开始<input id="synthetic-date" type="date" required min="2024-02-28" max="2024-03-02" value="2024-02-28"></label>';
        document.body.append(dialog);
        dialog.showModal();
    }""")
    field = page.locator("#synthetic-date")
    expect(field).to_have_attribute("aria-haspopup", "dialog")
    field.click()
    expect(page.locator('#date-picker-grid [data-date="2024-02-27"]')).to_be_disabled()
    expect(page.locator("#date-picker-clear")).not_to_be_visible()
    expect(page.locator('#date-picker-grid [data-date="2024-02-28"]')).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.locator('#date-picker-grid [data-date="2024-02-29"]')).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(page.locator('#date-picker-grid [data-date="2024-03-02"]')).to_be_focused()
    page.locator("#date-picker-confirm").click()
    expect(field).to_have_value("2024-03-02")
    expect(field).to_be_focused()
    expect(page.locator("#synthetic-date-dialog")).to_be_visible()
    field.click()
    page.keyboard.press("Escape")
    expect(page.locator("#date-picker-dialog")).not_to_be_visible()
    expect(page.locator("#synthetic-date-dialog")).to_be_visible()


@pytest.mark.parametrize("width,theme", [(320, "light"), (390, "dark")])
def test_mobile_bottom_wheels_scroll_and_adjust_leap_day(open_page, width, theme):
    page = open_page(width=width, height=844, theme=theme)
    field = page.locator("#range-start")
    field.fill("2024-02-29")
    field.click()
    picker = page.locator("#date-picker-dialog")
    expect(picker).to_have_attribute("data-mode", "wheel")
    expect(page.locator("#date-picker-calendar")).not_to_be_visible()
    box = picker.bounding_box()
    assert abs(box["y"] + box["height"] - 844) <= 1
    assert page.evaluate("document.documentElement.scrollWidth") <= width
    year = page.locator("#date-picker-wheel-year")
    month = page.locator("#date-picker-wheel-month")
    day = page.locator("#date-picker-wheel-day")
    expect(year).to_have_attribute("aria-valuenow", "2024")
    expect(day).to_have_attribute("aria-valuemax", "29")
    year.press("ArrowDown")
    expect(year).to_have_attribute("aria-valuenow", "2025")
    expect(day).to_have_attribute("aria-valuenow", "28")
    expect(day).to_have_attribute("aria-valuemax", "28")
    month.evaluate("element => { element.scrollTop += 88; }")
    expect(month).to_have_attribute("aria-valuenow", "4")
    expect(day).to_have_attribute("aria-valuemax", "30")
    day.press("End")
    expect(day).to_have_attribute("aria-valuenow", "30")
    expect(field).to_have_value("2024-02-29")
    page.locator("#date-picker-confirm").click()
    expect(field).to_have_value("2025-04-30")
    expect(field).to_be_focused()


def test_mobile_touch_scroll_and_optional_clear(open_page):
    page = open_page(width=390, height=844)
    field = page.locator("#range-start")
    field.fill("2026-01-15")
    field.click()
    month = page.locator("#date-picker-wheel-month")
    box = month.bounding_box()
    session = page.context.new_cdp_session(page)
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 45
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for amount in (20, 40, 60, 80):
        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - amount}]})
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    expect(month).not_to_have_attribute("aria-valuenow", "1")
    page.locator("#date-picker-cancel").click()
    expect(field).to_have_value("2026-01-15")
    field.click()
    page.locator("#date-picker-clear").click()
    expect(field).to_have_value("")
