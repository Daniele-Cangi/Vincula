# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Temporary SQLite ledgers only. No processes, SSH, live policies or devices."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

import resource_policy as resources

GPU = "GPU-11111111-2222-4333-8444-555555555555"
OTHER_GPU = "GPU-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def demand(cpu=1000, memory=2000, gpus=None):
    return {"cpu_millis": cpu, "memory_bytes": memory, "gpus": [] if gpus is None else gpus}


def gpu(identity=GPU, vram=1000, exclusive=False):
    return {"id": identity, "vram_bytes": vram, "exclusive": exclusive}


def observation():
    return {"observed_at": 100, "cpu_millis": 4000, "cpu_percent": 99.9,
            "memory_total_bytes": 16000, "memory_available_bytes": 12000,
            "gpus": [{"id": identity, "vram_total_bytes": 8000, "vram_available_bytes": 6000}
                     for identity in (GPU, OTHER_GPU)]}


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix="vincula-resource-test-")
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "resources.sqlite3"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(resources.SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        self.generation = str(uuid4())
        resources.worker_start(self.db, self.generation, "linux", now=100)
        resources.heartbeat(self.db, self.generation, observation(), now=100)

    def configure(self, **changes):
        return resources.set_policy(self.db, resources.policy(self.db) | changes)

    def claim(self, requirements=None):
        identity = str(uuid4())
        self.assertTrue(resources.try_claim(self.db, identity, self.generation, requirements, now=100))
        return identity

    def test_default_is_serial_with_a_durable_exclusive_legacy_claim(self):
        first = self.claim()
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=100))
        state = resources.status(self.db, now=100)
        self.assertEqual(state["policy"]["max_jobs"], 1)
        self.assertTrue(state["claims"][0]["exclusive"])
        self.assertTrue(state["claims"][0]["legacy"])
        self.assertFalse(resources.finish(self.db, first, self.generation, False, now=100))
        self.assertEqual(len(resources.status(self.db, now=100000)["claims"]), 1)
        self.assertTrue(resources.finish(self.db, first, self.generation, True, now=101))
        self.assertTrue(resources.finish(self.db, first, self.generation, True, now=102))
        self.assertFalse(resources.try_claim(self.db, first, self.generation, now=102))

    def test_configured_legacy_defaults_share_the_same_cpu_memory_and_job_budget(self):
        self.configure(max_jobs=3, cpu_millis=2000, memory_bytes=4000, legacy_defaults=demand())
        self.claim()
        self.claim(demand())
        state = resources.status(self.db, now=100)
        self.assertEqual(state["declared_consumption"]["jobs"], 2)
        self.assertEqual(state["declared_consumption"]["cpu_millis"], 2000)
        self.assertEqual(state["declared_consumption"]["memory_bytes"], 4000)
        check = resources.evaluate(state, demand())
        self.assertTrue(check["eligible"])
        self.assertFalse(check["admissible_now"])
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=100))

    def test_none_budgets_use_known_hardware_and_cpu_load_does_not_enforce_a_cap(self):
        self.assertTrue(resources.evaluate(resources.status(self.db, now=100), demand())["admissible_now"])
        self.claim(demand(4000, 12000, [gpu(vram=6000)]))
        self.assertEqual(resources.status(self.db, now=100)["hard_limits_enforced"],
                         {"cpu": False, "memory": False, "gpu": False})

    def test_observation_failures_staleness_and_legacy_worker_fail_closed(self):
        self.db.execute("DELETE FROM resource_worker")
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=100))
        resources.worker_start(self.db, self.generation, "linux", now=100)
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=100))
        for changes in ({"cpu_millis": None}, {"memory_total_bytes": None, "memory_available_bytes": None},
                        {"observed_at": 90}, {"observed_at": 101}):
            resources.heartbeat(self.db, self.generation, observation() | changes, now=100)
            self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=100))
        resources.heartbeat(self.db, self.generation, observation(), now=100)
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, demand(), now=106))
        self.assertFalse(resources.evaluate({"protocol_version": 1}, demand())["eligible"])

    def test_gpu_ids_exclusivity_and_shared_vram_are_accounted(self):
        self.configure(max_jobs=4)
        exclusive = self.claim(demand(gpus=[gpu(exclusive=True)]))
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation,
                                            demand(gpus=[gpu()]), now=100))
        self.claim(demand(gpus=[gpu(OTHER_GPU)]))
        resources.finish(self.db, exclusive, self.generation, True, now=100)
        self.claim(demand(gpus=[gpu(vram=4000)]))
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation,
                                            demand(gpus=[gpu(vram=3000)]), now=100))

    def test_explicit_gpu_disable_and_unknown_metrics_do_not_become_zero(self):
        request = demand(gpus=[gpu()])
        self.configure(gpus=[])
        self.assertFalse(resources.evaluate(resources.status(self.db, now=100), request)["eligible"])
        self.configure(gpus=None)
        resources.heartbeat(self.db, self.generation, observation() | {"gpus": None}, now=100)
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, request, now=100))
        sample = observation()
        sample["gpus"][0]["vram_available_bytes"] = None
        resources.heartbeat(self.db, self.generation, sample, now=100)
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), self.generation, request, now=100))

    def test_policy_cas_and_change_preserve_held_claim_identity(self):
        first = self.claim()
        revision = resources.status(self.db, now=100)["policy_revision"]
        self.configure(legacy_defaults=demand())
        with self.assertRaises(ValueError):
            resources.set_policy(self.db, resources.DEFAULT_POLICY, expected_revision=revision)
        self.assertTrue(resources.try_claim(self.db, first, self.generation, now=100))
        self.assertTrue(resources.status(self.db, now=100)["claims"][0]["exclusive"])

    def test_immutable_request_and_release_do_not_allow_uuid_reuse(self):
        identity = self.claim(demand())
        self.assertTrue(resources.try_claim(self.db, identity, self.generation, demand(), now=100))
        with self.assertRaises(ValueError):
            resources.set_request(self.db, identity, demand(cpu=2000))
        resources.finish(self.db, identity, self.generation, True, now=100)
        self.assertFalse(resources.try_claim(self.db, identity, self.generation, demand(), now=100))

    def test_new_generation_keeps_claims_until_a_scoped_cleanup_receipt(self):
        old_job = self.claim(demand())
        nonce = resources.claim_nonce(self.db, old_job, self.generation)
        newer = str(uuid4())
        resources.worker_start(self.db, newer, "linux", cleanup_confirmed_previous=True, now=101)
        resources.heartbeat(self.db, newer, observation() | {"observed_at": 101}, now=101)
        self.assertFalse(resources.try_claim(self.db, str(uuid4()), newer, demand(), now=101))
        with self.assertRaises(ValueError):
            resources.heartbeat(self.db, self.generation, observation(), now=101)
        with self.assertRaises(ValueError):
            resources.finish(self.db, old_job, self.generation, True, now=101)
        receipt = {"protocol_version": 2, "job_id": old_job, "generation": self.generation,
                   "cleanup_nonce": nonce, "cleanup_confirmed": True}
        for changes in ({"cleanup_nonce": str(uuid4())}, {"job_id": str(uuid4())},
                        {"generation": newer}, {"cleanup_confirmed": False}, {"protocol_version": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                resources.reconcile(self.db, receipt | changes, now=101)
        self.assertNotIn(nonce, json.dumps(resources.status(self.db, now=101)))
        self.assertTrue(resources.reconcile(self.db, receipt, now=101))
        self.assertTrue(resources.reconcile(self.db, receipt, now=102))
        self.assertTrue(resources.try_claim(self.db, str(uuid4()), newer, demand(), now=102))

    def test_validation_rejects_booleans_negative_quantities_and_unstable_gpu_ids(self):
        malformed = [demand(cpu=True), demand(memory=-1), demand(cpu=2**63),
                     demand(gpus=[gpu("0")]), demand(gpus=[gpu(), gpu()]), demand() | {"extra": 1}]
        for request in malformed:
            with self.subTest(request=request), self.assertRaises(ValueError):
                resources.validate_requirements(request)
        for maximum in (True, 0, resources.MAX_JOBS + 1):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                resources.validate_policy(resources.DEFAULT_POLICY | {"max_jobs": maximum})

    def test_claim_is_part_of_callers_transaction_and_never_commits(self):
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        self.claim(demand())
        self.db.rollback()
        self.db.execute("BEGIN IMMEDIATE")
        self.assertEqual(resources.status(self.db, now=100)["claims"], [])
        self.db.commit()
        with self.assertRaises(ValueError):
            resources.status(self.db, now=100)

    def test_competing_origins_cannot_overbook_the_serial_budget(self):
        self.db.commit()
        gate = Barrier(2)
        def compete(_):
            with closing(sqlite3.connect(self.path, isolation_level=None, timeout=2)) as db:
                gate.wait(timeout=2)
                db.execute("BEGIN IMMEDIATE")
                admitted = resources.try_claim(db, str(uuid4()), self.generation, demand(), now=100)
                db.commit()
                return admitted
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(compete, range(2))), 1)
        self.db.execute("BEGIN IMMEDIATE")
        self.assertEqual(len(resources.status(self.db, now=100)["claims"]), 1)

    def test_snapshot_counts_and_eligibility_distinguish_busy_from_impossible(self):
        self.db.executescript("CREATE TABLE jobs(id TEXT,state TEXT); INSERT INTO jobs VALUES('a','queued'),('b','running');")
        self.db.execute("BEGIN IMMEDIATE")
        self.claim(demand())
        state = resources.status(self.db, now=100)
        self.assertEqual(state["job_counts"], {"queued": 1, "running": 1})
        self.assertTrue(resources.evaluate(state, demand())["eligible"])
        self.assertFalse(resources.evaluate(state, demand())["admissible_now"])
        self.assertFalse(resources.evaluate(state, demand(cpu=5000))["eligible"])

    def test_state_identity_survives_reinitialization_and_differs_after_database_loss(self):
        original = resources.state_id(self.db)
        self.db.commit()
        self.db.executescript(resources.SCHEMA)
        self.db.execute("BEGIN IMMEDIATE")
        self.assertEqual(resources.state_id(self.db), original)
        newer = str(uuid4())
        resources.worker_start(self.db, newer, "linux", now=101)
        self.assertEqual(resources.status(self.db, now=101)["state_id"], original)
        with closing(sqlite3.connect(":memory:")) as other:
            other.executescript(resources.SCHEMA)
            other.execute("BEGIN IMMEDIATE")
            self.assertNotEqual(resources.state_id(other), original)

    def test_graceful_worker_stop_retires_freshness_without_releasing_claims(self):
        job = self.claim(demand())
        before = resources.status(self.db, now=100)
        self.assertTrue(resources.worker_stopped(self.db, self.generation, now=101))
        self.assertTrue(resources.worker_stopped(self.db, self.generation, now=102))
        after = resources.status(self.db, now=102)
        self.assertTrue(after["worker"]["stopped"])
        self.assertFalse(after["worker"]["fresh"])
        self.assertFalse(after["worker"]["observation_fresh"])
        self.assertEqual(after["worker"]["heartbeat_at"], before["worker"]["heartbeat_at"])
        self.assertEqual(after["claims"], before["claims"])
        self.assertFalse(resources.try_claim(self.db, job, self.generation, demand(), now=102))
        with self.assertRaises(ValueError):
            resources.heartbeat(self.db, self.generation, observation(), now=102)
        newer = str(uuid4())
        resources.worker_start(self.db, newer, "linux", now=102)
        with self.assertRaises(ValueError):
            resources.worker_start(self.db, self.generation, "linux", now=102)
        with self.assertRaises(ValueError):
            resources.worker_stopped(self.db, self.generation, now=102)
        self.assertEqual(resources.status(self.db, now=102)["claims"], before["claims"])

    def test_relative_freshness_lifetimes_do_not_require_controller_wall_clock(self):
        for now, expected in ((100, 5.0), (102.5, 2.5), (105, 0.0), (106, 0.0), (99, 0.0)):
            with self.subTest(now=now):
                worker = resources.status(self.db, now=now)["worker"]
                self.assertEqual(worker["fresh_remaining_seconds"], expected)
                self.assertEqual(worker["observation_remaining_seconds"], expected)
        resources.heartbeat(self.db, self.generation, observation() | {"observed_at": 101}, now=103)
        worker = resources.status(self.db, now=103)["worker"]
        self.assertEqual(worker["fresh_remaining_seconds"], 5.0)
        self.assertEqual(worker["observation_remaining_seconds"], 3.0)
        resources.heartbeat(self.db, self.generation, now=103)
        self.assertEqual(resources.status(self.db, now=103)["worker"]["observation_remaining_seconds"], 0.0)
        resources.heartbeat(self.db, self.generation, observation() | {"observed_at": 104}, now=103)
        self.assertEqual(resources.status(self.db, now=103)["worker"]["observation_remaining_seconds"], 0.0)
        resources.worker_stopped(self.db, self.generation, now=103)
        worker = resources.status(self.db, now=103)["worker"]
        self.assertEqual(worker["fresh_remaining_seconds"], 0.0)
        self.assertEqual(worker["observation_remaining_seconds"], 0.0)


if __name__ == "__main__":
    unittest.main()
