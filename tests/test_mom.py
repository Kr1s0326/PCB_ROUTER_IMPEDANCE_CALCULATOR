"""MoM 精确场解的正确性（用官网 T=0.01 的实测值做基准）。

这是整个离线模型里**最硬**的一块：它把 T→0 的二维场问题解到 0.1%，
基准值是 ``tools/check_engine.py`` 从官网采下来的 ``data/zerocopper.jsonl``。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from impedance_calculator import mom                                    # noqa: E402

ZEROCOPPER = os.path.join(ROOT, 'data', 'zerocopper.jsonl')


class TestMoM(unittest.TestCase):
    def test_microstrip_matches_hammerstad_limit(self):
        """T→0 的微带线：MoM 真空电容 + Hammerstad εeff 应与教科书公式一致。"""
        from impedance_calculator import analytic
        for w, h, er in ((5, 10, 4.2), (8, 10, 4.2), (20, 10, 4.2)):
            with self.subTest(w=w):
                zv = mom.z0_vacuum(w, h, None)
                eeff = analytic._microstrip_eeff(w / h, er, 1.0)
                z = zv / (eeff ** 0.5)
                zh = analytic._microstrip_z0_ratio(analytic._thickness_adjusted(w, 0.01) / h,
                                                   eeff)
                self.assertLess(abs(z - zh) / zh, 0.01)

    def test_symmetric_invariance(self):
        """偏置带状线交换 H1/H2 必须给出相同的阻抗（结构本身对称）。"""
        z1, _ = mom.z0_single(8, 8, 5, 4.3, 20, 4.3)
        z2, _ = mom.z0_single(8, 8, 20, 4.3, 5, 4.3)
        self.assertAlmostEqual(z1, z2, places=6)

    def test_diff_decouples_with_spacing(self):
        """差分间距越大越解耦：Zdiff → 2×Z0，且单调递增。"""
        zs, _ = mom.z0_single(8, 8, 20, 4.3, 20, 4.3)
        prev = 0.0
        for s in (4, 8, 16, 32):
            zd, _ = mom.z0_diff(8, 8, s, 20, 4.3, 20, 4.3)
            self.assertGreater(zd, prev)
            self.assertLess(zd, 2 * zs + 1e-9)
            prev = zd
        zd, _ = mom.z0_diff(8, 8, 4096, 20, 4.3, 20, 4.3)
        self.assertAlmostEqual(zd / (2 * zs), 1.0, places=4)

    @unittest.skipUnless(os.path.exists(ZEROCOPPER), '缺少 data/zerocopper.jsonl')
    def test_against_jlc_zero_copper(self):
        """与官网 T=0.01 的实测值比：单端 / 差分都要 < 0.3%。"""
        worst = 0.0
        n = 0
        with open(ZEROCOPPER, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = json.loads(line)
                p, mark = r['params'], r['mark']
                if 'Stripline' not in mark or not r.get('z'):
                    continue
                if mark.startswith('Diff'):
                    z, _ = mom.z0_diff(p['W1'], p['W2'], p['S1'], p['H1'], p['Er1'],
                                       p['H2'], p['Er2'])
                else:
                    z, _ = mom.z0_single(p['W1'], p['W2'], p['H1'], p['Er1'],
                                         p['H2'], p['Er2'])
                worst = max(worst, abs(z - r['z']) / r['z'])
                n += 1
        self.assertGreater(n, 10, '基准样本太少')
        self.assertLess(worst, 0.003, 'MoM 与官网偏差 %.3f%%' % (100 * worst))


if __name__ == '__main__':
    unittest.main()
