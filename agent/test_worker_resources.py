# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic OS/tool responses; optional native checks read only local public metrics."""
import subprocess
import sys
import unittest
from unittest.mock import patch

import resource_policy
import worker_resources as sampling

GPU = "GPU-11111111-2222-4333-8444-555555555555"


class WorkerResourcesTests(unittest.TestCase):
    def test_nvidia_uses_stable_identity_and_converts_mib_to_bytes(self):
        result = sampling.nvidia_observation((GPU + ", 1024, 512\n").encode())
        self.assertEqual(result, [{"id": GPU, "vram_total_bytes": 1073741824,
                                   "vram_available_bytes": 536870912}])
        for raw in (b"0,1024,512", (GPU + ", N/A, 0").encode(),
                    (GPU + ", 1, 2").encode(), (GPU + ",1,0\n" + GPU + ",1,0").encode(), b"x" * 65537):
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                sampling.nvidia_observation(raw)

    def test_tool_failure_stays_unknown_without_guessing_device_zero(self):
        with patch.object(sampling, "linux_memory", return_value=(8000, 4000)), \
             patch.object(sampling, "cpu_percent", return_value=45), \
             patch.object(sampling, "nvidia_devices", side_effect=subprocess.TimeoutExpired("synthetic", 2)), \
             patch.object(sampling.os, "cpu_count", return_value=4), \
             patch.object(sampling.sys, "platform", "linux"):
            observed = resource_policy.validate_observation(sampling.observe())
        self.assertIsNone(observed["gpus"])
        self.assertEqual(observed["cpu_millis"], 4000)
        self.assertEqual(observed["cpu_percent"], 45)

    def test_gpu_tool_reads_files_and_is_bounded_without_spawning_anything(self):
        def command(argv, **options):
            self.assertIn("--query-gpu=uuid,memory.total,memory.free", argv)
            self.assertEqual(options["timeout"], 2)
            self.assertIsNot(options["stdout"], subprocess.PIPE)
            options["stdout"].write((GPU + ", 1024, 512\n").encode())
        with patch.object(sampling.shutil, "which", return_value="synthetic-nvidia-smi"), \
             patch.object(sampling.subprocess, "run", side_effect=command) as run:
            self.assertEqual(sampling.nvidia_devices()[0]["id"], GPU)
            run.assert_called_once()

    def test_cpu_load_uses_deltas_and_unknown_is_not_reported_as_idle(self):
        with patch.object(sampling, "cpu_times", side_effect=[(100, 40), (200, 60)]), \
             patch.object(sampling.time, "sleep"):
            self.assertEqual(sampling.cpu_percent(), 80)
        with patch.object(sampling, "cpu_times", return_value=(100, 40)), \
             patch.object(sampling.time, "sleep"):
            self.assertIsNone(sampling.cpu_percent())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux public-metric fixture")
    def test_native_local_cpu_memory_only_without_gpu_tool(self):
        with patch.object(sampling, "nvidia_devices", return_value=None):
            observed = resource_policy.validate_observation(sampling.observe())
        self.assertGreater(observed["memory_total_bytes"], 0)
        self.assertGreater(observed["cpu_millis"], 0)
        self.assertIsNone(observed["gpus"])

    @unittest.skipUnless(sys.platform == "win32", "Native Windows memory API fixture")
    def test_native_windows_memory_api_only_without_gpu_tool(self):
        with patch.object(sampling, "nvidia_devices", return_value=None):
            observed = resource_policy.validate_observation(sampling.observe())
        self.assertGreater(observed["memory_total_bytes"], 0)
        self.assertGreater(observed["cpu_millis"], 0)


if __name__ == "__main__":
    unittest.main()
