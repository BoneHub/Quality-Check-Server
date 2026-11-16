"""Several clients hitting the server at the same time.

The users are people, so the load is tiny, but two of them pressing "next scan" at the
same moment must still never be handed the same scan, and one user's submission, which
takes seconds for a real scan, must not hold up anybody else.
"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from qc_server.client import REVIEWER, BoneHubQCClient
from qc_server.store import QCError

from tests.support import QCTestCase
from tests.test_client import LiveServerTestCase


class ConcurrentHandoutTests(QCTestCase):
    def test_simultaneous_requests_never_hand_out_the_same_subject(self):
        self.default_dataset(n_scans=12)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(12)]

        start = threading.Barrier(len(users))

        def grab(user):
            start.wait(timeout=10)
            return store.next_scan(user, REVIEWER).scan_key

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            keys = list(pool.map(grab, users))

        self.assertEqual(len(keys), 12)
        self.assertEqual(len(set(keys)), 12, "a scan was handed to two reviewers at once")

    def test_more_reviewers_than_subjects_means_some_get_a_clean_refusal(self):
        self.default_dataset(n_scans=3)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(8)]
        start = threading.Barrier(len(users))

        def grab(user):
            start.wait(timeout=10)
            try:
                return store.next_scan(user, REVIEWER).scan_key
            except QCError as exc:
                return f"refused:{exc.status_code}"

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            results = list(pool.map(grab, users))

        handed_out = [r for r in results if not r.startswith("refused")]
        self.assertEqual(len(handed_out), 3)
        self.assertEqual(len(set(handed_out)), 3)
        self.assertTrue(all(r == "refused:404" for r in results if r.startswith("refused")))

    def test_concurrent_verdicts_all_land_in_the_state_folder(self):
        """Each reviewer judges a different scan; every verdict is kept, and survives a restart."""
        self.default_dataset(n_scans=6)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(6)]
        assignments = [store.next_scan(user, REVIEWER) for user in users]

        start = threading.Barrier(len(users))

        def send(item):
            user, assignment = item
            start.wait(timeout=10)
            return store.submit(assignment.assignment_id, user, True, None, use_stored_segmentation=True)

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            outcomes = list(pool.map(send, zip(users, assignments)))

        self.assertTrue(all(o.stage == "approval" for o in outcomes))
        self.assertEqual(len(store.cases(stages=["approval"])), 6)
        self.assertEqual(len(self.make_store().cases(stages=["approval"])), 6, "a concurrent write lost a verdict")
        store.approve_all()
        for subject_id in range(1, 7):
            self.assertEqual(self.reviewed(store, 1, subject_id), {"FEMUR_LEFT", "FEMUR_RIGHT"})
        self.assertEqual(store.stats().eligible_scans, 0)

    def test_concurrent_user_creation_keeps_every_reviewer(self):
        self.default_dataset(n_scans=1)
        store = self.make_store()
        start = threading.Barrier(10)

        def create(index):
            start.wait(timeout=10)
            return store.create_user(f"reviewer_{index}")[1]

        with ThreadPoolExecutor(max_workers=10) as pool:
            keys = list(pool.map(create, range(10)))

        self.assertEqual(len(set(keys)), 10)
        self.assertEqual(len(store.list_users()), 10)
        reopened = self.make_store()
        self.assertEqual(len(reopened.list_users()), 10, "a concurrent write lost a user on disk")

    def test_the_audit_trail_keeps_every_concurrent_submission(self):
        self.default_dataset(n_scans=8)
        store = self.make_store()
        users = [store.create_user(f"reviewer_{i}")[0] for i in range(8)]
        assignments = [store.next_scan(user, REVIEWER) for user in users]
        start = threading.Barrier(len(users))

        def send(item):
            user, assignment = item
            start.wait(timeout=10)
            store.submit(assignment.assignment_id, user, False, None)

        with ThreadPoolExecutor(max_workers=len(users)) as pool:
            list(pool.map(send, zip(users, assignments)))

        entries = store.audit.read_recent(limit=100, kind="submission")
        self.assertEqual(len(entries), 8)
        self.assertEqual(len({e["scan_key"] for e in entries}), 8)


class BusyServerTests(LiveServerTestCase):
    """Everyone else is answered while the server works on one user's submission, or on an approval.

    Checking the segmentation of a real scan, and writing it into the dataset, take seconds.
    When that held up the other requests, the whole server seemed to freeze for everyone until
    it was done.
    """

    def setUp(self) -> None:
        super().setUp()
        # A short timeout, so that a request stuck behind the submission fails the test quickly.
        self.bob = BoneHubQCClient(self.base_url, self.bob_key, timeout=5, role=REVIEWER)

    def hold(self, name: str) -> tuple[threading.Event, threading.Event]:
        """Make a call to the store's method ``name`` wait until ``release`` is set.

        Returns ``(entered, release)``; ``entered`` is set once a call is waiting.
        """
        entered, release = threading.Event(), threading.Event()
        original = getattr(self.store, name)

        def held(*args, **kwargs):
            entered.set()
            release.wait(timeout=30)
            return original(*args, **kwargs)

        setattr(self.store, name, held)
        return entered, release

    def accept_in_background(self, pool: ThreadPoolExecutor):
        """Alice leases a scan on the review page and accepts it, on another thread; accepting
        checks the whole segmentation file."""
        handout = self.reviewer.next_scan()
        return pool.submit(
            self.reviewer.submit, handout["assignment_id"], quality_check_confirmed=True, use_stored_segmentation=True
        )

    def test_others_are_answered_while_a_segmentation_is_checked(self):
        entered, release = self.hold("inspect_segmentation")
        with ThreadPoolExecutor(max_workers=1) as pool:
            submission = self.accept_in_background(pool)
            try:
                self.assertTrue(entered.wait(timeout=10), "the submission never reached its check")
                self.assertEqual(self.bob.ping()["user"], "bob")
                handout = self.bob.next_scan()
                self.bob.download_image(handout["assignment_id"], self.tmp_path / "bob" / "image.nii.gz")
            finally:
                release.set()
            self.assertTrue(submission.result(timeout=30)["quality_check_confirmed"])

    def test_others_are_answered_while_an_approval_writes_the_dataset(self):
        """An approval writes into the dataset under the dataset's lock, which neither checking a
        key nor handing out a scan takes."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.accept_in_background(pool).result(timeout=30)
        entered, release = self.hold("_write_approved")
        with ThreadPoolExecutor(max_workers=1) as pool:
            approval = pool.submit(self.store.approve, "ds-0001/sub-000001/scan-01")
            try:
                self.assertTrue(entered.wait(timeout=10), "the approval never reached the dataset")
                self.assertEqual(self.bob.ping()["user"], "bob")
                self.assertEqual(self.bob.next_scan()["scan_key"], "ds-0001/sub-000002/scan-01")
            finally:
                release.set()
            self.assertEqual(approval.result(timeout=30).case.stage, "applied")


if __name__ == "__main__":
    unittest.main()
