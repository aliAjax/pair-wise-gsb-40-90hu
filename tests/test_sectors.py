import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import DomainError, MaritimeSARService  # noqa: E402


class SectorCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = MaritimeSARService(Path(self.tmp.name) / "test.db")
        self.incident = self.service.create_incident(
            "coord1", "coordinator", "SAR-101", "远星号", 31.0, 122.0, 10.0, 3, "东海中心"
        )
        self.asset = self.service.add_asset(
            "coord1", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 20, 300, 5
        )
        self.asset_b = self.service.add_asset(
            "coord1", "coordinator", "海巡02", "vessel", ["surface"], 30.5, 121.5, 18, 300, 5
        )
        self.area = self.service.create_search_area(
            "coord1", "coordinator", self.incident["id"], "SEC-A", "surface", 31.0, 122.0, 8, 1
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_area_splits_into_contiguous_sectors_and_is_idempotent(self):
        split = self.service.split_area("coord1", "coordinator", self.area["id"], 8)
        self.assertFalse(split["idempotent"])
        sectors = split["sectors"]
        self.assertEqual(8, len(sectors))
        for prev, cur in zip(sectors, sectors[1:]):
            self.assertAlmostEqual(prev["bearing_end"], cur["bearing_start"])
        self.assertAlmostEqual(0.0, sectors[0]["bearing_start"])
        self.assertAlmostEqual(360.0, sectors[-1]["bearing_end"])
        again = self.service.split_area("coord1", "coordinator", self.area["id"], 8)
        self.assertTrue(again["idempotent"])
        self.assertEqual(8, len(again["sectors"]))
        with self.assertRaises(DomainError):
            self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        state = self.service.state()
        self.assertEqual(8, len(state["sectors"]))
        self.assertEqual(8, state["search_areas"][0]["sector_count"])

    def test_coverage_deduplicated_by_client_report_id(self):
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sector = self.service.list_sectors(self.area["id"])[0]
        first = self.service.submit_coverage("field1", "field", sector["id"], self.asset["id"], "rep-1")
        self.assertTrue(first["applied"])
        second = self.service.submit_coverage("field1", "field", sector["id"], self.asset_b["id"], "rep-1")
        self.assertTrue(second["idempotent"])
        area = [a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"]][0]
        self.assertEqual(1, area["covered_sector_count"])
        refreshed = self.service.list_sectors(self.area["id"])[0]
        self.assertEqual("covered", refreshed["status"])

    def test_sea_state_failure_goes_to_review_without_covering(self):
        # 一艘海况能力不足的船（最大海况 2，事件海况 3）
        weak = self.service.add_asset(
            "coord1", "coordinator", "小艇", "vessel", ["surface"], 31.0, 122.0, 20, 300, 2
        )
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sector = self.service.list_sectors(self.area["id"])[0]
        result = self.service.submit_coverage("field1", "field", sector["id"], weak["id"], "rep-sea")
        self.assertFalse(result["applied"])
        self.assertEqual("sea_state", result["review_reason"])
        sector = self.service.list_sectors(self.area["id"])[0]
        self.assertEqual("pending", sector["status"])
        reviews = self.service.list_reviews("pending")
        self.assertEqual(1, len(reviews))
        self.assertEqual("ineligible", reviews[0]["kind"])
        # 协调员批准后才补记覆盖
        approved = self.service.resolve_coverage_review("coord1", "coordinator", reviews[0]["id"], True)
        self.assertEqual("approved", approved["status"])
        sector = self.service.list_sectors(self.area["id"])[0]
        self.assertEqual("covered", sector["status"])

    def test_range_failure_only_flagged_and_can_be_rejected(self):
        short = self.service.add_asset(
            "coord1", "coordinator", "近岸艇", "vessel", ["surface"], 31.0, 122.0, 20, 0.5, 5
        )
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sector = self.service.list_sectors(self.area["id"])[2]
        result = self.service.submit_coverage("field1", "field", sector["id"], short["id"], "rep-range")
        self.assertFalse(result["applied"])
        self.assertEqual("range", result["review_reason"])
        review = self.service.list_reviews("pending")[0]
        rejected = self.service.resolve_coverage_review("coord1", "coordinator", review["id"], False, "航程不可信")
        self.assertEqual("rejected", rejected["status"])
        self.assertEqual("pending", self.service.list_sectors(self.area["id"])[2]["status"])

    def test_concurrent_reassignment_only_one_succeeds(self):
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sector = self.service.list_sectors(self.area["id"])[1]
        outcomes = []

        def reassign(asset_id):
            try:
                updated = self.service.reassign_sector(
                    "coord-%d" % asset_id, "coordinator", sector["id"], asset_id, sector["version"]
                )
                outcomes.append(("ok", updated["assigned_asset_id"]))
            except DomainError as exc:
                outcomes.append(("conflict", exc.status))

        t1 = threading.Thread(target=reassign, args=(self.asset["id"],))
        t2 = threading.Thread(target=reassign, args=(self.asset_b["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(2, len(outcomes))
        self.assertEqual(1, sum(1 for status, _ in outcomes if status == "ok"))
        self.assertEqual(1, sum(1 for status, _ in outcomes if status == "conflict"))
        final = [s for s in self.service.list_sectors(self.area["id"]) if s["id"] == sector["id"]][0]
        self.assertEqual(final["version"], self.service.list_sectors(self.area["id"])[1]["version"])
        self.assertGreater(final["version"], sector["version"])
        # 旧版本号再次改派必然失败
        with self.assertRaises(DomainError) as ctx:
            self.service.reassign_sector("coord1", "coordinator", sector["id"], self.asset_b["id"], sector["version"])
        self.assertEqual(409, ctx.exception.status)

    def test_offline_recovery_replays_without_duplicates(self):
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sector = self.service.list_sectors(self.area["id"])[0]
        events = [{
            "type": "coverage", "client_event_id": "off-rep-1",
            "sector_id": sector["id"], "asset_id": self.asset["id"],
        }]
        batch = self.service.merge_offline_batch("field1", "field", "batch-sector-1", events)
        self.assertEqual(1, batch["summary"]["accepted"])
        replay = self.service.merge_offline_batch("field1", "field", "batch-sector-1", events)
        self.assertTrue(replay["idempotent"])
        reports = [r for r in self.service.state()["coverage_reports"] if r["client_report_id"] == "off-rep-1"]
        self.assertEqual(1, len(reports))
        self.assertEqual("covered", self.service.list_sectors(self.area["id"])[0]["status"])

    def test_offline_split_replay_does_not_duplicate_sectors(self):
        events = [{"type": "sector_split", "client_event_id": "off-split-1",
                   "area_id": self.area["id"], "sector_count": 6}]
        first = self.service.merge_offline_batch("field1", "field", "batch-split", events)
        self.assertEqual(1, first["summary"]["accepted"])
        self.service.merge_offline_batch("field1", "field", "batch-split", events)
        self.assertEqual(6, len(self.service.list_sectors(self.area["id"])))

    def test_recompute_closed_area_leaves_conflicts_for_review(self):
        self.service.split_area("coord1", "coordinator", self.area["id"], 4)
        sectors = self.service.list_sectors(self.area["id"])
        # 在线覆盖扇区 1、2，然后结束区域
        self.service.submit_coverage("field1", "field", sectors[0]["id"], self.asset["id"], "r-1")
        self.service.submit_coverage("field1", "field", sectors[1]["id"], self.asset["id"], "r-2")
        area = [a for a in self.service.state()["search_areas"] if a["id"] == self.area["id"]][0]
        self.service.complete_area("coord1", "coordinator", self.area["id"], "completed", area["version"])
        # 离线批次补报扇区 3 的覆盖：不能改变已结束区域，只能进冲突复核
        batch = self.service.merge_offline_batch(
            "field1", "field", "batch-after-close",
            [{"type": "coverage", "client_event_id": "r-3", "sector_id": sectors[2]["id"], "asset_id": self.asset["id"]}],
        )
        self.assertEqual(1, batch["summary"]["coverage_conflicts"])
        pending_conflicts = [r for r in self.service.list_reviews("pending") if r["kind"] == "conflict"]
        self.assertEqual(1, len(pending_conflicts))
        self.assertEqual("area_closed", pending_conflicts[0]["reason"])
        # 已结束扇区的覆盖范围没有被改写
        self.assertEqual("pending", self.service.list_sectors(self.area["id"])[2]["status"])
        # 复核确认：区域已结束，批准也只能落为冲突终态，不改覆盖
        kept = self.service.resolve_coverage_review("coord1", "coordinator", pending_conflicts[0]["id"], True)
        self.assertEqual("conflict", kept["status"])


if __name__ == "__main__":
    unittest.main()
