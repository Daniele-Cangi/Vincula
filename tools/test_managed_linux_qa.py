# SPDX-FileCopyrightText: 2026 Daniele Cangi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Search-path validation fixtures; no ELF execution or desktop access."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "managed_linux_qa", Path(__file__).parent / "build-managed-desktop-linux" / "qa.py")
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


class ElfSearchPathTests(unittest.TestCase):
    def test_reads_both_tags_and_ignores_other_dynamic_values(self):
        output = """Dynamic section at offset 0x1000 contains 3 entries:
 0x0000000000000001 (NEEDED) Shared library: [libc.so.6]
 0x000000000000000f (RPATH) Library rpath: [$ORIGIN/../lib]
 0x000000000000001d (RUNPATH) Library runpath: [$ORIGIN/plugins]
"""
        self.assertEqual(qa.elf_search_paths(output), ["$ORIGIN/../lib", "$ORIGIN/plugins"])

    def test_unterminated_tag_cannot_take_a_value_from_the_next_line(self):
        for broken in ("Library runpath: $ORIGIN", "Library runpath: [$ORIGIN"):
            with self.subTest(broken=broken):
                output = "0x001d (RUNPATH) " + broken + "\n0x0001 (NEEDED) [libc.so.6]\n"
                with self.assertRaisesRegex(ValueError, "Malformed ELF search path"):
                    qa.elf_search_paths(output)

    def test_unrelated_long_text_is_ignored_without_a_pattern_search(self):
        output = "(" * 100_000 + "\n0x001d (RUNPATH) Library runpath: [$ORIGIN]\n"
        self.assertEqual(qa.elf_search_paths(output), ["$ORIGIN"])


if __name__ == "__main__":
    unittest.main()
