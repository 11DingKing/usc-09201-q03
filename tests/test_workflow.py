"""证据仓工作流测试：版本、快照冻结、幂等、资格、双人复核与唯一结论。"""

from __future__ import annotations

import threading
import unittest

from service.models import (
    DomainError,
    ValuationStatus,
)
from service.storage import Store
from service.workflow import EvidenceVault

PARCEL = "P1"
APPRAISER = "A1"
PURPOSE = "bank_financing"


def _seed(vault: EvidenceVault, expires_on: str = "2030-12-31") -> None:
    vault.register_parcel(PARCEL, "城口样地", "城口县", ["中药材", "康养"])
    vault.register_appraiser(APPRAISER, "李评估", "CQ-EV-001", expires_on)


def _full_evidence(vault: EvidenceVault, season: str = "spring", idem: str = "base") -> None:
    vault.submit_evidence(
        PARCEL, "income", "income_record",
        {"streams": [
            {"component": "中药材", "annual_net_income": 60000},
            {"component": "康养设施", "annual_net_income": 40000},
        ]},
        season, "2026-03-01", "2026-03-02", f"ev-income-{idem}",
    )
    vault.submit_evidence(
        PARCEL, "road", "field_survey",
        {"road_accessibility_factor": 0.9},
        season, "2026-03-01", "2026-03-02", f"ev-road-{idem}",
    )
    vault.submit_evidence(
        PARCEL, "mgmt", "management_plan",
        {"annual_maintenance_cost": 8000},
        "all", "2026-01-01", "2026-01-05", f"ev-mgmt-{idem}",
    )


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = EvidenceVault(Store())
        _seed(self.vault)

    # ------------------------------------------------------------ 证据接收

    def test_duplicate_content_rejected_but_idempotent_replay_returns_same(self) -> None:
        vault = self.vault
        content = {"streams": [{"component": "中药材", "annual_net_income": 1000}]}
        first = vault.submit_evidence(
            PARCEL, "k1", "income_record", content,
            "spring", "2026-03-01", "2026-03-02", "key-1",
        )
        # 离线补传重放（同一幂等键）：返回首次证据，不产生新版本。
        replay = vault.submit_evidence(
            PARCEL, "k1", "income_record", content,
            "spring", "2026-03-01", "2026-03-05", "key-1",
        )
        self.assertEqual(first.id, replay.id)
        # 同一材料同一内容换键重传：拒绝，避免重复建档。
        with self.assertRaises(DomainError) as ctx:
            vault.submit_evidence(
                PARCEL, "k1", "income_record", content,
                "spring", "2026-03-01", "2026-03-05", "key-2",
            )
        self.assertEqual(ctx.exception.code, "duplicate_evidence")

    def test_supplemental_measurement_creates_new_version(self) -> None:
        vault = self.vault
        v1 = vault.submit_evidence(
            PARCEL, "road", "field_survey",
            {"road_accessibility_factor": 0.8},
            "spring", "2026-03-01", "2026-03-02", "r1",
        )
        v2 = vault.submit_evidence(
            PARCEL, "road", "field_survey",
            {"road_accessibility_factor": 0.95},
            "summer", "2026-06-01", "2026-06-02", "r2",
        )
        self.assertEqual(v1.version_no, 1)
        self.assertEqual(v2.version_no, 2)
        self.assertNotEqual(v1.content_hash, v2.content_hash)

    # ------------------------------------------------------------ 快照冻结与版本链

    def test_snapshot_freezes_inputs_and_later_uploads_do_not_mutate_it(self) -> None:
        vault = self.vault
        _full_evidence(vault, season="spring", idem="v1")
        vault.revise_market_params(
            PARCEL, {"cap_rate": 0.08, "price_index": {"中药材": 1.0, "康养设施": 1.0}},
            "2026-03-01", "v1参数",
        )
        val1 = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "val-1")
        snap1 = vault.get_snapshot(val1.snapshot_id)
        value_v1 = val1.value

        # 估值后补传夏季道路调查与新收益、修订市场参数：旧快照不变。
        vault.submit_evidence(
            PARCEL, "road", "field_survey",
            {"road_accessibility_factor": 0.6},
            "summer", "2026-07-01", "2026-07-02", "r-summer",
        )
        vault.submit_evidence(
            PARCEL, "income", "income_record",
            {"streams": [
                {"component": "中药材", "annual_net_income": 72000},
                {"component": "康养设施", "annual_net_income": 46000},
            ]},
            "summer", "2026-07-01", "2026-07-02", "inc-summer",
        )
        vault.revise_market_params(
            PARCEL, {"cap_rate": 0.07, "price_index": {"中药材": 1.1, "康养设施": 1.05}},
            "2026-07-01", "v2参数",
        )
        val2 = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "summer", "val-2")
        snap2 = vault.get_snapshot(val2.snapshot_id)

        self.assertNotEqual(snap1.fingerprint, snap2.fingerprint)
        self.assertEqual(vault.get_snapshot(val1.snapshot_id).fingerprint, snap1.fingerprint)
        self.assertEqual(snap1.params_version, 1)
        self.assertEqual(snap2.params_version, 2)
        self.assertEqual(snap1.evidence_refs[0].version_no, 1)
        # 夏季估值引用道路 v2，春季估值仍引用 v1。
        road_snap2 = next(r for r in snap2.evidence_refs if r.material_key == "road")
        road_snap1 = next(r for r in snap1.evidence_refs if r.material_key == "road")
        self.assertEqual(road_snap2.version_no, 2)
        self.assertEqual(road_snap1.version_no, 1)
        self.assertNotEqual(value_v1, val2.value)
        self.assertEqual(val1.version_no, 1)
        self.assertEqual(val2.version_no, 2)

        # 台账保留每版价值。
        ledger = vault.ledger(PARCEL, PURPOSE)
        self.assertEqual([v.version_no for v in ledger], [1, 2])
        self.assertEqual([v.value for v in ledger], [value_v1, val2.value])

    def test_season_selects_only_matching_evidence(self) -> None:
        vault = self.vault
        vault.submit_evidence(
            PARCEL, "income", "income_record",
            {"streams": [{"component": "中药材", "annual_net_income": 50000}]},
            "spring", "2026-03-01", "2026-03-02", "i-spring",
        )
        vault.submit_evidence(
            PARCEL, "income", "income_record",
            {"streams": [{"component": "中药材", "annual_net_income": 80000}]},
            "autumn", "2026-09-01", "2026-09-02", "i-autumn",
        )
        spring = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "vs")
        autumn = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "autumn", "va")
        spring_income = next(
            r for r in vault.get_snapshot(spring.snapshot_id).evidence_refs
            if r.material_key == "income"
        )
        autumn_income = next(
            r for r in vault.get_snapshot(autumn.snapshot_id).evidence_refs
            if r.material_key == "income"
        )
        self.assertEqual(spring_income.content["streams"][0]["annual_net_income"], 50000)
        self.assertEqual(autumn_income.content["streams"][0]["annual_net_income"], 80000)

    def test_missing_income_blocks_calculation(self) -> None:
        vault = self.vault
        vault.submit_evidence(
            PARCEL, "road", "field_survey", {"road_accessibility_factor": 0.9},
            "spring", "2026-03-01", "2026-03-02", "road-only",
        )
        with self.assertRaises(DomainError) as ctx:
            vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "noinc")
        self.assertEqual(ctx.exception.code, "incomplete_snapshot")

    # ------------------------------------------------------------ 中断与幂等

    def test_interrupted_calculation_retries_as_same_version(self) -> None:
        vault = self.vault
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        interrupted = vault.create_valuation(
            PARCEL, PURPOSE, APPRAISER, "spring", "val-x", simulate_failure=True,
        )
        self.assertEqual(interrupted.status, ValuationStatus.INTERRUPTED.value)
        self.assertIsNone(interrupted.value)
        self.assertEqual(interrupted.interruption_count, 1)

        # 再中断一次，版本号仍不变。
        again = vault.retry_calculation(interrupted.id, simulate_failure=True)
        self.assertEqual(again.id, interrupted.id)
        self.assertEqual(again.interruption_count, 2)

        ready = vault.retry_calculation(interrupted.id)
        self.assertEqual(ready.status, ValuationStatus.READY.value)
        self.assertIsNotNone(ready.value)

    def test_concurrent_creation_with_same_idempotency_key_is_one_version(self) -> None:
        vault = self.vault
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(
                    vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "same-key")
                )
            except DomainError as exc:  # pragma: no cover - 不应发生
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 8)
        self.assertEqual({v.id for v in results}, {results[0].id})
        self.assertEqual(len(vault.ledger(PARCEL, PURPOSE)), 1)

    # ------------------------------------------------------------ 资格与发布

    def test_expired_qualification_blocks_publish(self) -> None:
        vault = EvidenceVault(Store())
        _seed(vault, expires_on="2026-08-31")
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        valuation = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v")
        with self.assertRaises(DomainError) as ctx:
            vault.publish(valuation.id, "bank-user")
        self.assertEqual(ctx.exception.code, "qualification_expired")
        # 未发布成功，不存在正式结论。
        with self.assertRaises(DomainError):
            vault.official_conclusion(PARCEL, PURPOSE)

    def test_duplicate_publish_returns_same_conclusion_and_new_version_supersedes(self) -> None:
        vault = self.vault
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        v1 = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v1")
        c1 = vault.publish(v1.id, "publisher")
        c1_again = vault.publish(v1.id, "publisher")
        self.assertEqual(c1.id, c1_again.id)

        # 修订参数后出第二版并发布，旧结论变为 superseded，链条可追溯。
        vault.revise_market_params(PARCEL, {"cap_rate": 0.06}, "2026-09-01")
        v2 = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v2")
        c2 = vault.publish(v2.id, "publisher")
        self.assertNotEqual(c1.id, c2.id)
        self.assertEqual(vault.official_conclusion(PARCEL, PURPOSE).id, c2.id)
        self.assertEqual(c1.status, "superseded")
        self.assertEqual(c1.superseded_by, c2.id)
        self.assertEqual(v1.status, ValuationStatus.PUBLISHED.value)

    # ------------------------------------------------------------ 争议与双人复核

    def test_disputed_value_requires_two_distinct_reviewers(self) -> None:
        vault = self.vault
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        v = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v")

        vault.raise_dispute(v.id, "林农对道路系数有异议")
        self.assertEqual(vault.get_valuation(v.id).status, ValuationStatus.IN_REVIEW.value)
        # 复核未完成禁止发布。
        with self.assertRaises(DomainError) as ctx:
            vault.publish(v.id, "publisher")
        self.assertEqual(ctx.exception.code, "review_pending")

        # 一人通过仍不够。
        vault.submit_review(v.id, "R1", "approve", "口径正确")
        self.assertEqual(vault.get_valuation(v.id).status, ValuationStatus.IN_REVIEW.value)
        # 同一复核人不得重复表决。
        with self.assertRaises(DomainError):
            vault.submit_review(v.id, "R1", "approve")
        # 第二人通过后可发布。
        vault.submit_review(v.id, "R2", "approve", "复核无误")
        self.assertEqual(vault.get_valuation(v.id).status, ValuationStatus.READY.value)
        conclusion = vault.publish(v.id, "publisher")
        self.assertEqual(len(conclusion.reviews), 2)

    def test_any_rejection_blocks_publish(self) -> None:
        vault = self.vault
        _full_evidence(vault)
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        v = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v")
        vault.raise_dispute(v.id, "收益凭证存疑")
        vault.submit_review(v.id, "R1", "approve")
        vault.submit_review(v.id, "R2", "reject", "凭证不足")
        self.assertEqual(vault.get_valuation(v.id).status, ValuationStatus.REJECTED.value)
        with self.assertRaises(DomainError) as ctx:
            vault.publish(v.id, "publisher")
        self.assertEqual(ctx.exception.code, "review_rejected")

    # ------------------------------------------------------------ 可追溯

    def test_trace_links_value_snapshot_evidence_and_conclusion(self) -> None:
        vault = self.vault
        _full_evidence(vault, idem="t")
        vault.revise_market_params(PARCEL, {"cap_rate": 0.08}, "2026-03-01")
        v = vault.create_valuation(PARCEL, PURPOSE, APPRAISER, "spring", "v")
        vault.publish(v.id, "publisher")
        trace = vault.valuation_trace(v.id)
        self.assertEqual(trace["valuation"].value, v.value)
        self.assertEqual(trace["snapshot"].id, v.snapshot_id)
        kinds = {r.kind for r in trace["snapshot"].evidence_refs}
        self.assertIn("income_record", kinds)
        self.assertIn("field_survey", kinds)
        self.assertEqual(trace["conclusion"].value, v.value)
        # breakdown 中保留第三方估值交叉核对口径（若有）。
        self.assertIn("third_party_references", trace["valuation"].breakdown)


if __name__ == "__main__":
    unittest.main()
