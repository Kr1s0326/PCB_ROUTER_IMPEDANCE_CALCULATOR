# -*- coding: utf-8 -*-
"""校验图（``tools/make_validation_figure.py``）的离线测试。

分两层：

* **数学层** —— ``Lin`` 映射、``nice_ticks`` 刻度、``cdf``/``histogram`` 统计，
  这些是图的正确性根基，必须精确。
* **产物层** —— 把图渲染到临时目录，检查生成的 SVG：XML 良构、无 nan/inf、
  坐标不越界、散点数与有效样本数一致、四个面板标题都在。

产物层同时检查仓库里已提交的那份 SVG（存在才查），保证「提交的图」和
「脚本现在能生成的图」不会悄悄脱节。
"""

import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import make_validation_figure as fig  # noqa: E402

VALIDATION_JSON = os.path.join(ROOT, 'reports', 'validation_all.json')
COMMITTED_SVG = os.path.join(ROOT, 'reports', 'figures', 'validation.svg')


def valid_rows():
    with open(VALIDATION_JSON, encoding='utf-8') as fh:
        data = json.load(fh)
    return [r for r in data['rows'] if 'w_err' in r]


# ---------------------------------------------------------------- 数学层
class LinTests(unittest.TestCase):
    def test_endpoints_map_exactly(self):
        s = fig.Lin(0.0, 10.0, 100.0, 300.0)
        self.assertAlmostEqual(s(0.0), 100.0)
        self.assertAlmostEqual(s(10.0), 300.0)
        self.assertAlmostEqual(s(5.0), 200.0)

    def test_inverted_axis(self):
        s = fig.Lin(0.0, 10.0, 300.0, 100.0)     # y 轴向上
        self.assertAlmostEqual(s(0.0), 300.0)
        self.assertAlmostEqual(s(10.0), 100.0)
        self.assertGreater(s(1.0), s(9.0))

    def test_roundtrip(self):
        s = fig.Lin(-3.0, 7.0, 12.0, 480.0)
        for v in (-3.0, 0.0, 2.5, 7.0):
            self.assertAlmostEqual(s.inv(s(v)), v, places=9)

    def test_degenerate_range_does_not_raise(self):
        s = fig.Lin(5.0, 5.0, 0.0, 100.0)         # 单点数据不能除零
        self.assertEqual(s.k, 0.0)


class TicksTests(unittest.TestCase):
    def test_within_range_and_sorted(self):
        for lo, hi in ((0, 8), (20, 120), (0, 37), (0.4, 2.6)):
            t = fig.nice_ticks(lo, hi, 5)
            self.assertTrue(t, (lo, hi))
            self.assertEqual(t, sorted(t))
            self.assertGreaterEqual(t[0], lo - 1e-9)
            self.assertLessEqual(t[-1], hi + 1e-9)

    def test_evenly_spaced(self):
        t = fig.nice_ticks(0, 100, 5)
        d = [round(b - a, 9) for a, b in zip(t, t[1:])]
        self.assertEqual(len(set(d)), 1, d)

    def test_range_yields_multiple_ticks(self):
        self.assertGreaterEqual(len(fig.nice_ticks(0, 8, 4)), 3)


class StatsTests(unittest.TestCase):
    def test_cdf_monotone_and_ends_at_hundred(self):
        vals = [-3.0, 1.0, 2.0, 2.0, 7.5, 9.0]
        pts = fig.cdf(vals, 8.0)
        ys = [y for _, y in pts]
        self.assertEqual(ys, sorted(ys))                     # 单调不减
        self.assertAlmostEqual(ys[-1], 100.0 * 5 / 6)        # 9.0 超出 xmax，被截掉

    def test_cdf_starts_at_origin(self):
        pts = fig.cdf([1.0, 2.0], 5.0)
        self.assertEqual(pts[0], (0.0, 0.0))

    def test_histogram_conserves_count(self):
        vals = [0.1, 0.7, 1.2, 1.9, 4.4, 4.9, 6.02]
        bins, bw = fig.histogram(vals, 8.0, 0.5)
        self.assertEqual(sum(bins), len(vals))
        self.assertEqual(bw, 0.5)
        self.assertEqual(len(bins), 16)

    def test_histogram_clamps_overflow(self):
        bins, _ = fig.histogram([99.0], 4.0, 0.5)
        self.assertEqual(sum(bins), 1)
        self.assertEqual(bins[-1], 1)


# ---------------------------------------------------------------- 产物层
class RenderedSvgTests(unittest.TestCase):
    """把图渲染到临时目录再检查。"""

    @classmethod
    def setUpClass(cls):
        cls.rows = valid_rows()
        cls.tmp = tempfile.mkdtemp(prefix='jlcfig')
        cls.path = os.path.join(cls.tmp, 'validation.svg')
        fig.main(['--out', cls.path])
        with open(cls.path, encoding='utf-8') as fh:
            cls.raw = fh.read()
        cls.root = ET.fromstring(cls.raw)          # XML 良构，失败即抛错

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _tag(self, name):
        return [e for e in self.root.iter() if e.tag.split('}')[-1] == name]

    def test_wellformed_and_has_namespace(self):
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', self.raw)

    def test_no_nan_or_inf_or_none(self):
        for bad in ('nan', 'NaN', 'inf', 'Infinity', 'None'):
            self.assertNotIn('="%s' % bad, self.raw)

    def test_size_attributes(self):
        self.assertEqual(self.root.get('width'), '1140')
        self.assertEqual(self.root.get('height'), '800')

    def test_all_coordinates_inside_canvas(self):
        w, h = 1140, 800
        lim = max(w, h) + 1
        for e in self.root.iter():
            for k in ('x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy'):
                v = e.get(k)
                if v is None:
                    continue
                self.assertGreaterEqual(float(v), -1.0, (e.tag, k, v))
                self.assertLessEqual(float(v), lim, (e.tag, k, v))
            pts = e.get('points')
            if pts:
                for pair in pts.split():
                    for v in pair.split(','):
                        self.assertGreaterEqual(float(v), -1.0)
                        self.assertLessEqual(float(v), lim)

    def test_scatter_has_one_point_per_valid_row(self):
        marks = [e for e in self._tag('circle')
                 if e.get('r') == '2.60' and e.get('opacity') == '0.620']
        self.assertEqual(len(marks), len(self.rows))

    def test_four_panel_titles_present(self):
        text = ' '.join(e.text or '' for e in self._tag('text'))
        for kw in ('①', '②', '③', '④'):
            self.assertIn(kw, text)
        self.assertIn('正算', text)
        self.assertIn('反算', text)

    def test_reports_skip_count_in_header(self):
        with open(VALIDATION_JSON, encoding='utf-8') as fh:
            total = len(json.load(fh)['rows'])
        skipped = total - len(self.rows)
        text = ' '.join(e.text or '' for e in self._tag('text'))
        self.assertIn('有效样本 %d 组' % len(self.rows), text)
        self.assertIn('另有 %d 组' % skipped, text)

    def test_has_reference_bands_and_axis_grid(self):
        self.assertGreaterEqual(len(self._tag('polygon')), 2)   # ±1% / ±2% 参考带
        self.assertGreaterEqual(len(self._tag('line')), 10)     # 网格与轴


class CommittedSvgTests(unittest.TestCase):
    """仓库里已提交的图必须与脚本当前能生成的图结构一致。"""

    def setUp(self):
        if not os.path.exists(COMMITTED_SVG):
            self.skipTest('尚未生成 %s' % COMMITTED_SVG)

    def test_committed_svg_is_wellformed_and_complete(self):
        with open(COMMITTED_SVG, encoding='utf-8') as fh:
            raw = fh.read()
        root = ET.fromstring(raw)
        circles = [e for e in root.iter()
                   if e.tag.split('}')[-1] == 'circle' and e.get('r') == '2.60']
        self.assertEqual(len(circles), len(valid_rows()))

    def test_committed_svg_has_no_bad_numbers(self):
        with open(COMMITTED_SVG, encoding='utf-8') as fh:
            raw = fh.read()
        self.assertNotIn('nan', raw.lower())


if __name__ == '__main__':
    unittest.main()
