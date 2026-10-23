"""Build a portable, private dashboard from local SQLite without cloud access."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from dashboard_data import build_summary, demo_summary, resolve_current_database

ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "dashboard" / "template.html"
WORKER = ROOT / "dashboard" / "import-worker.js"
DETAIL_MODEL = ROOT / "dashboard" / "detail-data.js"
DETAIL_VIEW = ROOT / "dashboard" / "detail-view.js"
DETAIL_STYLES = ROOT / "dashboard" / "detail-view.css"
INTRADAY_MODEL = ROOT / "dashboard" / "intraday-data.js"
INTRADAY_VIEW = ROOT / "dashboard" / "intraday-view.js"
INTRADAY_STYLES = ROOT / "dashboard" / "intraday-view.css"
DEFAULT_OUTPUT = ROOT / "dashboard" / "output" / "健康仪表盘.html"


def render_html(summary: dict) -> str:
    """Embed JSON as inert script text; untrusted values cannot close the element."""
    if not isinstance(summary, dict) or summary.get("schema_version") != 1:
        raise ValueError("不支持的仪表盘数据格式。")
    encoded = json.dumps(summary, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for original, escaped in (
        ("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"),
        ("\u2028", "\\u2028"), ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(original, escaped)
    template = TEMPLATE.read_text(encoding="utf-8")
    markers = ("__DASHBOARD_DATA__", "__IMPORT_WORKER__", "__DETAIL_STYLES__",
               "__DETAIL_SCRIPT__", "__INTRADAY_MODEL__")
    if any(template.count(marker) != 1 for marker in markers):
        raise ValueError("仪表盘模板不完整。")
    record_schema = (ROOT / "dashboard" / "record-schema.json").read_text(encoding="utf-8")
    record_source = (
        "globalThis.MiFitRecordSchema = " + record_schema + ";\n"
        + (ROOT / "dashboard" / "record-data.js").read_text(encoding="utf-8")
    )
    model_source = record_source + "\n" + INTRADAY_MODEL.read_text(encoding="utf-8")
    worker_source = (model_source + "\n" + WORKER.read_text(encoding="utf-8")).encode("utf-8")
    worker_encoded = base64.b64encode(worker_source).decode("ascii")
    detail_script = "\n".join(
        source.read_text(encoding="utf-8") for source in (DETAIL_MODEL, INTRADAY_VIEW, ROOT / "dashboard" / "record-view.js", DETAIL_VIEW)
    ).replace("</script", "<\\/script")
    replacements = {
        "__DASHBOARD_DATA__": encoded,
        "__IMPORT_WORKER__": worker_encoded,
        "__DETAIL_STYLES__": "\n".join(
            source.read_text(encoding="utf-8") for source in (DETAIL_STYLES, INTRADAY_STYLES)
        ),
        "__DETAIL_SCRIPT__": detail_script,
        "__INTRADAY_MODEL__": model_source.replace("</script", "<\\/script"),
    }
    # Substitute template markers once, never markers that happen to occur in data.
    return re.sub("|".join(re.escape(marker) for marker in markers),
                  lambda match: replacements[match.group()], template)


def generate(output: Path = DEFAULT_OUTPUT, *, demo: bool = False, db: Path | None = None) -> Path:
    summary = demo_summary() if demo else build_summary(db or resolve_current_database())
    if demo:
        summary["meta"]["synthetic"] = True
    document = render_html(summary)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".dashboard-{uuid4().hex}.tmp")
    try:
        temporary.write_text(document, encoding="utf-8")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成可离线携带的小米健康仪表盘")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--db", type=Path, help="指定本机 SQLite 缓存（默认当前账号）")
    parser.add_argument("--demo", action="store_true", help="仅使用虚构数据")
    parser.add_argument("--open", action="store_true", help="生成后在系统浏览器打开")
    args = parser.parse_args()
    try:
        result = generate(args.output, demo=args.demo, db=args.db)
    except Exception:  # noqa: BLE001 - never print private record details from build errors.
        print("生成未完成。请先在导出工具中同步数据，并检查目录权限和本地缓存。")
        print("原始缓存与导出文件未改动；未输出健康数据或账号信息。")
        return 1
    print(f"仪表盘已生成：{result}")
    print(f"大小：{result.stat().st_size / 1024:.1f} KB；单文件，无外部依赖。")
    if args.open:
        try:
            os.startfile(str(result))
        except OSError:
            print("浏览器未能自动打开，请双击生成的 HTML 文件。")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
