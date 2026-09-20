"""林地价值证据仓 HTTP 服务。

路由（JSON 接口；写接口支持 ``Idempotency-Key`` 请求头）：

``POST /parcels``
    登记宗地。
``POST /parcels/<pid>/evidence``
    登记证据（重复材料/离线补传自动去重）。
``GET  /parcels/<pid>/evidence``
    列出宗地全部历史证据（含各季节补测）。
``POST /parcels/<pid>/market-parameters``
    追加一版市场参数（连续修订，不覆盖旧版）。
``POST /parcels/<pid>/purposes/<purpose>/versions``
    按估值目的冻结输入快照，生成新版本。
``GET  /parcels/<pid>/purposes/<purpose>/versions``
    列出该目的下全部版本（价值、证据引用、状态随版本可追溯）。
``GET  /parcels/<pid>/purposes/<purpose>/conclusion``
    获取当前正式结论。
``POST /versions/<vid>/calculate``
    执行价值计算（纯函数，中断可安全重试）。
``POST /versions/<vid>/reviews``
    争议值双人复核意见。
``POST /versions/<vid>/publish``
    通过全部闸门后发布唯一正式结论。
``GET  /versions/<vid>``
    查询版本详情。

调用方身份通过请求头传递：``X-User-Id`` 与逗号分隔的 ``X-User-Roles``
（appraiser / reviewer / publisher）。
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .errors import DomainError
from .repository import Repository
from .service import EvidenceVaultService

_SERVICE = EvidenceVaultService(Repository())


def _split_path(path: str) -> list[str]:
    return [unquote(segment) for segment in urlsplit(path).path.split("/") if segment]


class Handler(BaseHTTPRequestHandler):
    """处理证据仓 HTTP 请求。"""

    server: ThreadingHTTPServer

    # ---- 基础收发 -----------------------------------------------------

    def _write_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any] | None:
        """读取 JSON 请求体；空体返回 ``{}``，解析失败返回 ``None``。"""

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._write_json(
                400, {"error": "validation_error", "message": "请求体必须是 UTF-8 JSON"}
            )
            return None
        if not isinstance(data, dict):
            self._write_json(400, {"error": "validation_error", "message": "请求体必须是对象"})
            return None
        return data

    def _actor(self) -> dict[str, Any]:
        user_id = self.headers.get("X-User-Id", "anonymous")
        roles = {
            role.strip()
            for role in self.headers.get("X-User-Roles", "").split(",")
            if role.strip()
        }
        return {"user_id": user_id, "roles": roles}

    def _idempotency_key(self) -> str | None:
        return self.headers.get("Idempotency-Key")

    # ---- 入口 ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            parts = _split_path(self.path)
            if parts == ["health"]:
                self._write_json(200, {"status": "ok"})
                return
            if len(parts) == 3 and parts[0] == "parcels" and parts[2] == "evidence":
                self._write_json(200, self.service.list_evidence(parts[1]))
                return
            if len(parts) == 5 and parts[0] == "parcels" and parts[2] == "purposes" and parts[4] == "versions":
                self._write_json(
                    200, self.service.list_versions(parts[1], parts[3])
                )
                return
            if len(parts) == 5 and parts[0] == "parcels" and parts[2] == "purposes" and parts[4] == "conclusion":
                self._write_json(
                    200, self.service.get_published_conclusion(parts[1], parts[3])
                )
                return
            if len(parts) == 2 and parts[0] == "versions":
                self._write_json(200, self.service.get_version(parts[1]))
                return
            self._write_json(404, {"error": "not_found", "message": "路径不存在"})
        except DomainError as exc:
            self._write_json(exc.status_code, exc.to_dict())

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_body()
        if body is None:
            return  # JSON 解析失败，错误响应已发出
        try:
            parts = _split_path(self.path)
            actor = self._actor()

            if parts == ["parcels"]:
                self._write_json(
                    201,
                    self.service.register_parcel(
                        actor, name=str(body.get("name", "")), area_mu=body.get("area_mu")
                    ),
                )
                return
            if len(parts) == 3 and parts[0] == "parcels" and parts[2] == "evidence":
                self._write_json(
                    201,
                    self.service.submit_evidence(
                        actor,
                        parcel_id=parts[1],
                        kind=body.get("kind", ""),
                        source_doc=body.get("source_doc", ""),
                        title=body.get("title", ""),
                        collected_on=body.get("collected_on", ""),
                        season=body.get("season", ""),
                        attributes=body.get("attributes"),
                        idempotency_key=self._idempotency_key(),
                    ),
                )
                return
            if len(parts) == 3 and parts[0] == "parcels" and parts[2] == "market-parameters":
                self._write_json(
                    201,
                    self.service.revise_market_parameters(
                        actor,
                        parcel_id=parts[1],
                        timber_price=body.get("timber_price"),
                        herb_income_per_mu=body.get("herb_income_per_mu"),
                        wellness_annual_income=body.get("wellness_annual_income"),
                        road_accessibility_factor=body.get("road_accessibility_factor"),
                        management_cost_per_mu=body.get("management_cost_per_mu"),
                        discount_rate=body.get("discount_rate"),
                        effective_from=body.get("effective_from", ""),
                        note=body.get("note", ""),
                        idempotency_key=self._idempotency_key(),
                    ),
                )
                return
            if len(parts) == 5 and parts[0] == "parcels" and parts[2] == "purposes" and parts[4] == "versions":
                self._write_json(
                    201,
                    self.service.create_version(
                        actor,
                        parcel_id=parts[1],
                        purpose=parts[3],
                        purpose_label=body.get("purpose_label", parts[3]),
                        evidence_ids=body.get("evidence_ids", []),
                        params_id=body.get("params_id", ""),
                        qualification_id=body.get("qualification_id", ""),
                        qualification_expires_on=body.get(
                            "qualification_expires_on", ""
                        ),
                        idempotency_key=self._idempotency_key(),
                    ),
                )
                return
            if len(parts) == 3 and parts[0] == "versions" and parts[2] == "calculate":
                self._write_json(
                    200, self.service.calculate_version(actor, parts[1])
                )
                return
            if len(parts) == 3 and parts[0] == "versions" and parts[2] == "reviews":
                self._write_json(
                    200,
                    self.service.submit_review_opinion(
                        actor,
                        parts[1],
                        approved=bool(body.get("approved", False)),
                        comment=str(body.get("comment", "")),
                    ),
                )
                return
            if len(parts) == 3 and parts[0] == "versions" and parts[2] == "publish":
                self._write_json(
                    200,
                    self.service.publish_version(
                        actor,
                        parts[1],
                        conclusion_no=body.get("conclusion_no", ""),
                        idempotency_key=self._idempotency_key(),
                    ),
                )
                return
            self._write_json(404, {"error": "not_found", "message": "路径不存在"})
        except DomainError as exc:
            self._write_json(exc.status_code, exc.to_dict())

    @property
    def service(self) -> EvidenceVaultService:
        """每个服务可绑定自定义仓储（测试注入），默认使用进程内单例。"""

        return getattr(self.server, "evidence_service", _SERVICE)

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    host: str = "0.0.0.0",
    port: int = 0,
    service: EvidenceVaultService | None = None,
) -> ThreadingHTTPServer:
    """创建可由应用与测试共同使用的服务实例。"""

    server = ThreadingHTTPServer((host, port), Handler)
    if service is not None:
        server.evidence_service = service  # type: ignore[attr-defined]
    return server


def main() -> None:
    """启动服务。"""

    import os

    port = int(os.environ.get("PORT", "3000"))
    server = create_server(port=port)
    print(f"服务已启动：http://0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
