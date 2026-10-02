import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import kbo_analysis as k
from artifact_io import atomic_write_json
from scripts.manage_backup import create_backup, restore_backup, verify_backup
from storage import PredictionStore


class BackupTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = PredictionStore(str(self.root / "live.db"))
        self.calibration = self.root / "calibration.json"
        atomic_write_json(self.calibration, {"enabled": True, "kind": "platt", "slope": 1, "intercept": 0})

    def test_online_backup_captures_committed_wal_without_mutating_the_source(self):
        with self.store.connect() as writer:
            writer.execute("INSERT INTO collector_odds_slots VALUES ('2026-10-02','morning','test')")
            writer.commit()
            self.assertTrue(Path(str(self.store.path) + "-wal").is_file())
            backup = self.root / "backup"
            manifest = create_backup(self.store.path, self.calibration, backup)
            self.assertEqual(manifest["database"]["rows"]["collector_odds_slots"], 1)
            self.assertEqual(writer.execute("SELECT COUNT(*) FROM collector_odds_slots").fetchone()[0], 1)
        self.assertEqual(verify_backup(backup), manifest)
        restored = self.root / "restored"
        restore_backup(backup, restored)
        reopened = PredictionStore(str(restored / "playball.db"))
        self.assertFalse(reopened.claim_odds_slot("2026-10-02", "morning"))
        self.assertEqual(json.loads((restored / "calibration.json").read_text()), json.loads(self.calibration.read_text()))
        with self.assertRaises(FileExistsError):
            restore_backup(backup, restored)

    def test_corrupted_backup_fails_before_creating_a_restore_destination(self):
        backup = self.root / "backup"
        create_backup(self.store.path, self.calibration, backup)
        (backup / "calibration.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "파일 검증 실패"):
            restore_backup(backup, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_failed_backup_leaves_no_partial_folder_and_keeps_the_source(self):
        with patch("scripts.manage_backup.atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                create_backup(self.store.path, self.calibration, self.root / "backup")
        self.assertFalse((self.root / "backup").exists())
        self.assertTrue(self.store.path.exists())


class OddsPersistenceTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "odds-cache.json"
        env = patch.dict("kbo_analysis.os.environ", {"PLAYBALL_ODDS_API_KEY": "private-test-key",
            "PLAYBALL_ODDS_REGIONS": "eu", "PLAYBALL_ODDS_CACHE_PATH": str(self.path),
            "PLAYBALL_ODDS_CACHE_SECONDS": "3600"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.reset)
        self.reset()
        self.now = datetime.now(timezone.utc)
        self.day = (self.now + timedelta(hours=6)).astimezone(k.KST).date().isoformat()
        self.raw = [{"id": "one", "away_team": "LG Twins", "home_team": "Doosan Bears",
            "commence_time": (self.now + timedelta(hours=6)).isoformat(), "bookmakers": [
                {"title": name, "last_update": self.now.isoformat(), "markets": [{"key": "h2h",
                    "outcomes": [{"name": "LG Twins", "price": 1.55}, {"name": "Doosan Bears", "price": 2.7}]}]}
                for name in ("A", "B")]}]

    def reset(self):
        k._odds_restore_key = None
        k._odds_cache.update({"created": 0.0, "regions": None, "credential": None, "eventsByDate": {}})
        k._odds_state.update({"lastFetch": None, "lastError": None, "creditsRemaining": None,
            "creditsUsed": None, "requestCost": None, "eventCount": 0, "cacheRestored": False, "persistenceError": None})

    def populate(self):
        with patch("kbo_analysis._get_json_response", return_value=(self.raw, {"x-requests-remaining": "100"})) as provider:
            first = k.fetch_market_odds(self.day, refresh=True)
            self.assertEqual(provider.call_count, 1)
        return first

    def test_restart_restores_without_a_provider_call_or_exposing_the_key(self):
        first = self.populate()
        self.assertNotIn("private-test-key", self.path.read_text())
        self.reset()
        with patch("kbo_analysis._get_json_response") as provider:
            self.assertEqual(k.fetch_market_odds(self.day), first)
            provider.assert_not_called()
        self.assertTrue(k.odds_provider_status()["cacheRestored"])
        self.assertTrue(k.evaluate_value_bet("LG", "두산", .54, .46, first["one"])["recommendation"])
        with patch("kbo_analysis.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.now + timedelta(minutes=61)
            self.assertFalse(k.evaluate_value_bet("LG", "두산", .54, .46, first["one"])["recommendation"])

    def test_failed_refresh_persists_failure_and_cannot_resurrect_a_success(self):
        self.populate()
        with patch("kbo_analysis._get_json_response", side_effect=OSError("secret must not be logged")):
            self.assertEqual(k.fetch_market_odds(self.day, refresh=True), {})
        self.reset()
        with patch("kbo_analysis._get_json_response") as provider:
            self.assertEqual(k.fetch_market_odds(self.day), {})
            provider.assert_not_called()
        self.assertEqual(k.odds_provider_status()["lastError"], "OSError")
        self.assertEqual(k.odds_provider_status()["eventCount"], 0)
        self.assertFalse(k.odds_provider_status()["cacheRestored"])
        self.assertNotIn("secret must", self.path.read_text())

    def test_changed_credentials_region_and_expired_cache_are_not_restored(self):
        self.populate()
        for variable, value in (("PLAYBALL_ODDS_API_KEY", "another-key"), ("PLAYBALL_ODDS_REGIONS", "us")):
            self.reset()
            with patch.dict("kbo_analysis.os.environ", {variable: value}):
                self.assertEqual(k.fetch_market_odds(self.day), {})
        payload = json.loads(self.path.read_text())
        payload["created"] -= 3601
        atomic_write_json(self.path, payload)
        self.reset()
        self.assertEqual(k.fetch_market_odds(self.day), {})

    def test_nonfinite_provider_price_is_ignored_and_not_recommended(self):
        self.raw[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = float("inf")
        market = self.populate()["one"]
        self.assertEqual(market["teams"]["LG"]["price"], 1.55)
        self.assertFalse(k.evaluate_value_bet("LG", "두산", .54, .46, market)["recommendation"])

    def test_a_failed_persistence_after_provider_failure_leaves_no_old_cache(self):
        self.populate()
        with patch("kbo_analysis._get_json_response", side_effect=OSError("provider")), \
             patch("kbo_analysis.atomic_write_json", side_effect=OSError("disk")):
            self.assertEqual(k.fetch_market_odds(self.day, refresh=True), {})
        self.assertFalse(self.path.exists())
        self.reset()
        self.assertEqual(k.fetch_market_odds(self.day), {})

    def test_corrupt_cache_is_safe_and_an_invalid_target_never_overwrites_calibration(self):
        self.path.write_text("bad json")
        self.assertEqual(k.fetch_market_odds(self.day), {})
        self.assertEqual(k.odds_provider_status()["persistenceError"], "RestoreFailed")
        calibration = self.path.with_name("calibration.json")
        calibration.write_text('{"keep": true}')
        with patch.dict("kbo_analysis.os.environ", {"PLAYBALL_ODDS_CACHE_PATH": str(calibration),
                                                    "PLAYBALL_CALIBRATION_PATH": str(calibration)}):
            with patch("kbo_analysis._get_json_response") as provider:
                self.assertEqual(k.fetch_market_odds(self.day, refresh=True), {})
                provider.assert_not_called()
        self.assertEqual(json.loads(calibration.read_text()), {"keep": True})
