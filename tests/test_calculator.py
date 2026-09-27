"""校准层与高层门面。"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from impedance_calculator import ImpedanceCalculator, calibration         # noqa: E402
from impedance_calculator.stackup import BUILTIN_STACKUPS                 # noqa: E402


class TestCalibration(unittest.TestCase):
    def test_all_structures_calibrated(self):
        self.assertEqual(len(calibration.CALIBRATION), 12)
        self.assertEqual(len(calibration.KRRS), 12)

    def test_quality_within_bounds(self):
        """每个结构都应报出可信度，且不超过 15%。"""
        for mark in calibration.CALIBRATION:
            with self.subTest(mark=mark):
                cv = calibration.width_cv(mark)
                self.assertIsNotNone(cv)
                self.assertLess(cv, 15.0)
                self.assertIn('离线模型', calibration.quality_note(mark))

    def test_correction_is_bounded(self):
        """修正系数被夹在 exp(±1)，外插时不会失控。"""
        p = {'H1': 8.2835, 'Er1': 4.40, 'H2': 41.9291, 'Er2': 4.38,
             'W1': 10.0, 'W2': 9.5, 'T1': 0.6}
        for w in (0.5, 1, 5, 50, 500):
            q = dict(p, W1=w, W2=max(w - 0.5, 0.05))
            z, factor = calibration.apply_correction('OffsetStripline1B1A', q, 50.0)
            self.assertLessEqual(factor, math.e + 1e-9)
            self.assertGreaterEqual(factor, 1.0 / math.e - 1e-9)

    def test_unknown_structure_passthrough(self):
        z, factor = calibration.apply_correction('NoSuchModel', {}, 42.0)
        self.assertEqual((z, factor), (42.0, 1.0))


class TestCalculator(unittest.TestCase):
    def setUp(self):
        self.stack = BUILTIN_STACKUPS['JLC04161H-7628']
        self.calc = ImpedanceCalculator(self.stack)

    def test_offline_engine_label(self):
        self.assertEqual(self.calc.engine, 'analytic')

    def test_solve_single(self):
        s = self.calc.solve('L1', 50.0)
        self.assertIsNotNone(s.width)
        self.assertGreater(s.width, 1.0)
        self.assertAlmostEqual(s.width_mm, s.width * 0.0254, places=6)
        self.assertEqual(s.impedance_type, 'CoatedMicrostrip1B')
        self.assertIn('离线模型', s.note)
        self.assertIn('width_mil', s.as_dict())

    def test_solve_inner_uses_stripline(self):
        s = self.calc.solve('L2', 50.0)
        self.assertEqual(s.impedance_type, 'OffsetStripline1B1A')
        self.assertIsNotNone(s.width)

    def test_solve_diff(self):
        s = self.calc.solve('L1', 100.0, kind='diff', spacing=8.0)
        self.assertEqual(s.impedance_type, 'DiffEdgeCoupledCoatedMicrostrip1B')
        self.assertAlmostEqual(s.spacing, 8.0, places=6)

    def test_solve_diff_by_spacing(self):
        """给线宽反算线距。"""
        s = self.calc.solve('L1', 100.0, kind='diff', width=8.0)
        self.assertIsNotNone(s.spacing)
        self.assertGreater(s.spacing, 1.0)

    def test_forward_matches_solve_target(self):
        s = self.calc.solve('L1', 50.0)
        f = self.calc.forward('L1', width=s.width)
        self.assertLess(abs(f.impedance - 50.0), 0.05)

    def test_thin_layer_raises(self):
        with self.assertRaises(ValueError):
            self.calc.solve('L9', 50.0)


if __name__ == '__main__':
    unittest.main()
