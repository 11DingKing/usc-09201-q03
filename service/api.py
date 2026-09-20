"""证据仓 HTTP JSON 接口。"""

from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .models import BadRequestError, DomainError, NotFoundError
from .storage import Store
from .workflow import EvidenceVault


def _jsonable(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def create_app(store: Store | None = None) -> type[BaseHTTPRequestHandler]:
    """构建绑定指定仓储的处理器类。"""

    vault = EvidenceVault(store)

    class AppHandler(BaseHTTPRequestHandler):
        server_version = "EvidenceVault/0.1"

        # ------------------------------------------------------------ 工具

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(
                _jsonable(payload), ensure_ascii=False
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise BadRequestError("请求体不是合法 JSON", code="bad_json") from None
            if not isinstance(data, dict):
                raise BadRequestError("请求体必须为 JSON 对象")
            return data

        def _require(self, body: dict[str, Any], *keys: str) -> list[Any]:
            values = []
            for key in keys:
                if key not in body:
                    raise BadRequestError(f"缺少字段：{key}")
                values.append(body[key])
            return values

        def log_message(self, format: str, *args: object) -> None:
            return

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parts = [unquote(p) for p in urlsplit(self.path).path.split("/") if p]
                query = parse_qs(urlsplit(self.path).query)
                routes: list[tuple[str, str, Callable[..., Any]]] = []
                if method == "GET":
                    routes = [
                        ("health", self._health),
                        ("valuations", self._get_valuation),
                        ("snapshots", self._get_snapshot),
                        ("parcels", self._parcel_get),
                    ]
                else:
                    routes = [
                        ("appraisers", self._create_appraiser),
                        ("parcels", self._parcel_post),
                        ("valuations", self._valuation_action),
                    ]
                if not parts:
                    raise NotFoundError("资源不存在")
                for prefix, handler in routes:
                    if parts[0] == prefix:
                        handler(parts, query)
                        return
                raise NotFoundError("资源不存在")
            except DomainError as exc:
                self._send_json(exc.status, exc.to_dict())
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    500, {"error": "internal_error", "message": str(exc)}
                )

        # ------------------------------------------------------------ 端点

        def _health(self, parts: list[str], query: dict[str, Any]) -> None:
            if parts == ["health"]:
                self._send_json(200, {"status": "ok"})
            else:
                raise NotFoundError("资源不存在")

        def _create_appraiser(self, parts: list[str], query: Any) -> None:
            body = self._read_body()
            appraiser_id, name, license_no, expires_on = self._require(
                body, "id", "name", "license_no", "qualification_expires_on"
            )
            appraiser = vault.register_appraiser(
                appraiser_id, name, license_no, expires_on
            )
            self._send_json(201, appraiser)

        def _parcel_post(self, parts: list[str], query: Any) -> None:
            body = self._read_body()
            if len(parts) == 1:
                parcel_id, name, location = self._require(
                    body, "id", "name", "location"
                )
                parcel = vault.register_parcel(
                    parcel_id, name, location, body.get("components", [])
                )
                self._send_json(201, parcel)
                return
            if len(parts) == 3 and parts[2] == "evidence":
                material_key, kind, content, season, observed_on, recorded_on, idem = self._require(
                    body,
                    "material_key",
                    "kind",
                    "content",
                    "season",
                    "observed_on",
                    "recorded_on",
                    "idempotency_key",
                )
                evidence = vault.submit_evidence(
                    parts[1],
                    material_key,
                    kind,
                    content,
                    season,
                    observed_on,
                    recorded_on,
                    idem,
                )
                self._send_json(201, evidence)
                return
            if len(parts) == 3 and parts[2] == "market-params":
                values, effective_on = self._require(
                    body, "values", "effective_on"
                )
                params = vault.revise_market_params(
                    parts[1], values, effective_on, body.get("note", "")
                )
                self._send_json(201, params)
                return
            if len(parts) == 3 and parts[2] == "valuations":
                purpose, appraiser_id, season, idem = self._require(
                    body, "purpose", "appraiser_id", "season", "idempotency_key"
                )
                valuation = vault.create_valuation(
                    parts[1],
                    purpose,
                    appraiser_id,
                    season,
                    idem,
                    simulate_failure=bool(body.get("simulate_failure", False)),
                )
                self._send_json(201, valuation)
                return
            raise NotFoundError("资源不存在")

        def _parcel_get(self, parts: list[str], query: Any) -> None:
            if len(parts) == 3 and parts[2] == "conclusion":
                purpose = (query.get("purpose") or [""])[0]
                if not purpose:
                    raise BadRequestError("缺少 purpose 查询参数")
                conclusion = vault.official_conclusion(parts[1], purpose)
                self._send_json(200, conclusion)
                return
            if len(parts) == 3 and parts[2] == "ledger":
                purpose = (query.get("purpose") or [""])[0]
                if not purpose:
                    raise BadRequestError("缺少 purpose 查询参数")
                self._send_json(200, vault.ledger(parts[1], purpose))
                return
            raise NotFoundError("资源不存在")

        def _valuation_action(self, parts: list[str], query: Any) -> None:
            if len(parts) != 3:
                raise NotFoundError("资源不存在")
            valuation_id, action = parts[1], parts[2]
            body = self._read_body()
            if action == "retry":
                valuation = vault.retry_calculation(
                    valuation_id,
                    simulate_failure=bool(body.get("simulate_failure", False)),
                )
                self._send_json(200, valuation)
            elif action == "dispute":
                (reason,) = self._require(body, "reason")
                self._send_json(200, vault.raise_dispute(valuation_id, reason))
            elif action == "reviews":
                reviewer_id, decision = self._require(body, "reviewer_id", "decision")
                valuation = vault.submit_review(
                    valuation_id, reviewer_id, decision, body.get("reason", "")
                )
                self._send_json(200, valuation)
            elif action == "publish":
                (publisher_id,) = self._require(body, "publisher_id")
                conclusion = vault.publish(
                    valuation_id,
                    publisher_id,
                    today=body.get("today"),
                )
                self._send_json(201, conclusion)
            else:
                raise NotFoundError("资源不存在")

        def _get_valuation(self, parts: list[str], query: Any) -> None:
            if len(parts) == 2:
                self._send_json(200, vault.get_valuation(parts[1]))
            elif len(parts) == 3 and parts[2] == "trace":
                self._send_json(200, vault.valuation_trace(parts[1]))
            else:
                raise NotFoundError("资源不存在")

        def _get_snapshot(self, parts: list[str], query: Any) -> None:
            if len(parts) != 2:
                raise NotFoundError("资源不存在")
            self._send_json(200, vault.get_snapshot(parts[1]))

    return AppHandler
