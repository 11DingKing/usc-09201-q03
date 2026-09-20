"""证据仓 HTTP 接口端到端测试（银行抽查场景）。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from service.api import create_app
from service.storage import Store


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), create_app(Store()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_bank_spot_check_full_flow(self) -> None:
        status, _ = self.request("POST", "/appraisers", {
            "id": "A1", "name": "李评估", "license_no": "CQ-EV-001",
            "qualification_expires_on": "2030-12-31",
        })
        self.assertEqual(status, 201)

        status, parcel = self.request("POST", "/parcels", {
            "id": "P1", "name": "城口综合样地", "location": "城口县",
            "components": ["中药材", "康养设施"],
        })
        self.assertEqual(status, 201)
        self.assertEqual(parcel["components"], ["中药材", "康养设施"])

        evidence_payloads = [
            ("income", "income_record", {"streams": [
                {"component": "中药材", "annual_net_income": 60000},
                {"component": "康养设施", "annual_net_income": 40000},
            ]}, "spring", "ev-1"),
            ("road", "field_survey", {"road_accessibility_factor": 0.9}, "spring", "ev-2"),
            ("mgmt", "management_plan", {"annual_maintenance_cost": 8000}, "all", "ev-3"),
        ]
        for key, kind, content, season, idem in evidence_payloads:
            status, body = self.request("POST", "/parcels/P1/evidence", {
                "material_key": key, "kind": kind, "content": content,
                "season": season, "observed_on": "2026-03-01",
                "recorded_on": "2026-03-02", "idempotency_key": idem,
            })
            self.assertEqual(status, 201, body)

        # 重复上传同内容：400。
        status, body = self.request("POST", "/parcels/P1/evidence", {
            "material_key": "road", "kind": "field_survey",
            "content": {"road_accessibility_factor": 0.9},
            "season": "spring", "observed_on": "2026-03-01",
            "recorded_on": "2026-03-02", "idempotency_key": "ev-2-copy",
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "duplicate_evidence")

        # 幂等重放：201 返回同一证据。
        status, replay = self.request("POST", "/parcels/P1/evidence", {
            "material_key": "road", "kind": "field_survey",
            "content": {"road_accessibility_factor": 0.9},
            "season": "spring", "observed_on": "2026-03-01",
            "recorded_on": "2026-03-02", "idempotency_key": "ev-2",
        })
        self.assertEqual(status, 201)
        self.assertEqual(replay["version_no"], 1)

        status, params1 = self.request("POST", "/parcels/P1/market-params", {
            "values": {"cap_rate": 0.08, "price_index": {"中药材": 1.0, "康养设施": 1.0}},
            "effective_on": "2026-03-01", "note": "春季参数",
        })
        self.assertEqual(status, 201)
        self.assertEqual(params1["version_no"], 1)

        def create_valuation(idem: str) -> tuple[int, dict]:
            return self.request("POST", "/parcels/P1/valuations", {
                "purpose": "bank_financing", "appraiser_id": "A1",
                "season": "spring", "idempotency_key": idem,
            })

        status, v1 = create_valuation("val-1")
        self.assertEqual(status, 201)
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v1["status"], "ready")
        value_v1 = v1["value"]
        self.assertGreater(value_v1, 0)

        # 同键重复提交（离线补传/重试）：同一版本，不产生第二份结论。
        status, v1_replay = create_valuation("val-1")
        self.assertEqual(status, 201)
        self.assertEqual(v1_replay["id"], v1["id"])

        status, c1 = self.request("POST", f"/valuations/{v1['id']}/publish", {
            "publisher_id": "bank-007",
        })
        self.assertEqual(status, 201)
        self.assertEqual(c1["status"], "official")

        # 重复发布不制造第二份结论。
        status, c1_replay = self.request("POST", f"/valuations/{v1['id']}/publish", {
            "publisher_id": "bank-007",
        })
        self.assertEqual(status, 201)
        self.assertEqual(c1_replay["id"], c1["id"])

        # 连续修订市场参数并出第二版。
        status, params2 = self.request("POST", "/parcels/P1/market-params", {
            "values": {"cap_rate": 0.07, "price_index": {"中药材": 1.12, "康养设施": 1.05}},
            "effective_on": "2026-09-01", "note": "秋季参数",
        })
        self.assertEqual(params2["version_no"], 2)
        status, v2 = create_valuation("val-2")
        self.assertEqual(status, 201)
        self.assertEqual(v2["version_no"], 2)
        self.assertNotEqual(v2["value"], value_v1)
        status, c2 = self.request("POST", f"/valuations/{v2['id']}/publish", {
            "publisher_id": "bank-007",
        })
        self.assertEqual(status, 201)

        # 正式结论指向新版，旧版被取代且可追溯。
        status, official = self.request(
            "GET", "/parcels/P1/conclusion?purpose=bank_financing"
        )
        self.assertEqual(status, 200)
        self.assertEqual(official["id"], c2["id"])

        status, ledger = self.request("GET", "/parcels/P1/ledger?purpose=bank_financing")
        self.assertEqual(status, 200)
        self.assertEqual([v["version_no"] for v in ledger], [1, 2])

        status, trace = self.request("GET", f"/valuations/{v1['id']}/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["conclusion"]["superseded_by"], c2["id"])
        self.assertEqual(trace["conclusion"]["status"], "superseded")
        self.assertEqual(len(trace["snapshot"]["evidence_refs"]), 3)

        status, snapshot = self.request("GET", f"/snapshots/{v1['snapshot_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["params_version"], 1)

    def test_interruption_then_retry_flow(self) -> None:
        self.request("POST", "/appraisers", {
            "id": "A1", "name": "李评估", "license_no": "L1",
            "qualification_expires_on": "2030-01-01",
        })
        self.request("POST", "/parcels", {
            "id": "P1", "name": "样地", "location": "城口", "components": [],
        })
        self.request("POST", "/parcels/P1/evidence", {
            "material_key": "income", "kind": "income_record",
            "content": {"streams": [{"component": "中药材", "annual_net_income": 30000}]},
            "season": "all", "observed_on": "2026-01-01",
            "recorded_on": "2026-01-02", "idempotency_key": "e1",
        })
        status, v = self.request("POST", "/parcels/P1/valuations", {
            "purpose": "mortgage", "appraiser_id": "A1", "season": "summer",
            "idempotency_key": "vx", "simulate_failure": True,
        })
        self.assertEqual(status, 201)
        self.assertEqual(v["status"], "interrupted")
        self.assertIsNone(v["value"])

        status, v2 = self.request("POST", f"/valuations/{v['id']}/retry", {})
        self.assertEqual(status, 200)
        self.assertEqual(v2["status"], "ready")
        self.assertEqual(v2["id"], v["id"])

    def test_expired_qualification_blocked_via_api(self) -> None:
        self.request("POST", "/appraisers", {
            "id": "A9", "name": "过期评估师", "license_no": "L9",
            "qualification_expires_on": "2025-01-01",
        })
        self.request("POST", "/parcels", {
            "id": "P9", "name": "样地", "location": "城口", "components": [],
        })
        self.request("POST", "/parcels/P9/evidence", {
            "material_key": "income", "kind": "income_record",
            "content": {"streams": [{"component": "中药材", "annual_net_income": 10000}]},
            "season": "all", "observed_on": "2026-01-01",
            "recorded_on": "2026-01-02", "idempotency_key": "e9",
        })
        status, v = self.request("POST", "/parcels/P9/valuations", {
            "purpose": "loan", "appraiser_id": "A9", "season": "spring",
            "idempotency_key": "v9",
        })
        self.assertEqual(status, 201)
        status, body = self.request("POST", f"/valuations/{v['id']}/publish", {
            "publisher_id": "bank",
        })
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "qualification_expired")

    def test_health_still_available(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
