"""Start the local sysmonitor analysis workspace."""
import argparse
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from perf_api import create_app


def main():
    # Frozen Python may ignore PYTHONIOENCODING; redirected Windows streams
    # otherwise use a locale encoding that cannot represent Chinese messages.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="sysmonitor 本地离线分析工具")
    parser.add_argument("--port", type=int, default=8765, help="本机服务端口，默认 8765")
    parser.add_argument("--database", type=Path, default=Path.home() / ".sysmonitor" / "sessions.sqlite3",
                        help="SQLite 数据库路径")
    parser.add_argument("--dev", action="store_true", help="允许本机 5173 端口的 Vite 开发代理")
    parser.add_argument("--open-browser", action="store_true", help="服务启动成功后打开浏览器")
    parser.add_argument("--adb", type=Path, help="指定本机 adb 可执行文件的绝对路径")
    args = parser.parse_args()
    if args.adb:
        if not args.adb.is_absolute() or not args.adb.is_file():
            parser.error("--adb 必须为已存在的可执行文件绝对路径")
        os.environ["SYSMONITOR_ADB"] = str(args.adb)
    if not 1024 <= args.port <= 65535:
        parser.error("端口必须在 1024 至 65535 之间")
    app = create_app(args.database, port=args.port, development=args.dev)
    print(f"本地分析：http://127.0.0.1:{args.port}")
    print(f"数据存储：{args.database}")
    if args.dev:
        print("开发模式：在 frontend 目录执行 npm run dev，访问 http://127.0.0.1:5173")
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, proxy_headers=False,
        loop="asyncio", http="h11", ws="none"))
    if args.open_browser:
        def open_when_ready():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not server.should_exit:
                if server.started:
                    webbrowser.open(f"http://127.0.0.1:{args.port}")
                    return
                time.sleep(0.1)
        threading.Thread(target=open_when_ready, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()