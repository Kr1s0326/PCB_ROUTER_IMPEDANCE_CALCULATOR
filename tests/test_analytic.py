"""物理基底与正/反算。"""

from __future__ import annotations

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from impedance_calculator import analytic, mom                           # noqa: E402


def strip_params(h1=8.2835, er1=4.40, h2=41.9291, er2=4.38, t1=0.6):
    return {'H1': h1, 'Er1': er1, 'H2': h2, 'Er2': er2, 'T1': t1}


class TestMicrostrip(unittest.TestCase):
    def test_matches_textbook(self):
        """Hammerstad 微带线：与教材闭式解一致（T→0）。"""
        for w, h, er in ((8, 10, 4.2), (20, 10, 4.2)):
            z, _ = analytic.microstrip(h, er, w, w, 0.01)
            u = w / h
            eeff = (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 / u)
            ref = 120 * math.pi / (math.sqrt(eeff) *
                                   (u + 1.393 + 0.667 * math.log(u + 1.444)))
            self.assertLess(abs(z - ref) / ref, 0.02)

    def test_thicker_copper_lowers_impedance(self):
        p = strip_params()
        prev = 1e9
        for t in (0.01, 0.6, 1.6, 2.4):
            z, _ = analytic.microstrip(p['H1'], p['Er1'], 10, 10 - t / 2, t)
            self.assertLess(z, prev)
            prev = z


class TestStriplineUsesMoM(unittest.TestCase):
    def test_close_to_mom(self):
        """``analytic.stripline`` 现在直接调 MoM，应当与它一致。"""
        z, eeff = analytic.stripline(41.9291, 4.38, 8.8819, 4.40, 10.879, 10.379, 0.6)
        zm, _ = mom.z0_single(analytic._thickness_adjusted((10.879 + 10.379) / 2, 0.6),
                              analytic._thickness_adjusted((10.879 + 10.379) / 2, 0.6),
                              41.9291, 4.38, 8.8819, 4.40)
        self.assertAlmostEqual(z, zm, places=9)

    def test_symmetric(self):
        a, _ = analytic.stripline(20, 4.3, 20, 4.3, 8, 8, 0.6)
        b, _ = analytic.stripline(41.9, 4.3, 4.5, 4.3, 8, 8, 0.01)
        self.assertGreater(a, b)          # 偏置的阻抗更低


class TestDifferential(unittest.TestCase):
    def test_coupling_in_range(self):
        zs = 50.0
        for s in (2.5, 5, 10, 20):
            z = analytic.differential(zs, s, 8.0, False)
            self.assertGreater(z, zs)               # 差分 > 单端
            self.assertLess(z, 2 * zs)              # 但小于 2 倍


class TestSolve(unittest.TestCase):
    def test_roundtrip_width(self):
        """反算出的线宽，回代应命中目标阻抗。"""
        p = strip_params()
        for target in (40.0, 50.0, 75.0):
            w = analytic.solve_width('OffsetStripline1B1A', p, target, 0.5)
            self.assertIsNotNone(w)
            z, _ = analytic.estimate('OffsetStripline1B1A',
                                     dict(p, W1=w, W2=w - 0.5))
            self.assertLess(abs(z - target) / target, 1e-4)

    def test_roundtrip_spacing(self):
        p = dict(strip_params(), W1=6.0, W2=5.5, S1=8.0)
        s = analytic.solve_spacing('DiffOffsetStripline1B1A', p, 100.0)
        self.assertIsNotNone(s)
        z, _ = analytic.estimate('DiffOffsetStripline1B1A', dict(p, S1=s))
        self.assertLess(abs(z - 100.0) / 100.0, 1e-4)

    def test_impedance_decreases_with_width(self):
        p = strip_params()
        zs = [analytic.estimate('OffsetStripline1B1A', dict(p, W1=w, W2=w - 0.5))[0]
              for w in (4, 6, 8, 12, 16, 24)]
        self.assertEqual(zs, sorted(zs, reverse=True))

    def test_no_solution_returns_none(self):
        p = strip_params()
        self.assertIsNone(analytic.solve_width('OffsetStripline1B1A', p, 500.0, 0.5))
