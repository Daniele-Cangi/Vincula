# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import benchmark_cpu


class BenchmarkFixtureTests(unittest.TestCase):
    def test_known_prime_counts(self):
        self.assertEqual(benchmark_cpu.count_primes(2, 100), 25)
        self.assertEqual(benchmark_cpu.count_primes(100, 200), 21)

    def test_file_content_cannot_expand_workload(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            path = folder / 'input-0.json'
            expected = {'index': 0, 'start': 2, 'end': 1000000, 'passes': 2, 'padding': ''}
            raw = json.dumps(expected).encode()
            path.write_bytes(raw)
            self.assertEqual(benchmark_cpu.read_input(folder, 0), raw)
            for changes in ({'end': 10 ** 30}, {'passes': 10 ** 20}, {'index': False}, {'padding': []}):
                path.write_text(json.dumps(expected | changes))
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    benchmark_cpu.read_input(folder, 0)
            path.write_bytes(b' ' * (benchmark_cpu.MAX_INPUT_BYTES + 1))
            with self.assertRaisesRegex(ValueError, 'exceeds'):
                benchmark_cpu.read_input(folder, 0)


if __name__ == '__main__':
    unittest.main()
