"""Display a local startup error if dependencies or the GUI cannot initialize."""

import ctypes

try:
    from desktop_app import main
    main()
except Exception:
    ctypes.windll.user32.MessageBoxW(
        None,
        "导出工具未能启动。\n请确认 .venv、应用脚本和系统 Python 完整，"
        "并从项目目录中的“启动小米数据导出.cmd”重试。\n"
        "错误详情未写入日志，以免泄露账号或数据。",
        "小米运动健康数据导出",
        0x10,
    )
    raise SystemExit(1) from None
