import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402


class SectorFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MaritimeSARService(Path(self.tmp.name) / "test.db")
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-100", "海燕号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.vessel = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 200, 5
        )
        self.area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "A-10", "surface", 31.0, 122.0, 8, 1
        )
        self.sectors = self.service.split_area("coord1", "coordinator", self.area["id"], 4)

    def tearDown(self):
        self.tmp.cleanup()

    def test_area_splits_into_contiguous_sectors_once(self):
        self.assertEqual(4, len(self.sectors))
        for index, sector in enumerate(self.sectors):
            self.assertEqual(index + 1, sector["sequence"])
            self.assertEqual("A-10-S%02d" % (index + 1), sector["code"])
            self.assertEqual("pending", sector["status"])
        self.assertEqual(0.0, self.sectors[0]["bearing_start"])
        self.assertEqual(360.0, self.sectors[-1]["bearing_end"])
        # sectors are adjacent: end of one equals start of the next
        for prev, nxt in zip(self.sectors, self.sectors[1:]):
            self.assertEqual(prev["bearing_end"], nxt["bearing_start"])
        with self.assertRaises(DomainError):
            self.service.split_area("coord1", "coordinator", self.area["id"], 4)

    def test_coverage_dedupes_by_client_event_id(self):
        first = self.service.report_sector_coverage(
            "field1", "field", self.incident["id"], "cov-1", self.vessel["id"],
            area_id=self.area["id"], sequence=1, source="radar",
        )
        self.assertEqual("accepted", first["status"])
        again = self.service.report_sector_coverage(
            "field1", "field", self.incident["id"], "cov-1", self.vessel["id"],
            area_id=self.area["id"], sequence=1, source="radar",
        )
        self.assertTrue(again["idempotent"])
        self.assertEqual(first["coverage_id"], again["coverage_id"])
        state = self.service.state()
        sector = next(s for s in state["sectors"] if s["sequence"] == 1)
        self.assertEqual("covered", sector["status"])
        records = [c for c in state["sector_coverage"] if c["client_event_id"] == "cov-1"]
        self.assertEqual(1, len(records))

    def test_sea_state_failure_only_queues_review(self):
        weak = self.service.add_asset(
            "coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 15, 200, 1
        )
        result = self.service.report_sector_coverage(
            "field1", "field", self.incident["id"], "cov-sea", weak["id"],
            area_id=self.area["id"], sequence=2,
        )
        self.assertEqual("pending_review", result["status"])
        sector = next(s for s in self.service.state()["sectors"] if s["sequence"] == 2)
        self.assertEqual("pending", sector["status"], "不合格上报不能改变覆盖范围")
        reviews = self.service.list_review_items("pending")
        self.assertEqual(1, len(reviews))
        self.assertEqual("coverage_unqualified", reviews[0]["kind"])
        # coordinator confirms the report despite sea state
        resolved = self.service.resolve_review_item("coord1", "coordinator", reviews[0]["id"], "confirmed", "目视确认")
        self.assertEqual("confirmed", resolved["status"])
        sector = next(s for s in self.service.state()["sectors"] if s["sequence"] == 2)
        self.assertEqual("covered", sector["status"])

    def test_range_failure_only_queues_review_and_reject_keeps_pending(self):
        far = self.service.add_asset(
            "coord1", "coordinator", "近岸艇", "vessel", ["surface"], 35.0, 125.0, 15, 20, 5
        )
        result = self.service.report_sector_coverage(
            "field1", "field", self.incident["id"], "cov-range", far["id"],
            area_id=self.area["id"], sequence=3,
        )
        self.assertEqual("pending_review", result["status"])
        reviews = self.service.list_review_items("pending")
        self.assertEqual(1, len(reviews))
        self.service.resolve_review_item("coord1", "coordinator", reviews[0]["id"], "rejected", "位置不可信")
        sector = next(s for s in self.service.state()["sectors"] if s["sequence"] == 3)
        self.assertEqual("pending", sector["status"])
        self.assertEqual([], self.service.list_review_items("pending"))

    def test_two_coordinators_reassign_only_one_succeeds(self):
        self.service.assign_area(actor="coord1", role="coordinator", area_id=self.area["id"],
                                 asset_id=self.vessel["id"])
        area = next(a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"])
        version = area["version"]
        standby_a = self.service.add_asset(
            "coord1", "coordinator", "救援A", "vessel", ["surface"], 31.01, 122.01, 20, 200, 5
        )
        standby_b = self.service.add_asset(
            "coord1", "coordinator", "救援B", "vessel", ["surface"], 30.99, 121.99, 20, 200, 5
        )
        outcomes = []

        def reassign(asset_id, actor, barrier):
            barrier.wait()
            try:
                result = self.service.reassign_area(actor, "coordinator", self.area["id"], asset_id, version)
                outcomes.append(("ok", result["assigned_asset_id"]))
            except DomainError as exc:
                outcomes.append(("fail", exc.status))

        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=reassign, args=(standby_a["id"], "coord-A", barrier)),
            threading.Thread(target=reassign, args=(standby_b["id"], "coord-B", barrier)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(2, len(outcomes))
        self.assertEqual(1, sum(1 for kind, _ in outcomes if kind == "ok"))
        self.assertEqual(1, sum(1 for kind, status in outcomes if kind == "fail" and status == 409))
        final = next(a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"])
        winner = next(asset_id for kind, asset_id in outcomes if kind == "ok")
        self.assertEqual(winner, final["assigned_asset_id"])
        self.assertNotEqual(version, final["version"])
        # stale version reassign keeps failing
        with self.assertRaises(DomainError) as ctx:
            self.service.reassign_area("coord1", "coordinator", self.area["id"], standby_b["id"], version)
        self.assertEqual(409, ctx.exception.status)

    def test_offline_batch_recomputes_ended_area_and_conflicts_wait_review(self):
        self.service.assign_area(actor="coord1", role="coordinator", area_id=self.area["id"],
                                 asset_id=self.vessel["id"])
        # coverage for sectors 1 and 2 while still active
        self.service.report_sector_coverage("field1", "field", self.incident["id"], "live-1",
                                            self.vessel["id"], area_id=self.area["id"], sequence=1)
        area = next(a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"])
        self.service.complete_area("coord1", "coordinator", self.area["id"], "completed", area["version"])
        # vessel comes back online after the area was ended, with more sweeps
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-sector-1",
            [
                {"type": "sector_coverage", "client_event_id": "off-1", "incident_id": self.incident["id"],
                 "area_id": self.area["id"], "sequence": 2, "asset_id": self.vessel["id"], "source": "radar"},
                {"type": "sector_coverage", "client_event_id": "off-2", "incident_id": self.incident["id"],
                 "area_id": self.area["id"], "sequence": 3, "asset_id": self.vessel["id"], "source": "radar"},
            ],
        )
        self.assertEqual(2, batch["summary"]["conflicts"])
        self.assertEqual(1, len(batch["summary"]["ended_areas_recomputed"]))
        stats = batch["summary"]["ended_areas_recomputed"][0]
        self.assertEqual(4, stats["total_sectors"])
        self.assertEqual(3, stats["covered_sectors"])
        self.assertAlmostEqual(0.75, stats["coverage_ratio"])
        reviews = self.service.list_review_items("pending")
        self.assertTrue(any(item["kind"] == "ended_area_coverage" for item in reviews))
        # replay the same batch: no duplicate coverage / sector records
        replay = self.service.merge_offline_batch("field1", "field", "batch-sector-1", [])
        self.assertTrue(replay["idempotent"])
        coverage_rows = self.service.state()["sector_coverage"]
        self.assertEqual(3, len(coverage_rows))
        sectors = self.service.state()["sectors"]
        self.assertEqual(4, len(sectors))
        self.assertEqual(3, sum(1 for s in sectors if s["status"] == "covered"))

    def test_offline_batch_unqualified_coverage_pending_review(self):
        weak = self.service.add_asset(
            "coord1", "coordinator", "小艇2", "vessel", ["surface"], 31.0, 122.0, 15, 200, 1
        )
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-sector-2",
            [{"type": "sector_coverage", "client_event_id": "off-sea", "incident_id": self.incident["id"],
              "area_id": self.area["id"], "sequence": 4, "asset_id": weak["id"]}],
        )
        self.assertEqual(1, batch["summary"]["pending_review"])
        sector = next(s for s in self.service.state()["sectors"] if s["sequence"] == 4)
        self.assertEqual("pending", sector["status"])


if __name__ == "__main__":
    unittest.main()
