"""Geometry regressions for scrolling only the modal's synthetic content pane."""

import hashlib
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from test_dashboard_browser import browser as browser  # noqa: PLC0414
from test_dashboard_browser import open_page as open_page  # noqa: PLC0414
from test_dashboard_browser import render_summary as render_summary  # noqa: PLC0414
from test_dashboard_browser import tiny_summary as tiny_summary  # noqa: PLC0414
from test_dashboard_intraday_browser import intraday_summary as intraday_summary  # noqa: PLC0414

ROOT = Path(__file__).resolve().parents[1]
VIEWPORTS = [
    pytest.param(1440, 900, 1, id="desktop1440"),
    pytest.param(1536, 864, 1, id="desktop1536"),
    pytest.param(390, 844, 1, id="phone390"),
    pytest.param(320, 740, 1, id="phone320"),
    pytest.param(1440, 900, 1.25, id="desktop1440-zoom125"),
    pytest.param(1536, 864, 1.25, id="desktop1536-zoom125"),
    # Mobile browser zoom reduces the CSS viewport. Applying body zoom instead
    # would enlarge the 100vw/100dvh modal beyond the viewport before any click.
    pytest.param(round(390 / 1.25), round(844 / 1.25), 1, id="phone390-viewport125"),
    pytest.param(round(320 / 1.25), round(740 / 1.25), 1, id="phone320-viewport125"),
]
GEOMETRY = """target => {
  const selectors={
    dialog:'#detail-dialog',shell:'.detail-shell',scroll:'.detail-scroll',
    header:'.detail-header',nav:'.detail-nav',close:'#detail-close',panel:target
  };
  const output={window:{x:scrollX,y:scrollY,width:innerWidth,height:innerHeight}};
  for(const [key,selector] of Object.entries(selectors)){
    const element=document.querySelector(selector);
    if(!element){output[key]=null;continue;}
    const r=element.getBoundingClientRect();
    output[key]={
      x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,
      scrollTop:element.scrollTop,scrollLeft:element.scrollLeft,
      clientHeight:element.clientHeight,scrollHeight:element.scrollHeight,
      overflowY:getComputedStyle(element).overflowY
    };
  }
  return output;
}"""


def geometry(page, target):
    return page.evaluate(GEOMETRY, target)


def evidence(page, before, after, label):
    """Record only this module's freshly generated synthetic page."""
    source = (ROOT / "dashboard" / "detail-view.js").read_bytes()
    source += (ROOT / "dashboard" / "detail-view.css").read_bytes()
    version = hashlib.sha256(source).hexdigest()[:10]
    destination = ROOT / "dashboard" / "qa"
    destination.mkdir(parents=True, exist_ok=True)
    name = f"synthetic-detail-scroll-{label}-{version}"
    (destination / f"{name}.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8"
    )
    page.screenshot(path=str(destination / f"{name}.png"))


def stable_frame(before, after, zoom):
    for name in ("dialog", "shell", "header", "nav", "close"):
        for field in ("x", "y", "width", "height"):
            assert after[name][field] == pytest.approx(before[name][field], abs=1), (
                name, field, before[name], after[name]
            )
    for name in ("dialog", "shell", "header", "nav"):
        assert after[name]["scrollTop"] == before[name]["scrollTop"] == 0, (
            name, before[name], after[name]
        )
    assert after["window"] == before["window"]
    assert after["scroll"]["scrollTop"] > before["scroll"]["scrollTop"]
    assert after["scroll"]["y"] == pytest.approx(before["scroll"]["y"], abs=1)
    assert after["scroll"]["height"] == pytest.approx(before["scroll"]["height"], abs=1)
    # The content pane must still fill the frame to its bottom; outer scrolling
    # used to expose a large blank strip below the moved pane.
    assert abs(after["dialog"]["bottom"] - after["scroll"]["bottom"]) <= 2 * zoom
    assert after["scroll"]["y"] - 1 <= after["panel"]["y"] <= after["scroll"]["y"] + 32 * zoom
    assert after["header"]["y"] >= after["dialog"]["y"] - 1
    assert after["close"]["y"] >= after["dialog"]["y"] - 1
    assert after["close"]["bottom"] <= after["scroll"]["y"]


def setup(page, category, zoom):
    if zoom != 1:
        page.evaluate("zoom=>document.body.style.zoom=String(zoom)", zoom)
    page.evaluate(
        "category=>window.MiFitDetails.open(category,undefined,'2026-01-01')", category
    )
    expect(page.locator("#detail-dialog")).to_be_visible()
    page.locator(".detail-scroll").evaluate("(el)=>el.scrollTop=0")
    expect(page.locator("#detail-close")).to_be_in_viewport()


@pytest.mark.parametrize("width,height,zoom", VIEWPORTS)
@pytest.mark.parametrize("category", ["heart", "sleep"])
def test_intraday_shortcut_scrolls_only_content_and_preserves_fixed_frame(
    open_page, render_summary, intraday_summary, width, height, zoom, category
):
    page = open_page(render_summary(intraday_summary), width=width, height=height, theme="dark")
    setup(page, category, zoom)
    before = geometry(page, "#intraday-panel")
    page.locator("#detail-jump-intraday").click()
    after = geometry(page, "#intraday-panel")
    evidence(page, before, after, f"{category}-{width}-{height}-zoom{zoom}")
    stable_frame(before, after, zoom)
    panel = page.locator("#intraday-panel")
    assert panel.evaluate("(el)=>el===document.activeElement||el.contains(document.activeElement)")
    page.locator("#detail-category-body").click()
    expect(page.locator("#detail-dialog")).to_have_attribute("data-category", "body")
    page.locator("#detail-close").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()


@pytest.mark.parametrize("width,height,zoom", VIEWPORTS)
def test_reveal_workout_scrolls_only_content_and_preserves_fixed_frame(
    open_page, render_summary, intraday_summary, width, height, zoom
):
    page = open_page(render_summary(intraday_summary), width=width, height=height, theme="light")
    setup(page, "workouts", zoom)
    before = geometry(page, "#detail-single-workout")
    # Trigger the real record handler without Playwright scrolling every ancestor
    # before the click: the regression under test is revealWorkout's own scroll.
    page.locator('[data-detail-workout-index="0"]').evaluate("(el)=>el.click()")
    after = geometry(page, "#detail-single-workout")
    evidence(page, before, after, f"workout-{width}-{height}-zoom{zoom}")
    stable_frame(before, after, zoom)
    expect(page.locator("#detail-single-workout")).to_be_in_viewport()
    page.locator("#detail-close").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()


@pytest.mark.parametrize("category", ["heart", "sleep"])
def test_native_tab_and_shift_tab_keep_header_and_close_button_fixed(
    open_page, render_summary, intraday_summary, category
):
    page = open_page(render_summary(intraday_summary), width=1440, height=900, theme="dark")
    setup(page, category, 1)
    page.locator("#detail-jump-intraday").click()
    before = geometry(page, "#intraday-panel")
    target = "#intraday-sample-range" if category == "heart" else (
        "#intraday-sleep-chart [data-intraday-stage]"
    )
    reached = False
    for _ in range(35):
        page.keyboard.press("Tab")
        now = geometry(page, "#intraday-panel")
        assert now["dialog"]["scrollTop"] == 0
        for name in ("header", "nav", "close"):
            assert now[name]["y"] == pytest.approx(before[name]["y"], abs=1), (name, now)
        if page.evaluate("selector=>document.activeElement.matches(selector)", target):
            reached = True
            break
    assert reached, f"Native keyboard traversal did not reach {target}"
    page.keyboard.press("Shift+Tab")
    page.keyboard.press("Tab")
    assert page.evaluate("selector=>document.activeElement.matches(selector)", target)
    after = geometry(page, "#intraday-panel")
    evidence(page, before, after, f"native-tab-{category}-1440-900")
    assert after["dialog"]["scrollTop"] == 0
    for name in ("header", "nav", "close"):
        assert after[name]["y"] == pytest.approx(before[name]["y"], abs=1)
    page.locator("#detail-close").click()
    expect(page.locator("#detail-dialog")).not_to_be_visible()
