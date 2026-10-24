"""Themed dropdowns with synthetic options, including mobile scroll selection."""

import pytest
from playwright.sync_api import expect

from test_dashboard_browser import browser as browser
from test_dashboard_browser import open_page as open_page
from test_dashboard_browser import render_summary as render_summary


def add_select(page, count=3, *, modal=False):
    page.evaluate("""({count, modal}) => {
        const host = document.createElement(modal ? 'dialog' : 'section');
        host.id = 'synthetic-select-host';
        const label = document.createElement('label');
        label.textContent = '测试选项';
        const field = document.createElement('select');
        field.id = 'synthetic-select';
        for (let i = 0; i < count; i++) field.append(new Option('选项 ' + (i + 1), String(i)));
        label.append(field); host.append(label); document.querySelector('main').prepend(host);
        if (modal) host.showModal();
        window.selectEvents = [];
        for (const type of ['input', 'change']) field.addEventListener(type, () => selectEvents.push(type));
    }""", {"count": count, "modal": modal})
    field = page.locator("#synthetic-select")
    expect(field).to_have_class("mifit-select")
    return field


def test_desktop_themed_options_keep_select_api_and_keyboard(open_page):
    page = open_page()
    field = add_select(page)
    field.click()
    picker = page.locator("#select-picker-dialog")
    expect(picker).to_have_attribute("data-mode", "list")
    expect(page.locator('#select-picker-list [data-index="0"]')).to_be_focused()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(field).to_have_value("1")
    expect(field).to_be_focused()
    assert page.evaluate("selectEvents") == ["input", "change"]
    field.select_option("2")
    field.press("Alt+ArrowDown")
    expect(page.locator('#select-picker-list [data-index="2"]')).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Escape")
    expect(field).to_have_value("2")
    assert field.evaluate("el => getComputedStyle(el).appearance") == "none"


def test_dynamic_options_and_nested_date_month_picker(open_page):
    page = open_page()
    field = add_select(page, modal=True)
    field.evaluate("el => { el.options[1].disabled = true; el.append(new Option('新选项', 'new')); }")
    field.click()
    expect(page.locator('#select-picker-list [data-index="1"]')).to_be_disabled()
    page.keyboard.press("ArrowDown")
    expect(page.locator('#select-picker-list [data-index="2"]')).to_be_focused()
    page.locator('#select-picker-list [data-index="3"]').click()
    expect(field).to_have_value("new")
    expect(page.locator("#synthetic-select-host")).to_be_visible()
    field.click()
    field.evaluate("el => el.disabled = true")
    expect(page.locator("#select-picker-dialog")).not_to_be_visible()
    page.locator("#synthetic-select-host").evaluate("el => el.close()")
    page.locator("#range-start").fill("2026-01-15")
    page.locator("#range-start").click()
    page.locator("#date-picker-month").click()
    expect(page.locator("#select-picker-dialog")).to_be_visible()
    page.locator('#select-picker-list [data-index="1"]').click()
    expect(page.locator("#date-picker-month")).to_have_value("2")
    expect(page.locator("#date-picker-dialog")).to_be_visible()
    expect(page.locator("#date-picker-month")).to_be_focused()


@pytest.mark.parametrize("count,mode", [(5, "list"), (6, "wheel")])
def test_mobile_option_count_selects_list_or_bottom_wheel(open_page, count, mode):
    page = open_page(width=390, height=844, theme="dark")
    field = add_select(page, count)
    field.click()
    picker = page.locator("#select-picker-dialog")
    expect(picker).to_have_attribute("data-mode", mode)
    if mode == "list":
        page.locator('#select-picker-list [data-index="4"]').click()
        expect(field).to_have_value("4")
    else:
        box = picker.bounding_box()
        assert abs(box["y"] + box["height"] - 844) <= 1
        wheel = page.locator("#select-picker-wheel")
        wheel.press("End")
        expect(wheel.locator('[data-index="5"]')).to_have_attribute("aria-selected", "true")
        expect(field).to_have_value("0")
        page.locator("#select-picker-cancel").click()
        expect(field).to_have_value("0")
        field.click()
        wheel.press("End")
        page.locator("#select-picker-confirm").click()
        expect(field).to_have_value("5")
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_mobile_touch_scroll_commits_selected_row(open_page):
    page = open_page(width=320, height=740)
    field = add_select(page, 8)
    field.click()
    wheel = page.locator("#select-picker-wheel")
    box = wheel.bounding_box()
    session = page.context.new_cdp_session(page)
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 45
    session.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for amount in (20, 40, 60, 80):
        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - amount}]})
    session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    expect(wheel).not_to_have_attribute("aria-activedescendant", "select-picker-option-0")
    selected = wheel.locator('[aria-selected="true"]').get_attribute("data-index")
    page.locator("#select-picker-confirm").click()
    expect(field).to_have_value(selected)
    assert page.evaluate("selectEvents") == ["input", "change"]


def test_saved_offline_copy_recreates_one_select_popup(open_page, tmp_path):
    page = open_page()
    with page.expect_download() as download:
        page.locator("#download-html").click()
    saved = tmp_path / "synthetic-select-offline.html"
    download.value.save_as(saved)
    offline = open_page(saved)
    expect(offline.locator("#select-picker-dialog")).to_have_count(1)
    offline.locator("#workout-filter").click()
    expect(offline.locator("#select-picker-dialog")).to_be_visible()
    expect(offline.locator("#select-picker-list")).to_be_visible()
