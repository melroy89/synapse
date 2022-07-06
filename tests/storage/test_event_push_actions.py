# Copyright 2016-2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.storage.databases.main.event_push_actions import NotifCounts
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

USER_ID = "@user:example.com"

PlAIN_NOTIF = ["notify", {"set_tweak": "highlight", "value": False}]
HIGHLIGHT = [
    "notify",
    {"set_tweak": "sound", "value": "default"},
    {"set_tweak": "highlight"},
]


class EventPushActionsStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persist_events_store = hs.get_datastores().persist_events
        assert persist_events_store is not None
        self.persist_events_store = persist_events_store

    def test_get_unread_push_actions_for_user_in_range_for_http(self) -> None:
        self.get_success(
            self.store.get_unread_push_actions_for_user_in_range_for_http(
                USER_ID, 0, 1000, 20
            )
        )

    def test_get_unread_push_actions_for_user_in_range_for_email(self) -> None:
        self.get_success(
            self.store.get_unread_push_actions_for_user_in_range_for_email(
                USER_ID, 0, 1000, 20
            )
        )

    def test_count_aggregation(self) -> None:
        room_id = "!foo:example.com"
        user_id = "@user1235:test"

        last_read_stream_ordering = [0]

        def _assert_counts(noitf_count: int, highlight_count: int) -> None:
            counts, thread_counts = self.get_success(
                self.store.db_pool.runInteraction(
                    "",
                    self.store._get_unread_counts_by_pos_txn,
                    room_id,
                    user_id,
                    last_read_stream_ordering[0],
                )
            )
            self.assertEqual(
                counts,
                NotifCounts(
                    notify_count=noitf_count,
                    unread_count=0,  # Unread counts are tested in the sync tests.
                    highlight_count=highlight_count,
                ),
            )
            self.assertEqual(thread_counts, {})

        def _inject_actions(stream: int, action: list) -> None:
            event = Mock()
            event.room_id = room_id
            event.event_id = f"$test{stream}:example.com"
            event.internal_metadata.stream_ordering = stream
            event.internal_metadata.is_outlier.return_value = False
            event.depth = stream

            self.store._events_stream_cache.entity_has_changed(room_id, stream)

            self.get_success(
                self.store.db_pool.simple_insert(
                    table="events",
                    values={
                        "stream_ordering": stream,
                        "topological_ordering": stream,
                        "type": "m.room.message",
                        "room_id": room_id,
                        "processed": True,
                        "outlier": False,
                        "event_id": event.event_id,
                    },
                )
            )

            self.get_success(
                self.store.add_push_actions_to_staging(
                    event.event_id,
                    {user_id: action},
                    False,
                    None,
                )
            )
            self.get_success(
                self.store.db_pool.runInteraction(
                    "",
                    self.persist_events_store._set_push_actions_for_event_and_users_txn,
                    [(event, None)],
                    [(event, None)],
                )
            )

        def _rotate(stream: int) -> None:
            self.get_success(
                self.store.db_pool.runInteraction(
                    "rotate-receipts", self.store._handle_new_receipts_for_notifs_txn
                )
            )

            self.get_success(
                self.store.db_pool.runInteraction(
                    "rotate-notifs", self.store._rotate_notifs_before_txn, stream
                )
            )

        def _mark_read(stream: int, depth: int) -> None:
            last_read_stream_ordering[0] = stream

            self.get_success(
                self.store.insert_receipt(
                    room_id,
                    "m.read",
                    user_id=user_id,
                    event_ids=[f"$test{stream}:example.com"],
                    data={},
                )
            )

        _assert_counts(0, 0)
        _inject_actions(1, PlAIN_NOTIF)
        _assert_counts(1, 0)
        _rotate(1)
        _assert_counts(1, 0)

        _inject_actions(3, PlAIN_NOTIF)
        _assert_counts(2, 0)
        _rotate(3)
        _assert_counts(2, 0)

        _inject_actions(5, PlAIN_NOTIF)
        _mark_read(3, 3)
        _assert_counts(1, 0)

        _mark_read(5, 5)
        _assert_counts(0, 0)

        _inject_actions(6, PlAIN_NOTIF)
        _rotate(6)
        _assert_counts(1, 0)

        self.get_success(
            self.store.db_pool.simple_delete(
                table="event_push_actions", keyvalues={"1": 1}, desc=""
            )
        )

        _assert_counts(1, 0)

        _mark_read(6, 6)
        _assert_counts(0, 0)

        _inject_actions(8, HIGHLIGHT)
        _assert_counts(1, 1)
        _rotate(8)
        _assert_counts(1, 1)

        # Check that adding another notification and rotating after highlight
        # works.
        _inject_actions(10, PlAIN_NOTIF)
        _rotate(10)
        _assert_counts(2, 1)

        # Check that sending read receipts at different points results in the
        # right counts.
        _mark_read(8, 8)
        _assert_counts(1, 0)
        _mark_read(10, 10)
        _assert_counts(0, 0)

    def test_count_aggregation_threads(self) -> None:
        room_id = "!foo:example.com"
        user_id = "@user1235:test"
        thread_id = "$test7:example.com"

        last_read_stream_ordering = [0]

        def _assert_counts(
            noitf_count: int,
            highlight_count: int,
            thread_notif_count: int,
            thread_highlight_count: int,
        ) -> None:
            counts, thread_counts = self.get_success(
                self.store.db_pool.runInteraction(
                    "",
                    self.store._get_unread_counts_by_pos_txn,
                    room_id,
                    user_id,
                    last_read_stream_ordering[0],
                )
            )
            self.assertEqual(
                counts,
                NotifCounts(
                    notify_count=noitf_count,
                    unread_count=0,  # Unread counts are tested in the sync tests.
                    highlight_count=highlight_count,
                ),
            )
            if thread_notif_count or thread_highlight_count:
                self.assertEqual(
                    thread_counts,
                    {
                        thread_id: NotifCounts(
                            notify_count=thread_notif_count,
                            unread_count=0,  # Unread counts are tested in the sync tests.
                            highlight_count=thread_highlight_count,
                        ),
                    },
                )
            else:
                self.assertEqual(thread_counts, {})

        def _inject_actions(
            stream: int, action: list, thread_id: Optional[str] = None
        ) -> None:
            event = Mock()
            event.room_id = room_id
            event.event_id = f"$test{stream}:example.com"
            event.internal_metadata.stream_ordering = stream
            event.internal_metadata.is_outlier.return_value = False
            event.depth = stream

            self.store._events_stream_cache.entity_has_changed(room_id, stream)

            self.get_success(
                self.store.db_pool.simple_insert(
                    table="events",
                    values={
                        "stream_ordering": stream,
                        "topological_ordering": stream,
                        "type": "m.room.message",
                        "room_id": room_id,
                        "processed": True,
                        "outlier": False,
                        "event_id": event.event_id,
                    },
                )
            )

            self.get_success(
                self.store.add_push_actions_to_staging(
                    event.event_id,
                    {user_id: action},
                    False,
                    thread_id,
                )
            )
            self.get_success(
                self.store.db_pool.runInteraction(
                    "",
                    self.persist_events_store._set_push_actions_for_event_and_users_txn,
                    [(event, None)],
                    [(event, None)],
                )
            )

        def _rotate(stream: int) -> None:
            self.get_success(
                self.store.db_pool.runInteraction(
                    "rotate-receipts", self.store._handle_new_receipts_for_notifs_txn
                )
            )

            self.get_success(
                self.store.db_pool.runInteraction(
                    "rotate-notifs", self.store._rotate_notifs_before_txn, stream
                )
            )

        def _mark_read(stream: int, depth: int) -> None:
            last_read_stream_ordering[0] = stream

            self.get_success(
                self.store.insert_receipt(
                    room_id,
                    "m.read",
                    user_id=user_id,
                    event_ids=[f"$test{stream}:example.com"],
                    data={},
                )
            )

        _assert_counts(0, 0, 0, 0)
        _inject_actions(1, PlAIN_NOTIF)
        _assert_counts(1, 0, 0, 0)
        _rotate(1)
        _assert_counts(1, 0, 0, 0)

        _inject_actions(2, PlAIN_NOTIF, thread_id)
        _assert_counts(1, 0, 1, 0)
        _rotate(2)
        _assert_counts(1, 0, 1, 0)

        _inject_actions(4, PlAIN_NOTIF)
        _assert_counts(2, 0, 1, 0)
        _rotate(4)
        _assert_counts(2, 0, 1, 0)

        _inject_actions(5, PlAIN_NOTIF, thread_id)
        _assert_counts(2, 0, 2, 0)
        _rotate(5)
        _assert_counts(2, 0, 2, 0)

        _inject_actions(6, PlAIN_NOTIF)
        _inject_actions(7, PlAIN_NOTIF, thread_id)
        _mark_read(5, 5)
        _assert_counts(1, 0, 1, 0)

        _mark_read(7, 7)
        _assert_counts(0, 0, 0, 0)

        _inject_actions(8, PlAIN_NOTIF)
        _inject_actions(9, PlAIN_NOTIF, thread_id)
        _rotate(9)
        _assert_counts(1, 0, 1, 0)

        self.get_success(
            self.store.db_pool.simple_delete(
                table="event_push_actions", keyvalues={"1": 1}, desc=""
            )
        )

        _assert_counts(1, 0, 1, 0)

        _mark_read(9, 9)
        _assert_counts(0, 0, 0, 0)

        _inject_actions(10, HIGHLIGHT)
        _assert_counts(1, 1, 0, 0)
        _rotate(10)
        _assert_counts(1, 1, 0, 0)

        _inject_actions(11, HIGHLIGHT, thread_id)
        _assert_counts(1, 1, 1, 1)
        _rotate(11)
        _assert_counts(1, 1, 1, 1)

        # Check that adding another notification and rotating after highlight
        # works.
        _inject_actions(12, PlAIN_NOTIF)
        _rotate(12)
        _assert_counts(2, 1, 1, 1)

        _inject_actions(13, PlAIN_NOTIF, thread_id)
        _rotate(13)
        _assert_counts(2, 1, 2, 1)

        # Check that sending read receipts at different points results in the
        # right counts.
        _mark_read(11, 11)
        _assert_counts(1, 0, 1, 0)
        _mark_read(13, 13)
        _assert_counts(0, 0, 0, 0)

    def test_find_first_stream_ordering_after_ts(self) -> None:
        def add_event(so: int, ts: int) -> None:
            self.get_success(
                self.store.db_pool.simple_insert(
                    "events",
                    {
                        "stream_ordering": so,
                        "received_ts": ts,
                        "event_id": "event%i" % so,
                        "type": "",
                        "room_id": "",
                        "content": "",
                        "processed": True,
                        "outlier": False,
                        "topological_ordering": 0,
                        "depth": 0,
                    },
                )
            )

        # start with the base case where there are no events in the table
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(11))
        self.assertEqual(r, 0)

        # now with one event
        add_event(2, 10)
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(9))
        self.assertEqual(r, 2)
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(10))
        self.assertEqual(r, 2)
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(11))
        self.assertEqual(r, 3)

        # add a bunch of dummy events to the events table
        for (stream_ordering, ts) in (
            (3, 110),
            (4, 120),
            (5, 120),
            (10, 130),
            (20, 140),
        ):
            add_event(stream_ordering, ts)

        r = self.get_success(self.store.find_first_stream_ordering_after_ts(110))
        self.assertEqual(r, 3, "First event after 110ms should be 3, was %i" % r)

        # 4 and 5 are both after 120: we want 4 rather than 5
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(120))
        self.assertEqual(r, 4, "First event after 120ms should be 4, was %i" % r)

        r = self.get_success(self.store.find_first_stream_ordering_after_ts(129))
        self.assertEqual(r, 10, "First event after 129ms should be 10, was %i" % r)

        # check we can get the last event
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(140))
        self.assertEqual(r, 20, "First event after 14ms should be 20, was %i" % r)

        # off the end
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(160))
        self.assertEqual(r, 21)

        # check we can find an event at ordering zero
        add_event(0, 5)
        r = self.get_success(self.store.find_first_stream_ordering_after_ts(1))
        self.assertEqual(r, 0)
