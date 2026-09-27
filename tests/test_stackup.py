"""叠层解析与 SI9000 参数映射。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jlc_impedance.stackup import BUILTIN_STACKUPS, Dielectric, Stackup, match_name  # noqa: E402


class TestMatchName(unittest.TestCase):
    def test_requires_boundary(self):
        """不带括号的 key 必须边界匹配，否则会命中 A/B/C 变体。"""
        self.assertTrue(match_name('JLC04161H-3313(成品板厚1.56mm)', 'JLC04161H-3313'))
        self.assertTrue(match_name('JLC04161H-3313', 'JLC04161H-3313'))
        self.assertFalse(match_name('JLC04161H-3313A(特殊)', 'JLC04161H-3313'))

    def test_key_with_annotation(self):
        """key 已经带官方标注时，直接前缀匹配。"""
        self.assertTrue(match_name('JLC06161H-3313(免费/成品1.54mm)', 'JLC06161H-3313(免费'))
        self.assertFalse(match_name('JLC06161H-3313A(x)', 'JLC06161H-3313(免费'))


class TestStackup(unittest.TestCase):
    def test_index_of(self):
        st = BUILTIN_STACKUPS['JLC04161H-7628']
        self.assertEqual(st.index_of('L1'), 0)
        self.assertEqual(st.index_of('F.Cu'), 0)
        self.assertEqual(st.index_of('In1'), 1)
        self.assertEqual(st.index_of('L4'), 3)
        self.assertEqual(st.index_of('B.Cu'), 3)

    def test_outer_inner_geometry(self):
        """外层只有 H1；内层是「芯板当 H1、另一侧+铜厚当 H2」。"""
        st = BUILTIN_STACKUPS['JLC04161H-7628']
        l1 = st.si9000_geometry('L1')
        self.assertNotIn('H2', l1)
        self.assertAlmostEqual(l1['H1'], 0.2104 / 0.0254, places=3)
        self.assertAlmostEqual(l1['Er1'], 4.40, places=6)
        l2 = st.si9000_geometry('L2')
        self.assertAlmostEqual(l2['H1'], 1.065 / 0.0254, places=3)   # 芯板
        self.assertAlmostEqual(l2['Er1'], 4.38, places=6)
        # H2 = 另一侧介质 + 该层铜厚
        self.assertAlmostEqual(l2['H2'], 0.2104 / 0.0254 + 0.0152 / 0.0254, places=3)
        self.assertAlmostEqual(l2['T1'], 0.6, places=6)              # 内层 0.5oz

    def test_nominal_thickness(self):
        st = BUILTIN_STACKUPS['JLC04161H-3313']
        self.assertAlmostEqual(st.nominal_mm, 0.0994 + 1.265 + 0.0994 + 2 * 0.035
                               + 2 * 0.0152, places=6)

    def test_merge_consecutive_prepreg(self):
        """连续多张 PP 必须合并成一层（否则导体/介质数量对不上）。"""
        tpl = {'laminatedConstructionCode': 'T', 'basicDataList': [
            {'materialType': 1, 'materialName': '1oz', 'topConductorThick': 0.035},
            {'materialType': 0, 'material': 'PP', 'materialName': '1080 3.3mil',
             'dielectricThick': 0.0764, 'dielectricConstant': 3.91},
            {'materialType': 0, 'material': 'PP', 'materialName': '1080 3.3mil',
             'dielectricThick': 0.0764, 'dielectricConstant': 3.91},
            {'materialType': 2, 'material': '芯板', 'materialName': 'core',
             'dielectricThick': 0.4, 'dielectricConstant': 4.36,
             'topConductorThick': 0.0152, 'botConductorThick': 0.0152},
            {'materialType': 0, 'material': 'PP', 'materialName': '1080 3.3mil',
             'dielectricThick': 0.0764, 'dielectricConstant': 3.91},
            {'materialType': 1, 'materialName': '1oz', 'topConductorThick': 0.035},
        ]}
        st = Stackup.from_template(tpl)
        self.assertEqual(st.copper_layers, 4)
        self.assertEqual(len(st.dielectrics), 3)
        self.assertAlmostEqual(st.dielectrics[0].thickness_mm, 0.1528, places=6)
        self.assertAlmostEqual(st.dielectrics[0].dk, 3.91, places=6)
        self.assertAlmostEqual(st.dielectrics[1].thickness_mm, 0.4, places=6)

    def test_builtin_stackups_shipped(self):
        for name in ('JLC04161H-3313', 'JLC04161H-7628', 'JLC0216A'):
            with self.subTest(name=name):
                st = BUILTIN_STACKUPS[name]
                self.assertGreaterEqual(st.copper_layers, 2)
                self.assertTrue(st.describe())


if __name__ == '__main__':
    unittest.main()
