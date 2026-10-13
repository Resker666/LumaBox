import argparse
import getpass
import os
import sys
import warnings
from pathlib import Path

from .config import WebConfig, WebError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lumabox-web", description="LumaBox · 光匣，本机个人下载工作台")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    parser.add_argument("--data", type=Path, default=Path("WebData"), help="私有任务和文件目录")
    parser.add_argument("--headless", action="store_true", help="不显示下载浏览器窗口；当前网络需另行验证")
    args = parser.parse_args(argv)
    try:
        import uvicorn
        from .app import create_app
    except ImportError:
        print('请先安装网页依赖：python -m pip install -e ".[web]"', file=sys.stderr)
        return 1
    try:
        config = WebConfig(args.data, args.port, args.headless)
        password = os.environ.pop("LUMABOX_WEB_PASSWORD", None)
        if password is None:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("设置本次访问密码（至少 12 字符，输入不显示）：")
        app = create_app(config, password)
        del password
    except (WebError, getpass.GetPassWarning, EOFError) as exc:
        code = exc.code if isinstance(exc, WebError) else "PASSWORD_INPUT"
        messages = {"INVALID_PASSWORD": "访问密码须为 12～4096 字符。", "INVALID_CONFIG": "端口或数据目录不正确。",
                    "PASSWORD_INPUT": "当前终端无法隐藏输入。请在交互终端运行，或设置 LUMABOX_WEB_PASSWORD。"}
        print(messages.get(code, "无法启动光匣，请检查配置。"), file=sys.stderr)
        return 1
    print(f"光匣地址：http://127.0.0.1:{config.port} · 按 Ctrl+C 停止")
    uvicorn.run(app, host="127.0.0.1", port=config.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
