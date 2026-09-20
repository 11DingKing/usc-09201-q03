"""证据仓 HTTP 接口集成测试：以真实 HTTP 请求走通银行抽查主线。"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime

from service.main import create_server
from service.repository import Repository
from service.service import EvidenceVaultService


def request(
    method: str,
    url: str,
    payload: object | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = EvidenceVaultService(
            Repository(), clock=lambda: datetime(2026, 9, 20)
        )
        self.server = create_server("127.0.0.1", 0, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    APPRAISER = {"X-User-Id": "appr-01", "X-User-Roles": "appraiser"}
    REVIEWER_A = {"X-User-Id": "rev-01", "X-User-Roles": "reviewer"}
    REVIEWER_B = {"X-User-Id": "rev-02", "X-User-Roles": "reviewer"}
    PUBLISHER = {"X-User-Id": "pub-01", "X-User-Roles": "publisher"}

    def _full_flow_with_dispute_and_expiry(self) -> None:
        # 登记宗地
        status, parcel = request(
            "POST", f"{self.base}/parcels",
            {"name": "城口县东安镇混合林地", "area_mu": 100}, self.APPRAISER,
        )
        self.assertEqual(status, 201)
        pid = parcel["parcel_id"]

        # 重复上传同一材料：第二次走 Idempotency-Key 重发，只产生一条证据
        evidence_body = {
            "kind": "field_survey",
            "source_doc": "CK-DC-2026-042",
            "title": "春季现场调查报告",
            "collected_on": "2026-03-15",
            "season": "spring",
            "attributes": {"timber_volume_m3": 500, "herb_area_mu": 40},
        }
        status, first = request(
            "POST", f"{self.base}/parcels/{pid}/evidence",
            evidence_body, {**self.APPRAISER, "Idempotency-Key": "up-042"},
        )
        self.assertEqual(status, 201)
        status, retry = request(
            "POST", f"{self.base}/parcels/{pid}/evidence",
            evidence_body, {**self.APPRAISER, "Idempotency-Key": "up-042"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(retry["evidence_id"], first["evidence_id"])

        for body in (
            {
                "kind": "management_plan",
                "source_doc": "CK-JY-2026-007",
                "title": "经营方案",
                "collected_on": "2026-01-10",
                "season": "winter",
                "attributes": {"obligations": "抚育、防火"},
            },
            {
                "kind": "income_record",
                "source_doc": "CK-SY-2025-118",
                "title": "2025 收益台账",
                "collected_on": "2025-12-31",
                "season": "winter",
                "attributes": {"herb_income": 80000},
            },
            {
                "kind": "third_party_valuation",
                "source_doc": "CK-DSF-2026-003",
                "title": "第三方估值",
                "collected_on": "2026-09-01",
                "season": "autumn",
                "attributes": {"reference_value": 1_500_000},
            },
        ):
            status, _ = request(
                "POST", f"{self.base}/parcels/{pid}/evidence", body, self.APPRAISER
            )
            self.assertEqual(status, 201)

        # 市场参数：连续两版
        params_body = {
            "timber_price": 800, "herb_income_per_mu": 2000,
            "wellness_annual_income": 50000, "road_accessibility_factor": 0.8,
            "management_cost_per_mu": 100, "discount_rate": 0.05,
            "effective_from": "2026-01-01",
        }
        status, p1 = request(
            "POST", f"{self.base}/parcels/{pid}/market-parameters",
            params_body, self.APPRAISER,
        )
        self.assertEqual(status, 201)

        # 无证据角色的匿名调用被拒
        status, err = request(
            "POST",
            f"{self.base}/parcels/{pid}/purposes/bank_financing_2026/versions",
            {"evidence_ids": [], "params_id": p1["params_id"],
             "qualification_id": "Q1", "qualification_expires_on": "2026-12-31"},
            {"X-User-Id": "nobody", "X-User-Roles": ""},
        )
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "forbidden")

        status, evidence_list = request(
            "GET", f"{self.base}/parcels/{pid}/evidence", None, self.APPRAISER
        )
        ids = [e["evidence_id"] for e in evidence_list]

        # v1：资格先有效，后因过期阻断发布
        status, v1 = request(
            "POST",
            f"{self.base}/parcels/{pid}/purposes/bank_financing_2026/versions",
            {"purpose_label": "银行融资抵押估值", "evidence_ids": ids,
             "params_id": p1["params_id"], "qualification_id": "Q1",
             "qualification_expires_on": "2026-08-31"},
            self.APPRAISER,
        )
        self.assertEqual(status, 201)
        status, calc1 = request(
            "POST", f"{self.base}/versions/{v1['version_id']}/calculate",
            {}, self.APPRAISER,
        )
        self.assertEqual(status, 200)
        self.assertEqual(calc1["value"], 2_280_000.0)
        self.assertTrue(calc1["disputed"])
        self.assertEqual(calc1["status"], "in_review")

        # 资格过期 + 争议未复核：资格过期优先阻断
        status, err = request(
            "POST", f"{self.base}/versions/{v1['version_id']}/publish",
            {"conclusion_no": "CK-JL-2026-X1"}, self.PUBLISHER,
        )
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "qualification_expired")

        # v2：资格有效，但争议必须双人复核
        params_body["timber_price"] = 800
        status, v2 = request(
            "POST",
            f"{self.base}/parcels/{pid}/purposes/bank_financing_2026/versions",
            {"purpose_label": "银行融资抵押估值", "evidence_ids": ids,
             "params_id": p1["params_id"], "qualification_id": "Q2",
             "qualification_expires_on": "2026-12-31"},
            self.APPRAISER,
        )
        self.assertEqual(status, 201)
        request("POST", f"{self.base}/versions/{v2['version_id']}/calculate", {}, self.APPRAISER)

        status, err = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/publish",
            {"conclusion_no": "CK-JL-2026-X2"}, self.PUBLISHER,
        )
        self.assertEqual(err["error"], "review_required")

        status, _ = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/reviews",
            {"approved": True, "comment": "可核"}, self.REVIEWER_A,
        )
        self.assertEqual(status, 200)
        status, err = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/publish",
            {"conclusion_no": "CK-JL-2026-X2"}, self.PUBLISHER,
        )
        self.assertEqual(err["error"], "review_required")

        status, _ = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/reviews",
            {"approved": True, "comment": "差异系季节口径"}, self.REVIEWER_B,
        )
        self.assertEqual(status, 200)
        status, published = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/publish",
            {"conclusion_no": "CK-JL-2026-0001",
             }, {**self.PUBLISHER, "Idempotency-Key": "pub-0001"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(published["status"], "published")

        # 发布幂等：同键重发不报错、不产生新结论
        status, republished = request(
            "POST", f"{self.base}/versions/{v2['version_id']}/publish",
            {"conclusion_no": "CK-JL-2026-0001"},
            {**self.PUBLISHER, "Idempotency-Key": "pub-0001"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(republished["conclusion_no"], "CK-JL-2026-0001")

        # 正式结论查询与版本序列可追溯
        status, conclusion = request(
            "GET",
            f"{self.base}/parcels/{pid}/purposes/bank_financing_2026/conclusion",
        )
        self.assertEqual(status, 200)
        self.assertEqual(conclusion["version_id"], v2["version_id"])
        status, versions = request(
            "GET",
            f"{self.base}/parcels/{pid}/purposes/bank_financing_2026/versions",
        )
        self.assertEqual(status, 200)
        self.assertEqual([v["version_no"] for v in versions], [1, 2])

    def test_bank_spot_check_over_http(self) -> None:
        self._full_flow_with_dispute_and_expiry()

    def test_malformed_body(self) -> None:
        req = urllib.request.Request(
            f"{self.base}/parcels",
            data=b"{not-json",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertEqual(json.load(exc)["error"], "validation_error")


if __name__ == "__main__":
    unittest.main()
