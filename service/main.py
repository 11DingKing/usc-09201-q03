"""林地价值证据仓 HTTP 服务入口。"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from .api import create_app
from .storage import Store


# 单进程内共享的仓储；正式部署应替换为持久化实现。
store = Store()


def create_server(host: str = "0.0.0.0", port: int = 0) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    return ThreadingHTTPServer((host, port), create_app(store))


def main() -> None:
    """启动服务。"""

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
