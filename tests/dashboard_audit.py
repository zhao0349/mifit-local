"""Capture and inspect only freshly rendered, fully synthetic dashboards."""

import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard_builder import render_html  # noqa: E402
from dashboard_data import demo_summary  # noqa: E402

AUDIT_JS = r"""() => {
  const color = value => {
    const m = value.match(/[\d.]+/g) || [];
    return [+(m[0]||0), +(m[1]||0), +(m[2]||0), m.length>3?+m[3]:1];
  };
  const over = (a,b) => {
    const opacity=a[3]+b[3]*(1-a[3]);
    return opacity ? [
      ...[0,1,2].map(i=>(a[i]*a[3]+b[i]*b[3]*(1-a[3]))/opacity), opacity
    ] : [0,0,0,0];
  };
  const background = el => {
    let value=[0,0,0,0];
    for(let node=el;node&&value[3]<.999;node=node.parentElement)
      value=over(value,color(getComputedStyle(node).backgroundColor));
    return over(value,[255,255,255,1]);
  };
  const lum = c => c.slice(0,3).map(v=>{
    v/=255; return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;
  }).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
  const label = el => el.id || el.dataset.testid || el.className.baseVal || el.className || el.tagName;
  const visible = el => {
    const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return r.width>2&&r.height>2&&s.display!=='none'&&s.visibility!=='hidden'
      &&s.opacity!=='0'&&!el.closest('[hidden],dialog:not([open])');
  };
  const contrasts=[],overflow=[],targets=[];
  for(const el of document.body.querySelectorAll('*')){
    if(!visible(el))continue;
    const r=el.getBoundingClientRect();
    if(r.right>innerWidth+1||r.left< -1)
      overflow.push({element:label(el),left:r.left,right:r.right,width:r.width});
    const text=[...el.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim();
    if(text&&!el.closest('button:disabled')&&el.tagName!=='SCRIPT'&&el.tagName!=='STYLE'){
      const s=getComputedStyle(el),bg=background(el);
      const raw=el instanceof SVGElement?s.fill:s.color;
      if(!raw.startsWith('rgb'))continue;
      const fg=over(color(raw),bg),l1=lum(fg),l2=lum(bg);
      const ratio=(Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05);
      const big=parseFloat(s.fontSize)>=24||(parseFloat(s.fontSize)>=18.66&&parseFloat(s.fontWeight)>=700);
      const required=big?3:4.5;
      if(ratio+.02<required)contrasts.push({
        element:label(el),text:text.slice(0,70),ratio:+ratio.toFixed(2),required,fg:raw,bg,font:s.fontSize
      });
    }
    if(el.matches('button,input,select')&&!el.disabled&&(r.width<24||r.height<24))
      targets.push({element:label(el),width:+r.width.toFixed(1),height:+r.height.toFixed(1)});
  }
  return {viewport:innerWidth,scrollWidth:document.documentElement.scrollWidth,contrasts,
    overflow:overflow.slice(0,20),smallTargets:targets.slice(0,12),smallTargetCount:targets.length};
}"""


def main():
    destination = ROOT / "dashboard" / "qa"
    destination.mkdir(parents=True, exist_ok=True)
    summary = demo_summary()
    summary["meta"]["synthetic"] = True
    results = []
    with tempfile.TemporaryDirectory(prefix="mifit-synthetic-audit-") as temp:
        source = Path(temp) / "synthetic.html"
        source.write_text(render_html(summary), encoding="utf-8")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="msedge", headless=True, chromium_sandbox=True
            )
            try:
                for width, height, theme in (
                    (320, 740, "light"),
                    (390, 844, "dark"),
                    (1440, 960, "light"),
                    (1920, 1080, "dark"),
                ):
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        color_scheme=theme, offline=True, locale="zh-CN",
                        reduced_motion="reduce", timezone_id="Asia/Shanghai",
                    )
                    page = context.new_page()
                    page.goto(source.as_uri())
                    page.locator('html[data-ready="true"]').wait_for(state="attached")
                    page.screenshot(
                        path=str(destination / f"synthetic-{width}-{theme}.png"),
                        full_page=True,
                    )
                    results.append({"theme": theme, **page.evaluate(AUDIT_JS)})
                    context.close()
            finally:
                browser.close()
    (destination / "synthetic-audit.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
