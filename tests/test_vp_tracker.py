import tempfile
import unittest
import copy
from pathlib import Path

import vp_tracker


class ParserTests(unittest.TestCase):
    def test_count_parsers(self):
        cases = [
            ("votes_this_month", "3,601 Votes this month", 3601),
            ("vote_s_count", "Vote(s) 3470", 3470),
            (
                "players_have_voted",
                "3481 Players have voted for this server in June.",
                3481,
            ),
            ("votes_heading_count", "Votes\n3564", 3564),
            ("recent_votes_count", "Recent votes\n2772 in June, 33727 total", 2772),
        ]
        for parser_name, text, expected in cases:
            parsed = vp_tracker.parse_count_source(parser_name, text)
            self.assertEqual(parsed.count, expected)

    def test_recent_username_date_pairs(self):
        text = """Last 10 Voters
Username
Date
PlayerOne
today at 8:21 PM
PlayerTwo
today at 8:10 PM
"""
        parsed = vp_tracker.parse_recent_source("recent_list", "recent_username_dates", text)
        self.assertEqual(len(parsed.events), 2)
        self.assertEqual(parsed.events[0].username, "PlayerOne")
        self.assertIn("recent_list:playerone:", parsed.events[0].external_id)
        self.assertNotIn("today at", parsed.events[0].external_id)
        self.assertIsNotNone(parsed.events[0].vote_time_utc)

    def test_utc_vote_time_to_local_label(self):
        parsed = vp_tracker.parse_vote_time_to_utc("2026-06-20 20:23 UTC")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.tzinfo, vp_tracker.timezone.utc)
        label = vp_tracker.local_time_label(vp_tracker.iso(parsed))
        self.assertIsInstance(label, str)
        self.assertIn("2026", label)

    def test_relative_vote_time_can_use_source_timezone(self):
        reference = vp_tracker.datetime(2026, 6, 21, 2, 40, tzinfo=vp_tracker.timezone.utc)
        parsed = vp_tracker.parse_vote_time_to_utc(
            "today at 2:37 AM", "UTC", reference
        )
        self.assertEqual(parsed, vp_tracker.datetime(2026, 6, 21, 2, 37, tzinfo=vp_tracker.timezone.utc))

    def test_visible_text_strips_scripts(self):
        html = "<html><script>999 Votes this month</script><body><p>3 Votes this month</p></body></html>"
        text = vp_tracker.html_to_visible_text(html)
        self.assertIn("3 Votes this month", text)
        self.assertNotIn("999", text)


class StoreTests(unittest.TestCase):
    def test_vote_delta_wraps_mod_party_size(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.calibrate(119)
                updated = store.apply_vote_delta(2)
                self.assertEqual(updated, 1)
            finally:
                store.close()

    def test_votes_remaining_zero_estimate_means_full_party(self):
        self.assertEqual(vp_tracker.votes_remaining(0, 120), 120)
        self.assertEqual(vp_tracker.votes_remaining(118, 120), 2)

    def test_eta_from_velocity(self):
        self.assertEqual(vp_tracker.eta_from_velocity(10, 2.0), 300)
        self.assertIsNone(vp_tracker.eta_from_velocity(10, 0.0))
        self.assertEqual(vp_tracker.eta_from_velocity(0, 1.0), 0)

    def test_record_poll_cycle_tracks_party_crossing(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                now = vp_tracker.utc_now()
                result = vp_tracker.PollCycleResult(
                    started_at=now,
                    ended_at=now,
                    estimate_before=118,
                    total_delta=3,
                    successes=3,
                    failures=0,
                    count_successes=3,
                    resets=0,
                    large_jumps=0,
                    confidence="high",
                    estimate_after=1,
                    vote_parties_crossed=1,
                    source_results=[],
                )
                store.record_poll_cycle(result)
                events = store.vote_party_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["estimate_before"], 118)
                self.assertEqual(events[0]["estimate_after"], 1)
            finally:
                store.close()

    def test_dashboard_snapshot_has_velocity_windows(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.calibrate(100)
                now = vp_tracker.utc_now()
                result = vp_tracker.PollCycleResult(
                    started_at=now,
                    ended_at=now,
                    estimate_before=100,
                    total_delta=5,
                    successes=2,
                    failures=0,
                    count_successes=2,
                    resets=0,
                    large_jumps=0,
                    confidence="medium",
                    estimate_after=105,
                    vote_parties_crossed=0,
                    source_results=[],
                )
                store.record_poll_cycle(result)
                snapshot = store.dashboard_snapshot()
                self.assertEqual(snapshot.state["estimate"], 100)
                self.assertFalse(snapshot.state["auto_join_enabled"])
                self.assertIn("velocity_windows", snapshot.stats)
                self.assertGreaterEqual(len(snapshot.stats["velocity_windows"]), 4)
            finally:
                store.close()

    def test_source_diagnostics_track_solo_bursts(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                now = vp_tracker.utc_now()
                result = vp_tracker.PollCycleResult(
                    started_at=now,
                    ended_at=now,
                    estimate_before=10,
                    total_delta=3,
                    successes=2,
                    failures=0,
                    count_successes=2,
                    resets=0,
                    large_jumps=0,
                    confidence="high",
                    estimate_after=13,
                    vote_parties_crossed=0,
                    source_results=[
                        vp_tracker.PollSourceResult("source_a", True, delta=3),
                        vp_tracker.PollSourceResult("source_b", True, delta=0),
                    ],
                )
                store.record_poll_cycle(result)
                diagnostics = {
                    row["source"]: row for row in store.source_call_diagnostics()
                }
                self.assertEqual(diagnostics["source_a"]["solo_votes"], 3)
                self.assertEqual(diagnostics["source_a"]["catchup_votes"], 3)
                trace = store.source_delta_trace()
                self.assertEqual(trace[0]["note"], "single-source burst")
                self.assertIn("source_a:3", trace[0]["positive_detail"])
                snapshot = store.dashboard_snapshot()
                self.assertIn("source_debug", snapshot.stats)
                self.assertTrue(snapshot.history["source_delta_trace"])
            finally:
                store.close()

    def test_reader_single_source_burst_is_capped(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        results = [
            vp_tracker.PollSourceResult(
                "reader_source", True, delta=5, fetcher_used="reader"
            ),
            vp_tracker.PollSourceResult("other_source", True, delta=0),
        ]
        vp_tracker.apply_delta_safety(config, results)
        self.assertEqual(results[0].raw_delta, 5)
        self.assertEqual(results[0].delta, 0)
        self.assertEqual(results[0].suppressed_delta, 5)
        self.assertIn("capped", results[0].adjustment_note)

    def test_poll_lock_blocks_second_store(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            first = vp_tracker.Store(db_path, config)
            second = vp_tracker.Store(db_path, config)
            try:
                self.assertTrue(first.acquire_poll_lock(120))
                self.assertFalse(second.acquire_poll_lock(120))
                first.release_poll_lock()
                self.assertTrue(second.acquire_poll_lock(120))
            finally:
                second.release_poll_lock()
                first.close()
                second.close()

    def test_latest_voter_stats_use_local_time(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                event = vp_tracker.ParsedEvent(
                    external_id="source:playerone:2026-06-20t20:23z",
                    username="PlayerOne",
                    vote_time="2026-06-20 20:23 UTC",
                    vote_time_utc="2026-06-20T20:23:00+00:00",
                )
                self.assertTrue(store.insert_event("source", event))
                latest = store.latest_voters()
                stats = store.voter_stats(minutes=1000000)
                self.assertEqual(latest[0]["username"], "PlayerOne")
                self.assertIn("2026", latest[0]["vote_time_local"])
                self.assertEqual(stats[0]["votes_seen"], 1)
            finally:
                store.close()

    def test_calibration_mismatch_log_records_severity(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.calibrate(0, source="seed")
                store.apply_vote_delta(7)
                store.calibrate(5, source="manual-test")
                log = store.calibration_mismatch_log()
                self.assertEqual(len(log), 1)
                self.assertEqual(log[0]["signed_error"], -2)
                self.assertEqual(log[0]["severity"], "minor")
                self.assertIn("overestimated by 2", log[0]["message"])
                snapshot = store.dashboard_snapshot()
                self.assertEqual(len(snapshot.history["calibration_mismatch_log"]), 1)
            finally:
                store.close()

    def test_poll_interval_override_feeds_dashboard_countdown(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.calibrate(10)
                self.assertEqual(
                    store.effective_poll_interval_seconds(10),
                    config["polling"]["normal_interval_seconds"],
                )
                store.set_poll_interval_override_seconds(17)
                due_at = vp_tracker.utc_now() + vp_tracker.timedelta(seconds=17)
                store.set_next_poll_due_at(due_at)
                snapshot = store.dashboard_snapshot()
                self.assertEqual(snapshot.state["poll_interval_seconds"], 17)
                self.assertEqual(snapshot.state["poll_interval_override_seconds"], 17)
                self.assertLessEqual(snapshot.state["next_poll_seconds"], 17)
                self.assertGreater(snapshot.state["next_poll_seconds"], 0)
                store.clear_poll_interval_override_seconds()
                self.assertIsNone(store.poll_interval_override_seconds())
            finally:
                store.close()

    def test_source_failure_backoff_tracks_active_poll_interval(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.calibrate(10)
                self.assertEqual(store.source_failure_backoff_seconds(1), 90)
                self.assertEqual(store.source_failure_backoff_seconds(2), 180)
                store.set_poll_interval_override_seconds(20)
                self.assertEqual(store.source_failure_backoff_seconds(1), 20)

                before = vp_tracker.utc_now()
                source = store.source_rows()[0]
                store.update_source_failure(source)
                updated = store.conn.execute(
                    """
                    SELECT next_allowed_at
                    FROM vote_sources
                    WHERE name = ?
                    """,
                    (source["name"],),
                ).fetchone()
                next_allowed = vp_tracker.parse_iso(updated["next_allowed_at"])
                self.assertIsNotNone(next_allowed)
                self.assertLessEqual((next_allowed - before).total_seconds(), 25)
            finally:
                store.close()

    def test_stale_count_source_inference_is_credited_and_absorbed(self):
        config = copy.deepcopy(vp_tracker.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.conn.execute("UPDATE vote_sources SET enabled = 1")
                for name in (
                    "count_source_1",
                    "count_source_2",
                    "count_source_3",
                    "count_source_4",
                ):
                    store.conn.execute(
                        """
                        UPDATE vote_sources
                        SET last_count = 100
                        WHERE name = ?
                        """,
                        (name,),
                    )
                store.conn.commit()
                store.calibrate(0)

                results = [
                    vp_tracker.PollSourceResult(
                        "count_source_1",
                        True,
                        count=100,
                        delta=0,
                        fetcher_used="reader",
                    ),
                    vp_tracker.PollSourceResult("count_source_2", True, count=103, delta=3),
                    vp_tracker.PollSourceResult("count_source_3", True, count=103, delta=3),
                    vp_tracker.PollSourceResult("count_source_4", True, count=103, delta=3),
                ]
                vp_tracker.reconcile_inferred_source_deltas(store, config, results)
                self.assertEqual(results[0].delta, 3)
                self.assertEqual(results[0].raw_delta, 0)
                self.assertIn("inferred stale count source", results[0].adjustment_note)
                self.assertEqual(store.source_inference_credit("count_source_1"), 3)

                catchup = [
                    vp_tracker.PollSourceResult(
                        "count_source_1",
                        True,
                        count=103,
                        delta=3,
                        fetcher_used="reader",
                    ),
                    vp_tracker.PollSourceResult("count_source_2", True, count=103, delta=0),
                    vp_tracker.PollSourceResult("count_source_3", True, count=103, delta=0),
                    vp_tracker.PollSourceResult("count_source_4", True, count=103, delta=0),
                ]
                vp_tracker.reconcile_inferred_source_deltas(store, config, catchup)
                self.assertEqual(catchup[0].delta, 0)
                self.assertEqual(catchup[0].raw_delta, 3)
                self.assertIn("absorbed inferred credit", catchup[0].adjustment_note)
                self.assertEqual(store.source_inference_credit("count_source_1"), 0)
            finally:
                store.close()

    def test_store_repairs_relative_vote_times_from_source_timezone(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        config["sources"] = [
            {
                "name": "recent_test",
                "url": "https://example.test/",
                "type": "recent",
                "parser": "recent_username_dates",
                "recent_parser": "",
                "fetcher": "http",
                "fallback_fetcher": "",
                "date_timezone": "UTC",
                "enabled": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                store.conn.execute(
                    """
                    INSERT INTO vote_events(
                      source_name, external_id, username, vote_time,
                      vote_time_utc, detected_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "recent_test",
                        "recent_test:playerone:bad",
                        "PlayerOne",
                        "today at 2:37 AM",
                        "2026-06-20T18:37:00+00:00",
                        "2026-06-21T02:38:00+00:00",
                    ),
                )
                store.conn.commit()
                store.repair_relative_vote_times()
                row = store.conn.execute(
                    "SELECT vote_time_utc, external_id FROM vote_events"
                ).fetchone()
                self.assertEqual(row["vote_time_utc"], "2026-06-21T02:37:00+00:00")
                self.assertIn("2026-06-21T02:37:00+00:00", row["external_id"])
            finally:
                store.close()

    def test_store_deduplicates_recanonicalized_utc_events(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        config["sources"] = [
            {
                "name": "recent_total",
                "url": "https://example.test/",
                "type": "count",
                "parser": "recent_votes_count",
                "recent_parser": "recent_username_dates",
                "fetcher": "http",
                "fallback_fetcher": "",
                "date_timezone": "UTC",
                "enabled": True,
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            store = vp_tracker.Store(db_path, config)
            try:
                rows = [
                    (
                        "recent_total",
                        "recent_total:playerone:2026-06-21 02:35 utc",
                        "PlayerOne",
                        "2026-06-21 02:35 UTC",
                        "2026-06-21T02:35:00+00:00",
                        "2026-06-21T02:36:00+00:00",
                    ),
                    (
                        "recent_total",
                        "recent_total:playerone:2026-06-21T02:35:00+00:00",
                        "PlayerOne",
                        "2026-06-21 02:35 UTC",
                        "2026-06-21T02:35:00+00:00",
                        "2026-06-21T02:55:00+00:00",
                    ),
                ]
                store.conn.executemany(
                    """
                    INSERT INTO vote_events(
                      source_name, external_id, username, vote_time,
                      vote_time_utc, detected_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                store.conn.commit()
                store.repair_relative_vote_times()
                repaired = store.conn.execute(
                    """
                    SELECT external_id, detected_at
                    FROM vote_events
                    ORDER BY id
                    """
                ).fetchall()
                self.assertEqual(len(repaired), 1)
                self.assertIn("2026-06-21T02:35:00+00:00", repaired[0]["external_id"])
                self.assertEqual(repaired[0]["detected_at"], "2026-06-21T02:36:00+00:00")
            finally:
                store.close()

    def test_recent_source_first_poll_seeds_without_delta(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        source = {
            "name": "test_recent",
            "url": "https://example.test/",
            "type": "recent",
            "parser": "recent_username_dates",
            "last_checked_at": None,
            "failure_count": 0,
            "next_allowed_at": None,
            "date_timezone": "local",
        }

        class FakeStore:
            def __init__(self):
                self.inserted = 0
                self.snapshots = []
                self.successes = []
                self.conn = self

            def insert_event(self, source_name, event):
                self.inserted += 1
                return True

            def insert_snapshot(self, *args):
                self.snapshots.append(args)

            def update_source_success(self, *args):
                self.successes.append(args)

            def commit(self):
                pass

        original_fetch = vp_tracker.fetch_url
        try:
            vp_tracker.fetch_url = lambda url, cfg, fetcher="http": """
                <p>Last 10 Voters</p>
                <p>PlayerOne</p><p>today at 8:21 PM</p>
            """
            result = vp_tracker.poll_single_source(FakeStore(), config, source)
        finally:
            vp_tracker.fetch_url = original_fetch

        self.assertTrue(result.success)
        self.assertEqual(result.new_events, 1)
        self.assertEqual(result.delta, 0)

    def test_reader_url_for_prepends_reader_base(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        self.assertEqual(
            vp_tracker.reader_url_for("https://example.test/path", config),
            "https://r.jina.ai/https://example.test/path",
        )

    def test_count_source_can_fall_back_to_reader_fetcher(self):
        config = vp_tracker.DEFAULT_CONFIG.copy()
        source = {
            "name": "test_count",
            "url": "https://example.test/",
            "type": "count",
            "parser": "votes_this_month",
            "recent_parser": "",
            "last_count": 2,
            "last_checked_at": "2026-06-20T20:00:00+00:00",
            "failure_count": 0,
            "next_allowed_at": None,
            "fetcher": "http",
            "fallback_fetcher": "reader",
        }

        class FakeStore:
            def __init__(self):
                self.snapshots = []
                self.successes = []
                self.conn = self

            def insert_snapshot(self, *args):
                self.snapshots.append(args)

            def update_source_success(self, *args):
                self.successes.append(args)

            def commit(self):
                pass

        calls = []
        original_fetch = vp_tracker.fetch_url
        try:
            def fake_fetch(url, cfg, fetcher="http"):
                calls.append(fetcher)
                if fetcher == "http":
                    raise vp_tracker.urllib.error.HTTPError(
                        url, 403, "Forbidden", {}, None
                    )
                return "5 Votes this month"

            vp_tracker.fetch_url = fake_fetch
            result = vp_tracker.poll_single_source(FakeStore(), config, source)
        finally:
            vp_tracker.fetch_url = original_fetch

        self.assertEqual(calls, ["http", "reader"])
        self.assertTrue(result.success)
        self.assertEqual(result.count, 5)
        self.assertEqual(result.delta, 3)
        self.assertEqual(result.fetcher_used, "reader")


if __name__ == "__main__":
    unittest.main()
