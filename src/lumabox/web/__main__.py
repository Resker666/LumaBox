import argparse
import os
import sys
from pathlib import Path

from .config import WebConfig, WebError
from ..limits import DEFAULT_VIDEO_LIMIT_MIB


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lumabox-web", description="LumaBox · 光匣，本机个人下载工作台")
    parser.add_argument("--port", type=int, default=8765, help="本机端口，默认 8765")
    parser.add_argument("--data", type=Path, default=Path("WebData"), help="私有任务和文件目录")
    parser.add_argument("--headless", action="store_true", help="不显示下载浏览器窗口；当前网络需另行验证")
    parser.add_argument("--remote", action="store_true", help="使用云端保存与客户端下载界面；仍只监听本机，公网入口需受保护的反向代理")
    parser.add_argument("--output", type=Path, default=None, help="长期保存目录，默认数据目录旁的 Output")
    parser.add_argument("--video-limit-mib", type=int, default=DEFAULT_VIDEO_LIMIT_MIB, help="单视频上限 MiB，1～8192，默认 1024；网页可逐任务调整")
    args = parser.parse_args(argv)
    try:
        import uvicorn
        from .app import create_app
    except ImportError:
        print('请先安装网页依赖：python -m pip install -e ".[web]"', file=sys.stderr)
        return 1
    try:
        config = WebConfig(args.data, args.port, args.headless, output=args.output, video_limit_mib=args.video_limit_mib, remote_mode=args.remote)
        os.environ.pop("LUMABOX_WEB_PASSWORD", None)  # Discard the obsolete credential, including from child environments.
        app = create_app(config)
    except WebError as exc:
        code = exc.code
        messages = {"INVALID_CONFIG": "端口、保存目录或视频上限不正确；上限须为 1～8192 MiB。"}
        print(messages.get(code, "无法启动光匣，请检查配置。"), file=sys.stderr)
        return 1
    print(f"光匣地址：http://127.0.0.1:{config.port} · 按 Ctrl+C 停止")
    uvicorn.run(app, host="127.0.0.1", port=config.port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
