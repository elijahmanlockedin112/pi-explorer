"""Tests for pi_explorer.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.TemporaryDirectory()
os.environ["PI_EXPLORER_HOME"] = _TMP.name

from pi_explorer import engine, hunt, lab, search, stats, art  # noqa: E402

# The first 200 digits after the decimal point, from the published value.
PI_200 = (
    "14159265358979323846264338327950288419716939937510"
    "58209749445923078164062862089986280348253421170679"
    "82148086513282306647093844609550582231725359408128"
    "48111745028410270193852110555964462294895493038196"
)

DIGITS = 200_000
_TAPE: engine.Tape | None = None


def setUpModule() -> None:
    global _TAPE
    engine.VAULT = Path(_TMP.name)
    text = engine.compute_pi(DIGITS, workers=1, chatty=False)
    engine.write_vault(text, "test", 0.5)
    _TAPE = engine.Tape(engine.vault_path(), engine.vault_meta())


def tearDownModule() -> None:
    if _TAPE is not None:
        _TAPE.close()
    _TMP.cleanup()


class TestEngine(unittest.TestCase):
    def test_known_digits(self):
        self.assertEqual(_TAPE.get(0, 200), PI_200)

    def test_length(self):
        self.assertEqual(len(_TAPE), DIGITS)

    def test_parallel_matches_serial(self):
        """Big enough that the parallel path uses the limb-split multiply and
        the precision trim, so this covers both."""
        serial = engine.compute_pi(150_000, workers=1, chatty=False)
        parallel = engine.compute_pi(150_000, workers=4, chatty=False)
        self.assertEqual(serial, parallel)
        self.assertTrue(serial.startswith(PI_200))
        self.assertEqual(len(serial), 150_000)

    def test_worker_count_scales_with_the_job(self):
        threads = engine.profile_machine().threads
        tiny = max(1, min(threads - 1, 10_000 // engine.DIGITS_PER_WORKER))
        self.assertEqual(tiny, 1, "a tiny job must not spawn a pool")
        big = max(1, min(threads - 1,
                         10_000_000 // engine.DIGITS_PER_WORKER))
        self.assertEqual(big, max(1, threads - 1))

    def test_verify_rejects_impostors(self):
        self.assertTrue(engine.verify_digits(PI_200))
        self.assertFalse(engine.verify_digits("14159265358979323846264338327951"))

    def test_machine_profile_is_sane(self):
        profile = engine.profile_machine()
        self.assertGreaterEqual(profile.threads, 1)
        self.assertGreaterEqual(profile.workers, 1)
        self.assertGreater(profile.ram_total, 0)
        self.assertGreater(engine.max_safe_digits(), 10_000)


class TestParallelMultiply(unittest.TestCase):
    """The limb-split multiply is the load-bearing optimisation, so it gets
    checked against plain `*` at every shape that matters."""

    @classmethod
    def setUpClass(cls):
        from concurrent.futures import ProcessPoolExecutor
        cls.pool = ProcessPoolExecutor(max_workers=2)
        cls.pool.submit(engine._worker_mul, (2, 3)).result()

    @classmethod
    def tearDownClass(cls):
        cls.pool.shutdown()

    def test_matches_plain_multiplication(self):
        import random
        rng = random.Random(20260906)
        cases = []
        for bits in (600_000, 900_000):
            a = rng.getrandbits(bits) | (1 << (bits - 1))
            b = rng.getrandbits(bits // 2) | (1 << (bits // 2 - 1))
            cases += [(a, b), (-a, b), (a, -b), (-a, -b)]
        cases.append((0, rng.getrandbits(600_000)))
        got = engine.pmul_many(cases, self.pool, workers=8)
        for (a, b), product in zip(cases, got):
            self.assertEqual(product, a * b)

    def test_small_operands_bypass_the_pool(self):
        pairs = [(123456789, 987654321), (2 ** 40, 3 ** 25)]
        self.assertEqual(engine.pmul_many(pairs, self.pool, workers=8),
                         [a * b for a, b in pairs])

    def test_works_without_a_pool(self):
        pairs = [(7, 6), (-3, 11)]
        self.assertEqual(engine.pmul_many(pairs, None, workers=8), [42, -33])

    def test_limb_split_never_exceeds_three(self):
        for workers in (1, 2, 4, 8, 16, 64, 256):
            for pairs in (1, 3, 8, 40):
                k = engine._limb_split(workers, pairs)
                self.assertGreaterEqual(k, 1)
                self.assertLessEqual(k, 3)

    def test_limbs_reassemble(self):
        import random
        value = random.Random(1).getrandbits(5000)
        for k in (1, 2, 3):
            limbs, shift = engine._limbs(value, k)
            self.assertEqual(sum(l << (i * shift) for i, l in enumerate(limbs)),
                             value)


class TestPrecisionTrim(unittest.TestCase):
    def test_trim_preserves_the_quotient(self):
        """Dropping low-order bits of Q and T must not move a single digit of
        the answer, or the whole optimisation is worthless."""
        import random
        rng = random.Random(7)
        prec = 2_000
        target = 10 ** prec
        for _ in range(6):
            q = rng.getrandbits(40_000) | (1 << 39_999)
            t = rng.getrandbits(40_000) | (1 << 39_999)
            exact = (q * target) // t
            tq, tt = engine._trim_ratio(q, t, prec)
            trimmed = (tq * target) // tt
            self.assertEqual(exact, trimmed)

    def test_trim_is_a_no_op_when_already_small(self):
        q, t = 12345, 678
        self.assertEqual(engine._trim_ratio(q, t, 10_000), (q, t))

    def test_trim_actually_shrinks_big_operands(self):
        q = 1 << 200_000
        t = (1 << 200_000) + 12345
        tq, tt = engine._trim_ratio(q, t, 1_000)
        self.assertLess(tq.bit_length(), q.bit_length())
        self.assertLess(tt.bit_length(), t.bit_length())


class TestVaultWriting(unittest.TestCase):
    def test_can_rewrite_the_vault_while_it_is_mapped(self):
        """Regression: on Windows, replacing pi.dat while a Tape still had it
        memory-mapped failed with 'WinError 5: Access is denied'. That is what
        happened when you ran `compute` after a `find` in the same session."""
        global _TAPE
        everything = _TAPE.get(0, DIGITS)
        held = engine.Tape(engine.vault_path(), engine.vault_meta())
        try:
            engine.write_vault(everything[:50_000], "test-rewrite", 0.1)
            self.assertEqual(engine.vault_meta()["digits"], 50_000)
        finally:
            held.close()
            engine.write_vault(everything, "test", 0.5)
            _TAPE = engine.Tape(engine.vault_path(), engine.vault_meta())
        self.assertEqual(len(_TAPE), DIGITS)
        self.assertEqual(_TAPE.get(0, 200), PI_200)

    def test_open_tapes_are_tracked(self):
        before = len(list(engine._OPEN_TAPES))
        tape = engine.Tape(engine.vault_path(), engine.vault_meta())
        self.assertEqual(len(list(engine._OPEN_TAPES)), before + 1)
        tape.close()
        self.assertEqual(len(list(engine._OPEN_TAPES)), before)


class TestSearch(unittest.TestCase):
    """The counts are the whole product, so they get checked the dumb way."""

    def _brute(self, text: str, needle: str) -> tuple[int, int]:
        count = sum(1 for i in range(len(text) - len(needle) + 1)
                    if text.startswith(needle, i))
        return text.find(needle), count

    def test_counts_match_brute_force(self):
        text = _TAPE.get(0, DIGITS)
        for needle in ("1", "42", "314", "1234", "12345", "999999",
                       "11", "1111", "123123", "0000", "271828"):
            first, count = self._brute(text, needle)
            result = search.scan(_TAPE, needle)
            self.assertEqual(result.first, first, needle)
            self.assertEqual(result.count, count, needle)

    def test_self_overlapping_needles(self):
        """'11' overlaps itself, so the fast block count must not be used."""
        text = _TAPE.get(0, DIGITS)
        for needle in ("11", "111", "1212", "99999"):
            _first, count = self._brute(text, needle)
            self.assertEqual(search.scan(_TAPE, needle).count, count, needle)

    def test_feynman_point(self):
        result = search.scan(_TAPE, "999999")
        self.assertEqual(result.position, hunt.FEYNMAN_POINT)

    def test_window_is_respected(self):
        full = search.scan(_TAPE, "123")
        small = search.scan(_TAPE, "123", limit=10_000)
        self.assertLess(small.count, full.count)
        self.assertEqual(small.searched, 10_000)

    def test_missing_needle(self):
        result = search.scan(_TAPE, "1234567890123")
        self.assertFalse(result.found)
        self.assertEqual(result.count, 0)

    def test_rejects_non_digits(self):
        with self.assertRaises(ValueError):
            search.scan(_TAPE, "hello")


class TestText(unittest.TestCase):
    def test_a1z26_encoding(self):
        self.assertEqual(search.encode_text("cat", "a1z26"), "030120")
        self.assertEqual(search.encode_text("Pi!", "a1z26"), "1609")

    def test_ascii_encoding(self):
        self.assertEqual(search.encode_text("Hi", "ascii"), "072105")

    def test_letter_stream_matches_decode(self):
        book = search.letter_stream(_TAPE, 20_000)
        self.assertEqual(len(book), 10_000)
        self.assertEqual(book[:20], search.decode_letters(_TAPE, 0, 20))

    def test_letter_stream_alphabet(self):
        book = search.letter_stream(_TAPE, 5_000)
        self.assertTrue(all("a" <= ch <= "z" for ch in book))

    def test_word_position_maps_back_to_digits(self):
        book = search.letter_stream(_TAPE, DIGITS)
        at = book.find("cat")
        self.assertGreaterEqual(at, 0)
        self.assertEqual(search.decode_letters(_TAPE, at * 2, 3), "cat")


class TestDates(unittest.TestCase):
    def test_parse_formats(self):
        for text in ("2006-07-09", "7/9/2006", "July 9 2006", "9 July 2006"):
            self.assertEqual(search.parse_date(text), (2006, 7, 9))

    def test_two_digit_year(self):
        self.assertEqual(search.parse_date("7/9/06"), (2006, 7, 9))
        self.assertEqual(search.parse_date("7/9/95"), (1995, 7, 9))

    def test_variants(self):
        variants = search.date_variants(2006, 7, 9)
        self.assertEqual(variants["MM/DD/YYYY"], "07092006")
        self.assertEqual(variants["YYYY-MM-DD"], "20060709")

    def test_bad_date(self):
        with self.assertRaises(ValueError):
            search.parse_date("not a date")


class TestStats(unittest.TestCase):
    def test_normal_tail(self):
        self.assertAlmostEqual(stats.normal_sf(1.959964), 0.025, places=5)
        self.assertAlmostEqual(stats.normal_sf(0.0), 0.5, places=9)

    def test_chi2_against_known_values(self):
        # Textbook: the 95th percentile of chi-square with 9 df is 16.919.
        self.assertAlmostEqual(stats.chi2_sf(16.919, 9), 0.05, places=4)
        self.assertAlmostEqual(stats.chi2_sf(3.325, 10), 0.9727, places=3)

    def test_chi2_huge_df_uses_approximation(self):
        p = stats.chi2_sf(1_000_000, 999_999)
        self.assertGreater(p, 0.4)
        self.assertLess(p, 0.6)

    def test_poisson(self):
        self.assertAlmostEqual(stats.poisson_at_least_one(2.0),
                               1 - 2.718281828 ** -2, places=6)
        self.assertGreater(stats.poisson_two_sided(20, 20.0), 0.5)
        self.assertLess(stats.poisson_two_sided(60, 20.0), 1e-9)

    def test_search_odds(self):
        # A 6-digit string in 2M digits: expected 2, so 1-e^-2 chance.
        self.assertAlmostEqual(stats.expected_hits(2_000_000, 6), 2.0, places=3)
        self.assertAlmostEqual(stats.prob_appears(2_000_000, 6), 0.8646, places=3)


class TestLab(unittest.TestCase):
    def test_battery_runs_and_passes_on_pi(self):
        results, counts = lab.battery(_TAPE.raw(0, DIGITS))
        self.assertEqual(sum(counts), DIGITS)
        self.assertTrue(results)
        for res in results:
            if res.p is not None:
                self.assertGreaterEqual(res.p, 0.0)
                self.assertLessEqual(res.p, 1.0)

    def test_battery_catches_a_rigged_stream(self):
        """A stream with no 7s in it had better fail the frequency test."""
        rigged = (b"0123456890" * (DIGITS // 10))
        results, _ = lab.battery(rigged)
        freq = next(r for r in results if r.key == "freq")
        self.assertLess(freq.p, 1e-9)

    def test_synthetic_sources(self):
        for kind in ("mt", "os"):
            data = lab.synthesize(kind, 5_000)
            self.assertEqual(len(data), 5_000)
            self.assertTrue(all(48 <= b <= 57 for b in data))

    def test_pair_matrix_totals(self):
        grid = lab.pair_matrix(_TAPE.raw(0, 10_000))
        self.assertEqual(sum(sum(row) for row in grid), 9_999)


class TestHunt(unittest.TestCase):
    def test_feynman_point(self):
        self.assertEqual(_TAPE.get(hunt.FEYNMAN_POINT - 1, 6), "999999")

    def test_known_self_locating_strings(self):
        found = {f.detail for f in hunt.self_locating(_TAPE, DIGITS, cap=8)}
        self.assertIn("1", found)
        self.assertIn("16470", found)
        self.assertIn("44899", found)

    def test_palindrome_is_a_palindrome(self):
        finding = hunt.longest_palindrome(_TAPE, 100_000)
        self.assertEqual(finding.detail, finding.detail[::-1])
        actual = _TAPE.get(finding.position - 1, len(finding.detail))
        self.assertEqual(actual, finding.detail)

    def test_palindrome_is_maximal(self):
        """Nothing longer may exist anywhere in the window."""
        text = _TAPE.get(0, 100_000)
        finding = hunt.longest_palindrome(_TAPE, 100_000)
        longer = len(finding.detail) + 1
        for i in range(len(text) - longer + 1):
            chunk = text[i:i + longer]
            self.assertNotEqual(chunk, chunk[::-1])

    def test_long_runs_are_real(self):
        for finding in hunt.long_runs(_TAPE, DIGITS, minimum=6):
            self.assertEqual(len(set(finding.detail)), 1)
            self.assertEqual(_TAPE.get(finding.position - 1, len(finding.detail)),
                             finding.detail)

    def test_digit_deserts(self):
        for finding in hunt.digit_deserts(_TAPE, 50_000):
            digit = finding.label.split()[-1]
            length = int(finding.detail.split()[0])
            stretch = _TAPE.get(finding.position - 1, length)
            self.assertNotIn(digit, stretch)

    def test_first_occurrences(self):
        first = hunt.first_occurrences(_TAPE, 3, 50_000)
        self.assertEqual(len(first), 1000)
        for code, position in enumerate(first):
            if position > 0:
                self.assertEqual(_TAPE.get(position - 1, 3), f"{code:03d}")


class TestShapes(unittest.TestCase):
    def test_parse_shape(self):
        self.assertEqual(search.parse_shape("##/.#"), ["11", "01"])
        self.assertEqual(search.parse_shape("#.#"), ["101"])
        self.assertEqual(search.parse_shape("square3"), ["111", "111", "111"])

    def test_ragged_rows_are_padded(self):
        self.assertEqual(search.parse_shape("###/#"), ["111", "100"])

    def test_bit_tape_is_binary(self):
        bits = search.bit_tape(_TAPE, 1000, "half")
        self.assertEqual(set(bits), {48, 49})
        self.assertEqual(len(bits), 1000)

    def test_half_and_parity_rules(self):
        self.assertEqual(b"0123456789".translate(
            search.BITS_RULES["half"]), b"0000011111")
        self.assertEqual(b"0123456789".translate(
            search.BITS_RULES["parity"]), b"0101010101")

    def test_found_shape_is_really_there(self):
        shape = search.parse_shape("square3")
        hit, _widths = search.find_shape(_TAPE, shape, limit=DIGITS,
                                         max_width=64, workers=1)
        self.assertIsNotNone(hit)
        width, offset = hit
        bits = search.bit_tape(_TAPE, DIGITS, "half")
        # The claimed hit must actually match, row by row, at that stride.
        for row_index, row in enumerate(shape):
            start = offset + row_index * width
            self.assertEqual(bits[start:start + len(row)].decode(), row)
        # ...and must not wrap around the edge of the grid.
        self.assertLessEqual(offset % width + len(shape[0]), width)

    def test_impossible_shape_is_not_found(self):
        big = ["1" * 8 for _ in range(8)]  # 64 bits: 1 in 18 quintillion
        hit, _ = search.find_shape(_TAPE, big, limit=DIGITS, max_width=32,
                                   workers=1)
        self.assertIsNone(hit)

    def test_worker_plan_respects_its_bounds(self):
        for widths in (1, 8, 124):
            for limit in (10_000, 2_000_000, 500_000_000):
                for span in (3, 6, 12):
                    n = search.plan_shape_workers(widths, limit, span,
                                                  ceiling=15)
                    self.assertGreaterEqual(n, 1)
                    self.assertLessEqual(n, 15)
                    self.assertLessEqual(n, widths)

    def test_worker_plan_is_modest_for_small_work(self):
        """Oversizing this pool made a 0.65s scan take 2.65s."""
        self.assertLessEqual(
            search.plan_shape_workers(124, 2_000_000, 5, ceiling=15), 6)

    def test_worker_plan_scales_up_for_real_work(self):
        self.assertEqual(
            search.plan_shape_workers(124, 1_000_000_000, 5, ceiling=15), 15)

    def test_explicit_worker_count_wins(self):
        self.assertEqual(
            search.plan_shape_workers(124, 2_000_000, 5, ceiling=15,
                                      workers=1), 1)

    def test_shape_odds(self):
        expected, needed = search.shape_odds(["1111"] * 4, 1_000_000, 100)
        self.assertAlmostEqual(expected, 1_000_000 * 100 / 2 ** 16, places=3)
        self.assertGreater(needed, 0)


class TestArt(unittest.TestCase):
    def test_png_header_and_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "wall.png"
            path, w, h = art.wall_png(_TAPE, out, cols=100, rows=50, scale=2)
            self.assertEqual((w, h), (200, 100))
            blob = path.read_bytes()
            self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(b"IHDR", blob[:32])
            self.assertTrue(blob.rstrip().endswith(b"\xaeB`\x82"))

    def test_walk_step_length(self):
        points = art.walk_points(_TAPE, 100)
        self.assertEqual(len(points), 101)
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            step = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
            self.assertAlmostEqual(step, 1.0, places=9)

    def test_svg_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = art.walk_svg(_TAPE, Path(tmp) / "walk.svg", 2000)
            text = out.read_text()
            self.assertTrue(text.startswith("<svg"))
            self.assertTrue(text.rstrip().endswith("</svg>"))

    def test_palettes_are_ten_colours(self):
        for name, palette in art.PALETTES.items():
            self.assertEqual(len(palette), 10, name)
            for colour in palette:
                self.assertEqual(len(colour), 3, name)


class TestCLI(unittest.TestCase):
    def test_parser_accepts_every_command(self):
        from pi_explorer.cli import build_parser
        parser = build_parser()
        for argv in (["find", "123"], ["compute", "--auto"], ["random", "--deep"],
                     ["shape", "smiley"], ["wall", "--out", "x.png"],
                     ["birthday", "2006-07-09"], ["text", "cat"],
                     ["hunt"], ["odds"], ["walk", "1000"], ["quiz"],
                     ["dump", "1", "10"], ["decode", "1"], ["info"],
                     ["bench"], ["shapes"], ["rain"], ["shell"]):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(hasattr(args, "func"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
