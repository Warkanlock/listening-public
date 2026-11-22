import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import math
import random
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from playback.playback import PlaybackSession


class PlaybackSessionTest(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        tracks = {index: {"id": index, "title": str(index)} for index in (1, 2, 3, 4)}
        self.session = PlaybackSession(lambda playlist: [1, 2, 3] if playlist == "all" else [4],
                                       tracks.get, now=lambda: self.clock[0])

    def test_playback_history_and_clock_survive_queue_edits(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 2})
        self.clock[0] += 12
        self.session.apply({"op": "enqueue", "track_id": 4})
        self.clock[0] += 3
        self.assertEqual(self.session.snapshot()["position"], 15)
        self.session.apply({"op": "next"})
        self.assertEqual(self.session.snapshot()["current_ref"], 4)
        self.session.apply({"op": "previous"})
        self.assertEqual(self.session.snapshot()["current_ref"], 2)
        self.session.apply({"op": "pause"})
        self.clock[0] += 10
        self.assertEqual(self.session.snapshot()["position"], 0)

    def test_manually_queued_songs_appear_before_a_long_automatic_lineup_on_both_devices(self):
        tracks = {index: {"id": index} for index in range(1, 41)}
        session = PlaybackSession(lambda playlist: list(range(1, 39)), tracks.get)
        desktop = ("desktop-a", "Desktop")
        phone = ("phone-abc", "Phone")
        session.apply({"op": "play_available"}, output=desktop)
        session.apply({"op": "enqueue", "track_id": 39}, output=phone)
        session.apply({"op": "enqueue", "track_id": 40}, output=desktop)
        session.apply({"op": "play_next", "track_id": 9}, output=phone)

        for mode in ("shuffle", "shuffle", "genre_flow", "genre_flow"):
            session.apply({"op": mode}, output=desktop)
            self.assertEqual([track["id"] for track in session.snapshot(output=phone)["upcoming"][:3]],
                             [9, 39, 40])
        self.assertEqual(session.snapshot(output=desktop)["queue_length"], 40)
        self.assertEqual(session.apply({"op": "next"}, output=desktop)["current_ref"], 9)
        self.assertEqual([track["id"] for track in session.snapshot(output=phone)["upcoming"][:2]], [39, 40])

    def test_manually_queued_songs_survive_restart_and_queue_edits(self):
        tracks = {index: {"id": index} for index in range(1, 8)}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "player.json"
            available = lambda playlist: [1, 2, 3, 4]
            first = PlaybackSession(available, tracks.get, state_path=path)
            first.apply({"op": "play_available"})
            first.apply({"op": "enqueue", "track_id": 5})
            first.apply({"op": "enqueue", "track_id": 6})
            restored = PlaybackSession(available, tracks.get, state_path=path)
            restored.apply({"op": "queue_remove", "index": 0})
            restored.apply({"op": "enqueue", "track_id": 7})
            snapshot = restored.apply({"op": "shuffle"})
            self.assertEqual([track["id"] for track in snapshot["upcoming"][:2]], [6, 7])

    def test_previous_keeps_a_returned_track_ahead_of_new_manual_additions(self):
        tracks = {index: {"id": index} for index in range(1, 6)}
        session = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get)
        session.apply({"op": "play_available"})
        session.apply({"op": "enqueue", "track_id": 4})
        session.apply({"op": "next"})
        session.apply({"op": "previous"})
        session.apply({"op": "enqueue", "track_id": 5})
        snapshot = session.apply({"op": "shuffle"})
        self.assertEqual([track["id"] for track in snapshot["upcoming"][:2]], [4, 5])

    def test_adjacent_reorder_keeps_the_same_queue_regardless_of_dragged_song(self):
        tracks = {index: {"id": index} for index in range(1, 6)}
        queues = []
        for source, target in ((0, 1), (1, 0)):
            session = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get)
            session.apply({"op": "play_available"})
            session.apply({"op": "enqueue", "track_id": 4})
            session.apply({"op": "queue_move", "index": source, "to": target})
            result = session.apply({"op": "enqueue", "track_id": 5})
            queues.append([track["id"] for track in result["upcoming"]])
        self.assertEqual(queues, [[2, 5, 4, 3], [2, 5, 4, 3]])

    def test_play_routes_to_the_initiating_client_until_outputs_are_selected(self):
        desktop = ("desktop-a", "Desktop")
        phone = ("phone-abc", "Phone")
        tablet = ("tablet-ab", "Tablet")
        self.session.snapshot(output=desktop)
        self.session.snapshot(output=tablet)
        started = self.session.apply({"op": "play_available"}, output=phone)
        self.assertTrue(started["playing"])
        self.assertFalse(self.session.snapshot(output=desktop)["playing"])
        self.assertFalse(self.session.snapshot()["playing"])
        moved = self.session.apply({"op": "resume"}, output=desktop)
        self.assertTrue(moved["playing"])
        self.assertFalse(self.session.snapshot(output=phone)["playing"])

        self.clock[0] += 5
        selection = self.session.apply({"op": "outputs", "devices": [phone[0], tablet[0]]}, output=desktop)
        self.assertFalse(selection["playing"])
        self.assertTrue(selection["session_playing"])
        self.assertTrue(selection["outputs_explicit"])
        self.assertEqual(selection["position"], 5)
        self.assertEqual(selection["occurrence"], started["occurrence"])
        self.assertEqual(selection["queue_length"], started["queue_length"])
        for output in (phone, tablet):
            self.assertTrue(self.session.snapshot(output=output)["playing"])

        end = {"op": "next", "expected_ref": started["current_ref"], "expected_occurrence": started["occurrence"]}
        self.assertEqual(self.session.apply(end, output=desktop)["current_ref"], started["current_ref"])
        self.assertNotEqual(self.session.apply(end, output=tablet)["current_ref"], started["current_ref"])
        self.assertEqual(self.session.apply(end, output=phone)["current_ref"], 2)

        self.session.apply({"op": "pause"}, output=desktop)
        self.session.apply({"op": "resume"}, output=desktop)
        self.assertFalse(self.session.snapshot(output=desktop)["playing"])
        self.assertTrue(self.session.snapshot(output=phone)["playing"])

        self.session.reset()
        switched = self.session.apply({"op": "play_available"}, output=desktop)
        self.assertTrue(switched["playing"])
        self.assertFalse(switched["outputs_explicit"])
        self.assertFalse(self.session.snapshot(output=phone)["playing"])

    def test_departing_sole_listener_parks_the_actual_position_for_another_device(self):
        source = ("desktop-a", "Desktop")
        tablet = ("tablet-ab", "Tablet")
        self.session = PlaybackSession(lambda _: [1, 2], lambda ref: {"id": ref, "duration": 11} if ref else None,
                                       now=lambda: self.clock[0])
        started = self.session.apply({"op": "play_available"}, output=source)
        chosen = self.session.apply({"op": "outputs", "devices": [source[0]]}, output=source)
        self.clock[0] += 15
        park = {"op": "park", "position": 12.345, "expected_version": chosen["version"],
                "expected_ref": chosen["current_ref"], "expected_occurrence": chosen["occurrence"]}
        self.assertTrue(self.session.apply(park, output=tablet)["session_playing"])
        self.assertTrue(self.session.apply({**park, "expected_version": started["version"]}, output=source)["playing"])
        parked = self.session.apply(park, output=source)
        self.assertFalse(parked["session_playing"])
        self.assertEqual(parked["parked_at"], self.clock[0])
        self.clock[0] += 60
        resumed = self.session.apply({"op": "resume"}, output=tablet)
        self.assertTrue(resumed["playing"])
        self.assertEqual(resumed["position"], 12.345)
        self.assertEqual(resumed["current_ref"], started["current_ref"])
        self.assertEqual(resumed["queue_length"], started["queue_length"])

    def test_departing_one_of_several_listeners_leaves_them_playing(self):
        desktop = ("desktop-a", "Desktop")
        tablet = ("tablet-ab", "Tablet")
        self.session.snapshot(output=tablet)
        self.session.apply({"op": "play_available"}, output=desktop)
        both = self.session.apply({"op": "outputs", "devices": [desktop[0], tablet[0]]}, output=desktop)
        self.session.apply({"op": "park", "position": 1, "expected_version": both["version"],
                            "expected_ref": both["current_ref"], "expected_occurrence": both["occurrence"]},
                           output=desktop)
        self.assertTrue(self.session.snapshot(output=tablet)["playing"])

    def test_current_output_leave_parks_and_rejects_late_playback_requests(self):
        desktop = ("desktop-a", "Desktop", 1)
        reopened = (*desktop[:2], 2)
        tablet = ("tablet-ab", "Tablet")
        started = self.session.apply({"op": "play_available"}, output=desktop)
        left = self.session.apply({"op": "leave", "position": 1.25, "expected_version": started["version"],
                                   "expected_ref": started["current_ref"], "expected_occurrence": started["occurrence"]},
                                  output=desktop)
        self.assertFalse(left["session_playing"])
        self.assertEqual(left["position"], 1.25)
        self.assertFalse(self.session.snapshot(output=desktop)["outputs"])
        self.assertFalse(self.session.apply({"op": "next"}, output=desktop)["session_playing"])
        self.assertTrue(self.session.snapshot(output=reopened)["outputs"])
        self.session.apply({"op": "leave"}, output=desktop)
        resumed = self.session.apply({"op": "resume"}, output=tablet)
        self.assertTrue(resumed["playing"])
        self.assertEqual(resumed["current_ref"], started["current_ref"])
        self.assertEqual(resumed["position"], 1.25)
        self.assertEqual({item["id"] for item in resumed["outputs"]}, {desktop[0], tablet[0]})

    def test_leaving_position_survives_reload_ordering_and_storage_retry(self):
        desktop = ("desktop-a", "Desktop", 1)
        reopened = (*desktop[:2], 2)
        started = self.session.apply({"op": "play_available"}, output=desktop)
        leave = {"op": "leave", "position": 7.25, "expected_version": started["version"],
                 "expected_ref": started["current_ref"], "expected_occurrence": started["occurrence"]}
        self.session.snapshot(output=reopened)
        with mock.patch.object(self.session, "_save", side_effect=OSError):
            with self.assertRaises(OSError):
                self.session.apply(leave, output=desktop)
        parked = self.session.apply(leave, output=desktop)
        self.assertFalse(parked["session_playing"])
        self.assertEqual(parked["position"], 7.25)
        self.assertEqual([item["id"] for item in parked["outputs"]], [desktop[0]])

    def test_solo_listener_can_park_after_its_heartbeat_has_expired(self):
        desktop = ("desktop-a", "Desktop", 1)
        started = self.session.apply({"op": "play_available"}, output=desktop)
        self.clock[0] += 121
        parked = self.session.apply({"op": "leave", "position": 11.25, "expected_version": started["version"],
                                     "expected_ref": started["current_ref"], "expected_occurrence": started["occurrence"]},
                                    output=desktop)
        self.assertFalse(parked["session_playing"])
        self.assertEqual(parked["position"], 11.25)
        self.assertFalse(parked["outputs"])

    def test_one_output_leaving_keeps_other_audio_and_reload_restores_its_selection(self):
        desktop = ("desktop-a", "Desktop", 1)
        tablet = ("tablet-ab", "Tablet", 1)
        self.session.snapshot(output=tablet)
        self.session.apply({"op": "play_available"}, output=desktop)
        both = self.session.apply({"op": "outputs", "devices": [desktop[0], tablet[0]]}, output=desktop)
        self.session.apply({"op": "leave", "position": 1, "expected_version": both["version"],
                            "expected_ref": both["current_ref"], "expected_occurrence": both["occurrence"]},
                           output=desktop)
        continued = self.session.snapshot(output=tablet)
        self.assertTrue(continued["playing"])
        self.assertEqual(continued["outputs"], [{"id": tablet[0], "name": tablet[1], "selected": True}])
        self.assertTrue(self.session.snapshot(output=(*desktop[:2], 2))["playing"])

    def test_manual_skip_routes_automatically_but_track_boundary_keeps_outputs(self):
        desktop = ("desktop-a", "Desktop")
        phone = ("phone-abc", "Phone")
        first = self.session.apply({"op": "play_available"}, output=phone)
        advance = {"op": "next", "expected_ref": first["current_ref"], "expected_occurrence": first["occurrence"]}
        second = self.session.apply(advance, output=phone)
        self.assertTrue(second["playing"])
        self.assertFalse(self.session.snapshot(output=desktop)["playing"])
        third = self.session.apply({"op": "next"}, output=desktop)
        self.assertTrue(third["playing"])
        self.assertFalse(self.session.snapshot(output=phone)["playing"])

    def test_disconnected_outputs_can_be_replaced_without_touching_the_queue(self):
        desktop = ("desktop-a", "Desktop")
        phone = ("phone-abc", "Phone")
        self.session.snapshot(output=phone)
        self.session.apply({"op": "outputs", "devices": [phone[0]]}, output=desktop)
        first = self.session.apply({"op": "play_available"}, output=desktop)
        self.clock[0] += 121
        with self.assertRaisesRegex(ValueError, "connected playback"):
            self.session.apply({"op": "outputs", "devices": [phone[0]]}, output=desktop)
        recovered = self.session.apply({"op": "resume"}, output=desktop)
        self.assertTrue(recovered["playing"])
        self.assertFalse(recovered["outputs_explicit"])
        self.assertEqual(recovered["occurrence"], first["occurrence"])

    def test_queue_reorder_remove_and_play_next(self):
        self.session.apply({"op": "play_available", "playlist": "all"})
        self.session.apply({"op": "play_next", "track_id": 4})
        snapshot = self.session.snapshot()
        self.assertEqual([track["id"] for track in snapshot["upcoming"]], [4, 2, 3])
        self.session.apply({"op": "queue_move", "index": 2, "to": 0,
                            "expected_version": snapshot["version"]})
        snapshot = self.session.snapshot()
        self.assertEqual([track["id"] for track in snapshot["upcoming"]], [3, 4, 2])
        self.session.apply({"op": "queue_remove", "index": 1,
                            "expected_version": snapshot["version"]})
        self.assertEqual([track["id"] for track in self.session.snapshot()["upcoming"]], [3, 2])

    def test_stale_queue_edit_does_not_change_another_device_order(self):
        self.session.apply({"op": "play_available"})
        version = self.session.snapshot()["version"]
        self.session.apply({"op": "play_next", "track_id": 4})
        with self.assertRaisesRegex(ValueError, "another device"):
            self.session.apply({"op": "queue_remove", "index": 0, "expected_version": version})
        self.assertEqual(self.session.snapshot()["upcoming"][0]["id"], 4)

    def test_invalid_actions_and_reset(self):
        with self.assertRaisesRegex(ValueError, "Choose a song"):
            self.session.apply({"op": "seek", "position": 5})
        self.session.apply({"op": "play_available"})
        with self.assertRaisesRegex(ValueError, "valid playback"):
            self.session.apply({"op": "seek", "position": math.nan})
        self.session.reset()
        self.assertIsNone(self.session.snapshot()["current"])
        self.assertEqual(self.session.snapshot()["queue_length"], 0)

    def test_two_clients_ending_the_same_occurrence_advance_only_once(self):
        first = self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.session.apply({"op": "play_next", "track_id": 1})
        end = {"op": "next", "expected_ref": 1, "expected_occurrence": first["occurrence"]}
        duplicate = self.session.apply(end)
        self.assertEqual(duplicate["current_ref"], 1)
        self.assertNotEqual(duplicate["occurrence"], first["occurrence"])
        self.assertEqual(self.session.apply(end), duplicate)
        self.session.apply({"op": "seek", "position": 90})
        following = self.session.apply({**end, "expected_occurrence": duplicate["occurrence"]})
        self.assertEqual(following["current_ref"], 2)

    def test_stale_expected_ref_next_is_ignored(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.session.apply({"op": "next", "expected_ref": 1})
        before = self.session.snapshot()
        self.assertEqual(before["current_ref"], 2)
        stale = self.session.apply({"op": "next", "expected_ref": 1})
        self.assertEqual(stale["current_ref"], 2)
        self.assertEqual(stale["version"], before["version"])

    def test_shuffle_off_restores_original_order(self):
        tracks = {index: {"id": index} for index in range(1, 7)}
        session = PlaybackSession(lambda playlist: [1, 2, 3, 4, 5, 6], tracks.get,
                                  now=lambda: self.clock[0])
        session.apply({"op": "play_available"})
        self.assertEqual([track["id"] for track in session.snapshot()["upcoming"]],
                         [2, 3, 4, 5, 6])
        random.seed(20261116)
        session.apply({"op": "shuffle"})
        shuffled = [track["id"] for track in session.snapshot()["upcoming"]]
        self.assertEqual(sorted(shuffled), [2, 3, 4, 5, 6])
        session.apply({"op": "shuffle"})
        self.assertEqual([track["id"] for track in session.snapshot()["upcoming"]],
                         [2, 3, 4, 5, 6])

    def test_jump_splices_queue_and_records_history(self):
        tracks = {index: {"id": index} for index in range(1, 6)}
        session = PlaybackSession(lambda playlist: [1, 2, 3, 4, 5], tracks.get,
                                  now=lambda: self.clock[0])
        session.apply({"op": "play_available"})
        self.clock[0] += 30
        snapshot = session.apply({"op": "jump", "index": 2})
        self.assertEqual(snapshot["current_ref"], 4)
        self.assertEqual([track["id"] for track in snapshot["upcoming"]], [5])
        self.assertEqual(snapshot["position"], 0)
        self.assertTrue(snapshot["playing"])
        # History holds the old current and the skipped tracks in order:
        # stepping back walks 3, then 2, then 1.
        self.assertEqual(session.apply({"op": "previous"})["current_ref"], 3)
        self.assertEqual(session.apply({"op": "previous"})["current_ref"], 2)
        self.assertEqual(session.apply({"op": "previous"})["current_ref"], 1)

    def test_corrupt_playback_file_is_ignored_and_unresolvable_current_dropped(self):
        tracks = {index: {"id": index} for index in (1, 2, 3)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "playback.json"
            path.write_text("{not json", encoding="utf-8")
            session = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get,
                                      now=lambda: self.clock[0], state_path=path)
            self.assertIsNone(session.snapshot()["current"])
            self.assertEqual(session.snapshot()["queue_length"], 0)
            path.write_text(json.dumps({"version": 4}), encoding="utf-8")
            session = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get,
                                      now=lambda: self.clock[0], state_path=path)
            self.assertIsNone(session.snapshot()["current"])
            self.assertEqual(session.snapshot()["queue_length"], 0)
            saved = {"version": 4, "current": 99, "upcoming": [], "history": [],
                     "order": [], "shuffle": False, "playing": True,
                     "position": 12.0, "changed_at": self.clock[0]}
            path.write_text(json.dumps(saved), encoding="utf-8")
            session = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get,
                                      now=lambda: self.clock[0], state_path=path)
            snapshot = session.snapshot()
            self.assertIsNone(snapshot["current"])
            self.assertIsNone(snapshot["current_ref"])
            self.assertFalse(snapshot["playing"])

    def test_next_at_end_stops_and_previous_without_history_holds_position(self):
        self.session.apply({"op": "play_available", "playlist": "other"})
        self.clock[0] += 45
        snapshot = self.session.apply({"op": "next"})
        self.assertEqual(snapshot["current_ref"], 4)
        self.assertFalse(snapshot["playing"])
        self.assertEqual(snapshot["position"], 0)
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.clock[0] += 25
        snapshot = self.session.apply({"op": "previous"})
        self.assertEqual(snapshot["current_ref"], 1)
        self.assertEqual(snapshot["position"], 25)
        self.clock[0] += 5
        self.assertEqual(self.session.snapshot()["position"], 30)

    def test_lyric_seek_only_moves_the_expected_track(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        current = self.session.apply({"op": "seek", "expected_ref": 1, "position": 24})
        self.assertEqual(current["position"], 24)
        stale = self.session.apply({"op": "seek", "expected_ref": 2, "position": 15})
        self.assertEqual(stale["position"], 24)
        self.assertEqual(stale["version"], current["version"])

    def test_seek_clamps_and_holds_while_paused(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertEqual(self.session.apply({"op": "seek", "position": -30})["position"], 0)
        self.assertEqual(self.session.apply({"op": "seek", "position": 25 * 3600})["position"],
                         24 * 3600)
        self.session.apply({"op": "pause"})
        self.clock[0] += 600
        self.assertEqual(self.session.snapshot()["position"], 24 * 3600)

    def test_snapshot_truncates_upcoming_but_reports_full_queue_length(self):
        tracks = {index: {"id": index} for index in range(1, 27)}
        session = PlaybackSession(lambda playlist: list(range(1, 27)), tracks.get,
                                  now=lambda: self.clock[0])
        session.apply({"op": "play_available"})
        snapshot = session.snapshot()
        self.assertEqual(len(snapshot["upcoming"]), 20)
        self.assertEqual(snapshot["queue_length"], 25)

    def test_play_request_and_unready_enqueue_rejections(self):
        tracks = {index: {"id": index} for index in (1, 2, 3)}

        def resolve(ref):
            if ref == "custom-7":
                return {"id": ref}
            return tracks.get(ref)

        session = PlaybackSession(lambda playlist: [1, 2, 3], resolve,
                                  now=lambda: self.clock[0])
        snapshot = session.apply({"op": "play_request", "request_id": 7})
        self.assertEqual(snapshot["current_ref"], "custom-7")
        self.assertTrue(snapshot["playing"])
        with self.assertRaises(ValueError):
            session.apply({"op": "play_request", "request_id": 8})
        with self.assertRaises(ValueError):
            session.apply({"op": "play_request", "request_id": "7"})
        with self.assertRaises(ValueError):
            session.apply({"op": "enqueue", "track_id": 99})
        with self.assertRaises(ValueError):
            session.apply({"op": "play_next", "track_id": 99})

    def test_toggle_and_resume_semantics(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.assertTrue(self.session.snapshot()["playing"])
        self.assertFalse(self.session.apply({"op": "toggle"})["playing"])
        self.assertTrue(self.session.apply({"op": "toggle"})["playing"])
        self.clock[0] += 10
        self.assertEqual(self.session.apply({"op": "pause"})["position"], 10)
        self.clock[0] += 50
        self.assertEqual(self.session.snapshot()["position"], 10)
        self.assertTrue(self.session.apply({"op": "resume"})["playing"])
        self.assertTrue(self.session.apply({"op": "resume"})["playing"])
        self.clock[0] += 5
        self.assertEqual(self.session.snapshot()["position"], 15)

    def test_saved_queue_resumes_and_invalid_action_preserves_clock(self):
        tracks = {index: {"id": index} for index in (1, 2, 3)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "playback.json"
            first = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get,
                                    now=lambda: self.clock[0], state_path=path)
            first.apply({"op": "play_available"})
            self.clock[0] += 10
            with self.assertRaisesRegex(ValueError, "no longer"):
                first.apply({"op": "queue_remove", "index": 12})
            self.assertEqual(first.snapshot()["position"], 10)
            first.apply({"op": "queue_move", "index": 1, "to": 0})
            resumed = PlaybackSession(lambda playlist: [1, 2, 3], tracks.get,
                                      now=lambda: self.clock[0], state_path=path)
            self.assertEqual(resumed.snapshot()["current_ref"], 1)
            self.assertEqual([track["id"] for track in resumed.snapshot()["upcoming"]], [3, 2])
            self.assertEqual(resumed.snapshot()["position"], 10)


class PlayEventTests(unittest.TestCase):
    """Plan 045: play boundaries emit events through the recorder.

    The recorder receives (kind, ref, device_id, listened); position
    ticks, pauses, seeks, and queue edits emit nothing.
    """

    def setUp(self):
        self.clock = [100.0]
        self.events = []
        tracks = {1: {"id": 1, "duration": 100.0}, 2: {"id": 2, "duration": 200.0},
                  3: {"id": 3, "duration": 300.0}, 4: {"id": 4, "duration": 50.0}}
        self.session = PlaybackSession(
            lambda playlist: [1, 2, 3] if playlist == "all" else [4],
            tracks.get, now=lambda: self.clock[0],
            record_event=lambda *args: self.events.append(args))

    def test_natural_end_completes_and_opens_the_next_track(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.clock[0] += 40
        self.session.apply({"op": "next", "expected_ref": 1})
        self.assertEqual(self.events, [("started", 1, None, None),
                                       ("completed", 1, None, 40.0),
                                       ("started", 2, None, None)])

    def test_bare_next_skips_with_the_listened_position(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.clock[0] += 30
        self.session.apply({"op": "next"})
        self.assertEqual(self.events, [("started", 1, None, None),
                                       ("skipped", 1, None, 30.0),
                                       ("started", 2, None, None)])

    def test_skip_near_the_end_reads_as_completed_by_position(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.clock[0] += 98
        self.session.apply({"op": "next"})
        self.assertEqual(self.events[1], ("completed", 1, None, 98.0))

    def test_queue_end_closes_without_opening_and_replay_opens_again(self):
        self.session.apply({"op": "play_available", "playlist": "other"})
        self.clock[0] += 50
        self.session.apply({"op": "next", "expected_ref": 4})
        self.assertEqual(self.events, [("started", 4, None, None),
                                       ("completed", 4, None, 50.0)])
        self.session.apply({"op": "toggle"})
        self.assertEqual(self.events[-1], ("started", 4, None, None))

    def test_buffered_tracks_rejoin_in_order_and_close_at_the_end(self):
        first = self.session.apply({"op": "play_available"})
        action = {"op": "resume_buffer", "expected_version": first["version"],
                  "expected_ref": first["current_ref"], "expected_occurrence": first["occurrence"],
                  "advances": [{"position": 100, "natural": True}, {"position": 12, "natural": False}],
                  "finished": {"position": 300, "natural": True}, "position": 0, "playing": False}
        finished = self.session.apply(action)
        self.assertEqual(finished["current_ref"], 3)
        self.assertEqual(finished["occurrence"], first["occurrence"] + 2)
        self.assertFalse(finished["playing"])
        self.assertEqual(finished["position"], 0)
        self.assertEqual(self.events, [("started", 1, None, None), ("completed", 1, None, 100),
                                       ("started", 2, None, None), ("skipped", 2, None, 12),
                                       ("started", 3, None, None), ("completed", 3, None, 300)])

        self.assertEqual(self.session.apply(action), finished)
        self.assertEqual(len(self.events), 6)
        resumed = self.session.apply({"op": "resume"})
        self.assertTrue(resumed["playing"])
        self.assertEqual(resumed["position"], 0)
        self.assertEqual(self.events[-1], ("started", 3, None, None))

    def test_reconnecting_after_the_cached_queue_runs_out_starts_the_next_track(self):
        first = self.session.apply({"op": "play_available"})
        rejoined = self.session.apply({"op": "resume_buffer", "expected_version": first["version"],
                                       "expected_ref": first["current_ref"], "expected_occurrence": first["occurrence"],
                                       "advances": [{"position": 100, "natural": True}],
                                       "finished": {"position": 200, "natural": True}, "position": 200, "playing": False})
        self.assertEqual(rejoined["current_ref"], 3)
        self.assertEqual(rejoined["position"], 0)
        self.assertTrue(rejoined["playing"])
        self.assertEqual(self.events[-2:], [("completed", 2, None, 200), ("started", 3, None, None)])

    def test_pause_resume_seek_and_shuffle_emit_nothing(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.events.clear()
        self.session.apply({"op": "pause"})
        self.session.apply({"op": "resume"})
        self.session.apply({"op": "seek", "position": 10})
        self.session.apply({"op": "shuffle"})
        self.assertEqual(self.events, [])

    def test_failed_actions_emit_nothing(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.events.clear()
        with self.assertRaises(ValueError):
            self.session.apply({"op": "queue_remove", "index": 9})
        with self.assertRaises(ValueError):
            self.session.apply({"op": "play", "playlist": "all", "track_id": 99})
        self.assertEqual(self.events, [])
        self.assertEqual(self.session.snapshot()["current_ref"], 1)

    def test_events_carry_the_acting_device(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1},
                           device_id="device-a")
        self.clock[0] += 10
        self.session.apply({"op": "next"}, device_id="device-b")
        self.assertEqual(self.events, [("started", 1, "device-a", None),
                                       ("skipped", 1, "device-b", 10.0),
                                       ("started", 2, "device-b", None)])

    def test_reset_closes_the_open_play_as_skipped(self):
        self.session.apply({"op": "play", "playlist": "all", "track_id": 1})
        self.clock[0] += 20
        self.session.reset()
        self.assertEqual(self.events, [("started", 1, None, None),
                                       ("skipped", 1, None, 20.0)])


class RestartPositionTests(unittest.TestCase):
    """Plan 045: a saved-as-playing session restarts paused, in place."""

    def setUp(self):
        self.clock = [1000.0]
        self.tracks = {1: {"id": 1, "duration": 100.0},
                       2: {"id": 2, "duration": 200.0}}

    def saved(self, **overrides):
        state = {"version": 3, "current": 1, "upcoming": [2], "history": [],
                 "order": [1, 2], "shuffle": False, "playing": False,
                 "position": 0.0, "changed_at": self.clock[0]}
        state.update(overrides)
        return state

    def load(self, saved, **kwargs):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "playback.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        return PlaybackSession(lambda playlist: [1, 2], self.tracks.get,
                               now=lambda: self.clock[0], state_path=path, **kwargs)

    def test_saved_as_playing_restarts_paused_at_the_saved_position(self):
        session = self.load(self.saved(playing=True, position=25.0,
                                       changed_at=self.clock[0] - 3 * 3600))
        snapshot = session.snapshot()
        self.assertFalse(snapshot["playing"])
        self.assertEqual(snapshot["position"], 25.0)
        session.apply({"op": "resume"})
        self.clock[0] += 5
        self.assertEqual(session.snapshot()["position"], 30.0)

    def test_saved_as_playing_position_clamps_to_the_track_length(self):
        session = self.load(self.saved(playing=True, position=500.0,
                                       changed_at=self.clock[0] - 3 * 3600))
        snapshot = session.snapshot()
        self.assertFalse(snapshot["playing"])
        self.assertEqual(snapshot["position"], 100.0)

    def test_saved_paused_loads_as_saved(self):
        session = self.load(self.saved(playing=False, position=25.0,
                                       changed_at=self.clock[0] - 3 * 3600))
        snapshot = session.snapshot()
        self.assertFalse(snapshot["playing"])
        self.assertEqual(snapshot["position"], 25.0)

    def test_restored_mid_play_is_still_open(self):
        events = []
        session = self.load(self.saved(playing=True, position=30.0),
                            record_event=lambda *args: events.append(args))
        session.apply({"op": "next"})
        self.assertEqual(events, [("skipped", 1, None, 30.0),
                                  ("started", 2, None, None)])


if __name__ == "__main__":
    unittest.main()
