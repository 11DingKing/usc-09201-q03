"""证据仓领域规则验收测试。

以银行抽查场景为主线：一宗含中药材与康养设施的林地，连续修订市场参数、
经历季节补测、计算中断、资格过期、争议双人复核与并发发布，验证：

* 每版价值可复算、引用证据可追溯、发布权限受控；
* 补测只产生新版本，旧结论可解释；
* 重复上传 / 离线补传 / 计算中断 / 并发发布都不会产生两份正式结论。
"""

from __future__ import annotations

import threading
import unittest
from datetime import datetime

from service.calc import CalculationFailure
from service.errors import (
    AuthorizationError,
    CalculationInterruptedError,
    ConflictError,
    PublishBlockedError,
    QualificationExpiredError,
    ReviewRequiredError,
)
from service.models import VersionStatus
from service.repository import Repository
from service.service import (
    ROLE_APPRAISER,
    ROLE_PUBLISHER,
    ROLE_REVIEWER,
    EvidenceVaultService,
)

FIXED_NOW = datetime(2026, 9, 20, 10, 0, 0)


def appraiser(user_id: str = "appr-01") -> dict[str, object]:
    return {"user_id": user_id, "roles": {ROLE_APPRAISER}}


def reviewer(user_id: str) -> dict[str, object]:
    return {"user_id": user_id, "roles": {ROLE_REVIEWER}}


def publisher(user_id: str = "pub-01") -> dict[str, object]:
    return {"user_id": user_id, "roles": {ROLE_PUBLISHER}}


class VaultTest(unittest.TestCase):
    """端到端领域流程测试。"""

    def setUp(self) -> None:
        self.svc = EvidenceVaultService(Repository(), clock=lambda: FIXED_NOW)
        self.parcel = self.svc.register_parcel(
            appraiser(), name="城口县东安镇混合林地", area_mu=100
        )
        self.pid = self.parcel["parcel_id"]

    # ---- 夹具 ---------------------------------------------------------

    def _seed_base_evidence(self) -> dict[str, str]:
        """登记春季现场调查、经营方案、收益记录，返回 id 映射。"""

        survey = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="field_survey",
            source_doc="CK-DC-2026-042",
            title="春季现场调查报告",
            collected_on="2026-03-15",
            season="spring",
            attributes={
                "timber_volume_m3": 500,
                "herb_area_mu": 40,
                "road_status": "晴通雨阻",
            },
        )
        plan = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="management_plan",
            source_doc="CK-JY-2026-007",
            title="森林经营方案（2026-2030）",
            collected_on="2026-01-10",
            season="winter",
            attributes={"obligations": "每年抚育两次、防火巡护"},
        )
        income = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="income_record",
            source_doc="CK-SY-2025-118",
            title="2025 年度林下作物与康养收益台账",
            collected_on="2025-12-31",
            season="winter",
            attributes={"herb_income": 80000, "wellness_income": 50000},
        )
        return {"survey": survey["evidence_id"], "plan": plan["evidence_id"], "income": income["evidence_id"]}

    def _params(self, **overrides: object) -> str:
        kwargs = dict(
            parcel_id=self.pid,
            timber_price=800,
            herb_income_per_mu=2000,
            wellness_annual_income=50000,
            road_accessibility_factor=0.8,
            management_cost_per_mu=100,
            discount_rate=0.05,
            effective_from="2026-01-01",
        )
        kwargs.update(overrides)
        return self.svc.revise_market_parameters(appraiser(), **kwargs)["params_id"]

    def _version(
        self, evidence_ids: list[str], params_id: str, *, expires: str = "2026-12-31"
    ) -> dict[str, object]:
        return self.svc.create_version(
            appraiser(),
            parcel_id=self.pid,
            purpose="bank_financing_2026",
            purpose_label="银行融资抵押估值",
            evidence_ids=evidence_ids,
            params_id=params_id,
            qualification_id="ZG-PG-2024-7788",
            qualification_expires_on=expires,
        )

    # ---- 用例 ---------------------------------------------------------

    def test_healthstyle_full_publish_flow(self) -> None:
        ev = self._seed_base_evidence()
        params_id = self._params()
        version = self._version(
            [ev["survey"], ev["plan"], ev["income"]], params_id
        )
        self.assertEqual(version["version_no"], 1)
        self.assertEqual(version["status"], "frozen")
        self.assertEqual(len(version["evidence_fingerprints"]), 3)

        result = self.svc.calculate_version(appraiser(), version["version_id"])
        # 手工复算：400000 + 1280000 + 800000 - 200000
        self.assertEqual(result["value"], 2_280_000.0)
        self.assertEqual(result["status"], "calculated")
        self.assertFalse(result["disputed"])
        self.assertEqual(result["physical_inputs"]["timber_volume_m3"], 500.0)
        self.assertEqual(result["season"], "spring")

        published = self.svc.publish_version(
            publisher(),
            version["version_id"],
            conclusion_no="CK-JL-2026-0001",
        )
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["conclusion_no"], "CK-JL-2026-0001")

    def test_duplicate_and_offline_uploads_deduplicate(self) -> None:
        first = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="field_survey",
            source_doc="CK-DC-2026-042",
            title="春季现场调查报告",
            collected_on="2026-03-15",
            season="spring",
            attributes={"timber_volume_m3": 500, "herb_area_mu": 40},
        )
        self.assertFalse(first["deduplicated"])

        # 同一材料再次上传（网络重试 / 离线补传）：指纹相同
        second = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="field_survey",
            source_doc="CK-DC-2026-042",
            title="春季现场调查报告",
            collected_on="2026-03-15",
            season="spring",
            attributes={"herb_area_mu": 40, "timber_volume_m3": 500},  # 键序不同
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["evidence_id"], first["evidence_id"])
        self.assertEqual(len(self.svc.list_evidence(self.pid)), 1)

        # 相同幂等键的重发（即使载体异常）也只落一次
        third = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="income_record",
            source_doc="CK-SY-2025-118",
            title="收益台账",
            collected_on="2025-12-31",
            season="winter",
            attributes={"total": 130000},
            idempotency_key="upload-118",
        )
        third_retry = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="income_record",
            source_doc="CK-SY-2025-118",
            title="收益台账",
            collected_on="2025-12-31",
            season="winter",
            attributes={"total": 130000},
            idempotency_key="upload-118",
        )
        self.assertEqual(third_retry["evidence_id"], third["evidence_id"])

    def test_parameter_revision_creates_traceable_new_version(self) -> None:
        ev = self._seed_base_evidence()
        ids = [ev["survey"], ev["plan"], ev["income"]]

        p1 = self._params(timber_price=800)
        v1 = self._version(ids, p1)
        self.svc.calculate_version(appraiser(), v1["version_id"])
        self.svc.publish_version(
            publisher(), v1["version_id"], conclusion_no="CK-JL-2026-0001"
        )

        # 银行抽查期间连续修订市场参数：新参数 → 新版本，旧结论保留
        p2 = self._params(timber_price=860, effective_from="2026-06-01", note="木材价格上行")
        v2 = self._version(ids, p2)
        self.assertEqual(v2["version_no"], 2)
        self.assertEqual(v2["supersedes_version_id"], v1["version_id"])
        self.assertNotEqual(v2["snapshot_hash"], v1["snapshot_hash"])
        # v1 快照内的参数不被污染
        self.assertEqual(v1["params_snapshot"]["timber_price"], 800)
        self.assertEqual(v2["params_snapshot"]["timber_price"], 860)

        result2 = self.svc.calculate_version(appraiser(), v2["version_id"])
        # 林木 500*860=430000，其余不变 → 2310000
        self.assertEqual(result2["value"], 2_310_000.0)

        # 同一目的已有正式结论，未经流程不得再发第二份
        with self.assertRaises(ConflictError):
            self.svc.publish_version(
                publisher(), v2["version_id"], conclusion_no="CK-JL-2026-0002"
            )

        # 旧结论仍然可查、可解释
        current = self.svc.get_published_conclusion(self.pid, "bank_financing_2026")
        self.assertEqual(current["version_id"], v1["version_id"])
        self.assertEqual(current["value"], 2_280_000.0)

    def test_seasonal_supplementary_survey_forms_new_version(self) -> None:
        ev = self._seed_base_evidence()
        p1 = self._params()
        v1 = self._version([ev["survey"], ev["plan"], ev["income"]], p1)
        self.svc.calculate_version(appraiser(), v1["version_id"])

        # 秋季补测：中药材采收面积扩大、道路硬化后可达性提升、管护义务加重
        autumn = self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="field_survey",
            source_doc="CK-DC-2026-077",
            title="秋季补充现场调查",
            collected_on="2026-09-10",
            season="autumn",
            attributes={
                "timber_volume_m3": 505,
                "herb_area_mu": 55,
                "road_status": "道路硬化，全年可达",
            },
        )
        p2 = self._params(
            road_accessibility_factor=1.0,
            management_cost_per_mu=120,
            effective_from="2026-09-01",
        )
        v2 = self._version([autumn["evidence_id"], ev["plan"], ev["income"]], p2)
        result = self.svc.calculate_version(appraiser(), v2["version_id"])
        self.assertEqual(result["season"], "autumn")
        # 差异可由证据与参数版本逐项解释
        self.assertEqual(result["physical_inputs"]["herb_area_mu"], 55.0)
        self.assertEqual(result["physical_inputs"]["road_accessibility_factor"], 1.0)
        # 春季证据与春季版本原样保留
        self.assertEqual(len(self.svc.list_evidence(self.pid)), 4)
        old = self.svc.get_version(v1["version_id"])
        self.assertEqual(old["season"], "spring")
        self.assertEqual(old["value"], 2_280_000.0)

    def test_calculation_interruption_leaves_no_partial_result(self) -> None:
        ev = self._seed_base_evidence()
        version = self._version(
            [ev["survey"], ev["plan"], ev["income"]], self._params()
        )
        vid = version["version_id"]

        calls = {"n": 0}

        def interrupt_once() -> None:
            calls["n"] += 1
            raise CalculationFailure("断电/进程中断")

        with self.assertRaises(CalculationInterruptedError):
            self.svc.calculate_version(appraiser(), vid, interrupter=interrupt_once)

        interrupted = self.svc.get_version(vid)
        self.assertIsNone(interrupted["value"])
        self.assertEqual(interrupted["status"], VersionStatus.CALCULATING.value)

        # 中断后可安全重试并成功
        retried = self.svc.calculate_version(appraiser(), vid)
        self.assertEqual(retried["value"], 2_280_000.0)
        self.assertEqual(retried["status"], "calculated")

    def test_expired_qualification_blocks_publish(self) -> None:
        ev = self._seed_base_evidence()
        version = self._version(
            [ev["survey"], ev["plan"], ev["income"]],
            self._params(),
            expires="2026-08-31",  # 评估资格在发布日（9/20）之前过期
        )
        self.svc.calculate_version(appraiser(), version["version_id"])
        with self.assertRaises(QualificationExpiredError):
            self.svc.publish_version(
                publisher(),
                version["version_id"],
                conclusion_no="CK-JL-2026-0009",
            )
        # 被阻止后版本仍为 calculated，没有产生结论号
        self.assertEqual(
            self.svc.get_version(version["version_id"])["status"], "calculated"
        )

    def test_disputed_value_requires_two_independent_reviews(self) -> None:
        ev = self._seed_base_evidence()
        # 第三方估值与收益法结果偏差超过 20% → 自动标记争议
        self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="third_party_valuation",
            source_doc="CK-DSF-2026-003",
            title="第三方评估机构估值报告",
            collected_on="2026-09-01",
            season="autumn",
            attributes={"reference_value": 1_500_000},
        )
        evidence = self.svc.list_evidence(self.pid)
        ids = [e["evidence_id"] for e in evidence]
        version = self._version(ids, self._params())
        result = self.svc.calculate_version(appraiser(), version["version_id"])
        self.assertTrue(result["disputed"])
        self.assertEqual(result["status"], "in_review")

        # 未完成双人复核前禁止发布
        with self.assertRaises(ReviewRequiredError):
            self.svc.publish_version(
                publisher(), version["version_id"], conclusion_no="CK-JL-2026-0011"
            )

        # 编制人不能复核自己的版本
        with self.assertRaises(AuthorizationError):
            self.svc.submit_review_opinion(
                appraiser("appr-01"), version["version_id"], approved=True
            )

        # 仅一名复核人通过仍不足
        self.svc.submit_review_opinion(
            reviewer("rev-01"), version["version_id"], approved=True, comment="参数可核"
        )
        with self.assertRaises(ReviewRequiredError):
            self.svc.publish_version(
                publisher(), version["version_id"], conclusion_no="CK-JL-2026-0011"
            )

        # 同一复核人不能重复投票
        with self.assertRaises(ConflictError):
            self.svc.submit_review_opinion(
                reviewer("rev-01"), version["version_id"], approved=True
            )

        # 第二名独立复核人通过 → 闸门放行
        self.svc.submit_review_opinion(
            reviewer("rev-02"), version["version_id"], approved=True, comment="差异系季节口径"
        )
        published = self.svc.publish_version(
            publisher(),
            version["version_id"],
            conclusion_no="CK-JL-2026-0011",
        )
        self.assertEqual(published["status"], "published")
        self.assertEqual(published["review"]["result"], "approved")

    def test_review_rejection_forces_new_version(self) -> None:
        ev = self._seed_base_evidence()
        self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="third_party_valuation",
            source_doc="CK-DSF-2026-004",
            title="第三方估值（偏低）",
            collected_on="2026-09-01",
            season="autumn",
            attributes={"reference_value": 1_500_000},
        )
        ids = [e["evidence_id"] for e in self.svc.list_evidence(self.pid)]
        version = self._version(ids, self._params())
        vid = version["version_id"]
        self.svc.calculate_version(appraiser(), vid)
        self.svc.submit_review_opinion(reviewer("rev-01"), vid, approved=True)
        self.svc.submit_review_opinion(
            reviewer("rev-02"), vid, approved=False, comment="中药材面积存疑"
        )
        self.assertEqual(self.svc.get_version(vid)["status"], "review_rejected")
        with self.assertRaises(PublishBlockedError):
            self.svc.publish_version(publisher(), vid, conclusion_no="CK-JL-2026-0021")

    def test_concurrent_publish_yields_single_conclusion(self) -> None:
        ev = self._seed_base_evidence()
        ids = [ev["survey"], ev["plan"], ev["income"]]
        p1, p2 = self._params(), self._params(timber_price=860)
        v1 = self._version(ids, p1)
        v2 = self._version(ids, p2)
        self.svc.calculate_version(appraiser(), v1["version_id"])
        self.svc.calculate_version(appraiser(), v2["version_id"])

        errors: list[Exception] = []

        def publish(version_id: str, conclusion_no: str) -> None:
            try:
                self.svc.publish_version(
                    publisher(), version_id, conclusion_no=conclusion_no
                )
            except Exception as exc:  # noqa: BLE001 - 汇总到列表断言
                errors.append(exc)

        t1 = threading.Thread(target=publish, args=(v1["version_id"], "CK-JL-2026-A"))
        t2 = threading.Thread(target=publish, args=(v2["version_id"], "CK-JL-2026-B"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        published = [
            v
            for v in self.svc.list_versions(self.pid, "bank_financing_2026")
            if v["status"] == "published"
        ]
        self.assertEqual(len(published), 1)

    def test_concurrent_same_conclusion_number_only_one_wins(self) -> None:
        ev = self._seed_base_evidence()
        p1, p2 = self._params(), self._params(timber_price=860)
        v1 = self._version([ev["survey"], ev["plan"], ev["income"]], p1)
        v2 = self._version([ev["survey"], ev["plan"], ev["income"]], p2)
        self.svc.calculate_version(appraiser(), v1["version_id"])
        self.svc.calculate_version(appraiser(), v2["version_id"])
        errors: list[Exception] = []

        def publish(version_id: str) -> None:
            try:
                self.svc.publish_version(
                    publisher(), version_id, conclusion_no="CK-JL-2026-SAME"
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=publish, args=(v1["version_id"],))
        t2 = threading.Thread(target=publish, args=(v2["version_id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            len(
                [
                    v
                    for v in self.svc.list_versions(self.pid, "bank_financing_2026")
                    if v["status"] == "published"
                ]
            ),
            1,
        )

    def test_publish_requires_publisher_role(self) -> None:
        ev = self._seed_base_evidence()
        version = self._version(
            [ev["survey"], ev["plan"], ev["income"]], self._params()
        )
        self.svc.calculate_version(appraiser(), version["version_id"])
        with self.assertRaises(AuthorizationError):
            self.svc.publish_version(
                appraiser(),  # 评估师无发布权限
                version["version_id"],
                conclusion_no="CK-JL-2026-0031",
            )

    def test_frozen_snapshot_ignores_later_evidence_change(self) -> None:
        ev = self._seed_base_evidence()
        version = self._version(
            [ev["survey"], ev["plan"], ev["income"]], self._params()
        )
        # 快照冻结后又补来材料，不影响已冻结版本的引用集合
        self.svc.submit_evidence(
            appraiser(),
            parcel_id=self.pid,
            kind="field_survey",
            source_doc="CK-DC-2026-088",
            title="冬季核查",
            collected_on="2026-11-05",
            season="winter",
            attributes={"timber_volume_m3": 510, "herb_area_mu": 55},
        )
        frozen = self.svc.get_version(version["version_id"])
        self.assertEqual(len(frozen["evidence_ids"]), 3)


if __name__ == "__main__":
    unittest.main()
