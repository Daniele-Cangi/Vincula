# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure placement/replay fixtures: no SQLite, processes, devices or network."""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import math
import unittest
from uuid import UUID, uuid4, uuid5

import execution_plans as plans
import resource_policy as resources

GPU = "GPU-11111111-2222-4333-8444-555555555555"
OTHER_GPU = "GPU-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def demand(cpu=1000, memory=1000, gpus=None):
    return {"cpu_millis": cpu, "memory_bytes": memory, "gpus": gpus or []}


def process(**changes):
    return {"argv": ["python3", "-c", "print('fixture')"], "cwd": None, "env": None,
            "stdin_text": None, "timeout_seconds": 10, "resources": demand()} | changes


def candidate(node, seconds=1, setup=0, **changes):
    return {"node_id": node, "process": process(**changes), "estimated_seconds": seconds, "setup_seconds": setup}


def request(*rows):
    return {"tasks": [{"candidates": list(row)} for row in rows]}


def snapshot(*, platform="linux", load=0, queue=0, policy=None, observation=None):
    limits = resources.validate_policy(resources.DEFAULT_POLICY | (policy or {}))
    observed = {"observed_at": 100, "cpu_millis": 4000, "cpu_percent": load, "memory_total_bytes": 16000,
                "memory_available_bytes": 12000,
                "gpus": [{"id": GPU, "vram_total_bytes": 8000, "vram_available_bytes": 6000}]} | (observation or {})
    return {"protocol_version": 2, "state_id": str(uuid4()), "policy": limits,
            "policy_revision": sha256(resources.canonical(limits).encode()).hexdigest(),
            "worker": {"generation": str(uuid4()), "protocol_version": 2, "platform": platform,
                       "heartbeat_at": 100, "fresh": True, "observation": observed, "observation_fresh": True},
            "claims": [], "declared_consumption": {"jobs": 0, "cpu_millis": 0, "memory_bytes": 0, "gpus": {}},
            "job_counts": {"queued": queue}, "admission_only": True,
            "hard_limits_enforced": {"cpu": False, "memory": False, "gpu": False}}


def record(manifest):
    return {"manifest": deepcopy(manifest), "sha256": sha256(plans.canonical(manifest).encode()).hexdigest()}


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.local, self.remote, self.unknown = (str(uuid4()) for _ in range(3))
        self.plan_id = str(uuid4())
        self.snapshots = {self.local: snapshot(), self.remote: snapshot()}

    def make(self, value, snapshots=None):
        return plans.make_manifest(self.plan_id, self.local, value, self.snapshots if snapshots is None else snapshots)

    def test_normalization_defaults_and_detaches_all_mutable_input(self):
        source = {"tasks": [{"candidates": [{"node_id": self.local,
                   "process": {"argv": ["python3"], "resources": demand()}}]}]}
        value = plans.normalize_request(source)
        choice = value["tasks"][0]["candidates"][0]
        self.assertEqual(choice["estimated_seconds"], 1.0)
        self.assertEqual(choice["setup_seconds"], 0.0)
        self.assertEqual(set(choice["process"]), plans.PROCESS_FIELDS)
        source["tasks"][0]["candidates"][0]["process"]["argv"].append("changed")
        self.assertEqual(choice["process"]["argv"], ["python3"])

    def test_invalid_counts_fields_estimates_and_implicit_resources_are_rejected(self):
        good = candidate(self.local)
        invalid = [{"tasks": []}, {"tasks": [{"candidates": []}]}, request(*[[good]] * 13),
                   request([good] * 65), {"tasks": [{"candidates": [good], "depends_on": []}]},
                   request([dict(good, node_id="../../host")]), request([dict(good, extra=True)]),
                   request([dict(good, process={"argv": ["python3"]})]),
                   request([dict(good, process=process(protocol_version=2))])]
        invalid += [request([dict(good, estimated_seconds=value)]) for value in (0, -1, True, math.inf, math.nan)]
        invalid += [request([dict(good, setup_seconds=value)]) for value in (-1, False, math.inf)]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                plans.normalize_request(value)

    def test_request_and_manifest_json_limits_are_separate(self):
        with self.assertRaisesRegex(ValueError, "size limit"):
            plans.normalize_request(request(*[[candidate(self.local, stdin_text="x" * 50000)]] * 3))
        small = request([candidate(self.local, argv=["python3", ""])])
        overhead = len(plans.canonical(plans.normalize_request(small)).encode())
        near_limit = request([candidate(self.local, argv=["python3", "x" * (plans.MAX_REQUEST_BYTES - overhead - 1)])])
        plans.normalize_request(near_limit)
        with self.assertRaisesRegex(ValueError, "size limit"):
            self.make(near_limit)

    def test_unknown_stale_and_legacy_nodes_do_not_become_eligible(self):
        with self.assertRaisesRegex(ValueError, "no eligible"):
            self.make(request([candidate(self.unknown)]))
        for replacement in (None, {"protocol_version": 1}, snapshot()):
            if isinstance(replacement, dict) and replacement.get("worker"):
                replacement["worker"]["fresh"] = False
            with self.subTest(snapshot=replacement), self.assertRaisesRegex(ValueError, "no eligible"):
                self.make(request([candidate(self.local)]), {self.local: replacement})

    def test_impossible_cpu_and_memory_quotas_or_unknown_hardware_are_rejected(self):
        for state in (snapshot(policy={"cpu_millis": 500}), snapshot(policy={"memory_bytes": 500}),
                      snapshot(observation={"cpu_millis": None}), snapshot(observation={"memory_total_bytes": None,
                                                                                              "memory_available_bytes": None})):
            selected = self.make(request([candidate(self.local), candidate(self.remote)]),
                                 {self.local: state, self.remote: self.snapshots[self.remote]})
            self.assertEqual(selected["plan"][0]["node_id"], self.remote)

    def test_gpu_identity_total_vram_and_owner_policy_are_required(self):
        gpu = {"id": GPU, "vram_bytes": 7000, "exclusive": False}
        for state, needed in ((snapshot(observation={"gpus": None}), gpu),
                              (snapshot(), dict(gpu, id=OTHER_GPU)),
                              (snapshot(), dict(gpu, vram_bytes=9000)),
                              (snapshot(policy={"gpus": []}), gpu),
                              (snapshot(policy={"gpus": [{"id": GPU, "vram_bytes": 5000}]}), gpu)):
            with self.subTest(state=state, needed=needed), self.assertRaisesRegex(ValueError, "no eligible"):
                self.make(request([candidate(self.local, resources=demand(gpus=[needed]))]), {self.local: state})
        value = self.make(request([candidate(self.local, resources=demand(gpus=[gpu]))]))
        decision = value["plan"][0]["decision"]
        self.assertFalse(decision["snapshot_admissible_now"])
        self.assertEqual(decision["reason"], "gpu_vram_limit")
        self.assertTrue(decision["advisory"])

    def test_busy_eligible_node_can_queue_but_is_penalized(self):
        busy = snapshot(queue=8)
        value = self.make(request([candidate(self.local), candidate(self.remote)]),
                          {self.local: busy, self.remote: self.snapshots[self.remote]})
        self.assertEqual(value["plan"][0]["node_id"], self.remote)
        occupied = snapshot(policy={"memory_bytes": 1000}, observation={"memory_available_bytes": 500})
        value = self.make(request([candidate(self.local)]), {self.local: occupied})
        decision = value["plan"][0]["decision"]
        self.assertFalse(decision["projected_admissible_now"])
        self.assertEqual(decision["reason"], "memory_bytes_limit")
        self.assertGreater(decision["busy_seconds"], 0)

    def test_cpu_load_and_data_preparation_cost_change_placement(self):
        value = self.make(request([candidate(self.local), candidate(self.remote)]),
                          {self.local: snapshot(load=100), self.remote: snapshot(load=0)})
        self.assertEqual(value["plan"][0]["node_id"], self.remote)
        for setup, expected in ((10, self.local), (1, self.remote)):
            value = self.make(request([candidate(self.local, 5), candidate(self.remote, 2, setup)]))
            self.assertEqual(value["plan"][0]["node_id"], expected)
        value = self.make(request([candidate(self.local)]), {self.local: snapshot(load=None)})
        self.assertIsNone(value["plan"][0]["decision"]["cpu_percent"])

    def test_measured_rpc_cost_can_outweigh_a_remote_compute_gain(self):
        choices = request([candidate(self.local, 5), candidate(self.remote, 1)])
        self.assertEqual(self.make(choices)["plan"][0]["node_id"], self.remote)
        states = deepcopy(self.snapshots)
        states[self.local]["request_round_trip_seconds"] = 0.01
        states[self.remote]["request_round_trip_seconds"] = 1.0
        result = self.make(choices, states)
        part = result["plan"][0]
        self.assertEqual(part["node_id"], self.local)
        self.assertEqual(part["decision"]["rpc_seconds"], 0.01)
        self.assertAlmostEqual(part["decision"]["control_seconds"], 0.06)
        self.assertAlmostEqual(part["decision"]["score_seconds"], 5.06)
        altered = deepcopy(result)
        altered["plan"][0]["decision"]["control_seconds"] = 0
        with self.assertRaises(ValueError):
            plans.validate_manifest(altered)
        for invalid in (-1, True, math.inf, math.nan):
            changed = deepcopy(states)
            changed[self.local]["request_round_trip_seconds"] = invalid
            with self.subTest(rpc=invalid), self.assertRaisesRegex(ValueError, "no eligible"):
                self.make(request([candidate(self.local)]), changed)

    def test_planned_cpu_quota_and_gpu_exclusivity_avoid_reusing_occupied_capacity(self):
        choices = [candidate(self.local), candidate(self.remote)]
        states = {self.local: snapshot(policy={"max_jobs": 4, "cpu_millis": 1000}),
                  self.remote: snapshot(policy={"max_jobs": 4})}
        result = self.make(request(choices, choices, choices), states)
        self.assertEqual([part["node_id"] for part in result["plan"]], [self.local, self.remote, self.remote])
        gpu_request = demand(gpus=[{"id": GPU, "vram_bytes": 1000, "exclusive": True}])
        choices = [candidate(self.local, resources=gpu_request)]
        result = self.make(request(choices, choices), {self.local: snapshot(policy={"max_jobs": 4})})
        self.assertEqual(result["plan"][1]["decision"]["reason"], "gpu_exclusive")
        self.assertFalse(result["plan"][1]["decision"]["projected_admissible_now"])

    def test_stable_ids_do_not_collide_with_legacy_batches_or_depend_on_node_order(self):
        value = request([candidate(self.local), candidate(self.remote)])
        before = deepcopy(self.snapshots)
        first = self.make(value)
        second = self.make(value, dict(reversed(list(self.snapshots.items()))))
        self.assertEqual(first, second)
        self.assertEqual(self.snapshots, before)
        job = first["plan"][0]["job_id"]
        self.assertNotEqual(job, str(uuid5(UUID(self.plan_id), f"{self.local}:0")))
        self.assertEqual(job, str(uuid5(UUID(self.plan_id), f"execution:{self.local}:0")))
        self.assertEqual(first["plan"][0]["expected_state_id"], self.snapshots[self.local]["state_id"])

    def test_variants_for_one_node_preserve_candidate_index_and_destination_path_semantics(self):
        for platform, paths in (("linux", ["C:\\Data", "/data"]), ("windows", ["/data", "C:\\Data"])):
            value = self.make(request([candidate(self.local, cwd=path) for path in paths]),
                              {self.local: snapshot(platform=platform)})
            self.assertEqual(value["plan"][0]["candidate_index"], 1)
            self.assertEqual(value["plan"][0]["process"]["cwd"], paths[1])
        environment = {"PATH": "first", "Path": "second"}
        choices = [candidate(self.local, env=environment), candidate(self.local, env={"PATH": "single"})]
        linux = self.make(request(choices), {self.local: snapshot(platform="linux")})
        windows = self.make(request(choices), {self.local: snapshot(platform="windows")})
        self.assertEqual(linux["plan"][0]["candidate_index"], 0)
        self.assertEqual(windows["plan"][0]["candidate_index"], 1)
        self.assertEqual(plans.select_replica(self.plan_id, [record(windows)]), windows)

    def test_replica_recovery_preserves_origin_job_and_node_state_without_replanning(self):
        saved = self.make(request([candidate(self.local), candidate(self.remote)]))
        replay = plans.select_replica(self.plan_id, [None, record(saved), record(saved)])
        self.assertEqual(replay, saved)
        another_origin = plans.make_manifest(self.plan_id, self.remote, saved["request"], self.snapshots)
        self.assertNotEqual(another_origin["plan"][0]["job_id"], replay["plan"][0]["job_id"])
        self.assertEqual(replay["origin"], self.local)
        replay["origin"] = self.remote
        self.assertEqual(saved["origin"], self.local)
        changed = deepcopy(self.snapshots)
        changed[self.local]["state_id"] = str(uuid4())
        newly_planned = self.make(saved["request"], changed)
        self.assertEqual(saved["plan"][0]["job_id"], newly_planned["plan"][0]["job_id"])
        self.assertNotEqual(saved["plan"][0]["expected_state_id"], newly_planned["plan"][0]["expected_state_id"])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            plans.select_replica(self.plan_id, [record(saved), record(newly_planned)])

    def test_conflicting_corrupt_wrong_kind_and_wrong_id_replicas_fail_closed(self):
        saved = self.make(request([candidate(self.local)]))
        self.assertIsNone(plans.select_replica(self.plan_id, [None]))
        for changed in (dict(record(saved), sha256="0" * 64),
                        record(dict(saved, kind="batch")), record(dict(saved, id=str(uuid4())))):
            with self.subTest(record=changed), self.assertRaises(ValueError):
                plans.select_replica(self.plan_id, [changed])

    def test_manifest_rejects_changed_process_ids_decisions_and_candidate_index(self):
        source = self.make(request([candidate(self.local)]))
        mutations = [lambda x: x.update(version=True), lambda x: x.update(origin=self.remote),
                     lambda x: x["plan"][0].update(candidate_index=1),
                     lambda x: x["plan"][0].update(node_id=self.remote),
                     lambda x: x["plan"][0].update(job_id=str(uuid4())),
                     lambda x: x["plan"][0].update(expected_state_id="not-a-uuid"),
                     lambda x: x["plan"][0]["process"]["argv"].append("unapproved"),
                     lambda x: x["plan"][0]["process"]["resources"].update(cpu_millis=True),
                     lambda x: x["plan"][0]["decision"].update(score_seconds=0)]
        for change in mutations:
            value = deepcopy(source); change(value)
            with self.subTest(change=change), self.assertRaises(ValueError):
                plans.validate_manifest(value)

    def test_twelve_tasks_and_sixty_four_variants_are_bounded_and_use_generic_ids(self):
        value = self.make(request(*[[candidate(self.local)]] * 12))
        self.assertEqual(len({part["job_id"] for part in value["plan"]}), 12)
        self.assertEqual(Counter(part["node_id"] for part in value["plan"]), {self.local: 12})
        value = self.make(request([*[candidate(self.unknown)] * 63, candidate(self.local)]))
        self.assertEqual(value["plan"][0]["candidate_index"], 63)


if __name__ == "__main__":
    unittest.main()
