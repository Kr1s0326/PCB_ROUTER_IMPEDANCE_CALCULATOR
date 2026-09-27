"""从嘉立创后台采样（正算 / 反算），用于校准离线模型。

礼貌策略（请勿改成高频请求）
----------------------------
* 单条 WebSocket 长连接复用；
* 每次请求之间随机 sleep ``--delay`` 秒（默认 0.9~1.6s ≈ 0.8 req/s）；
* 断线自动重连；结果**逐条落盘**，可中断续采（按 ``(模型, 参数)`` 去重）。

采样空间（regime）
------------------
======  ==================================================  ==================
regime  说明                                                写入
======  ==================================================  ==================
fill    空间填充：H1/H2 独立取，覆盖最广                     calibration_extra
stackup 内层沿**真实叠层流形**（H1+H2 固定、H2 占比小）      calibration_extra
layer2  外层大 H（20~130 mil，对应 1.6~3.2mm 板）            calibration_extra
oz2     厚铜（T1=1.2/2.4）+ 2oz 阻焊                          calibration_extra
test    **独立留出集**，绝不参与拟合                          holdout
reverse 官网 goal-seek 反算（对拍"正算模型+二分"）            reverse
======  ==================================================  ==================

> 第一轮 750 组只用了 ``fill``，结果内层结构只有 15% 的样本落在真实叠层区间；
> 第二轮按 regime 补采后，内层有效样本从 ~10 涨到 ~100。见
> ``tools/experiments/analyze_coverage.py``。

用法::

    python tools/collect.py --plan            # 只看计划，不发请求
    python tools/collect.py --regime stackup  # 只补某一类
    python tools/collect.py                   # 全部（约 960 个请求 / 28 分钟）
"""

import argparse
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C                                              # noqa: E402
from impedance_calculator.api import JlcApi, JlcApiError                # noqa: E402

OUT_EXTRA = os.path.join(C.DATA, 'calibration_extra.jsonl')

DELTAS = [0.5, 0.7, 1.0, 1.2]          # W1 - W2（蚀刻线宽增量，mil）
COPPERS = [0.6, 1.2, 1.6, 2.4]         # T1
BASES = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43]

#: 每个结构：(模型名, 单端/差分, 内/外层, 是否共面, 是否有阻焊)
STRUCTS = [
    ('SurfaceMicrostrip1B', 'single', 'outer', False, False),
    ('CoatedMicrostrip1B', 'single', 'outer', False, True),
    ('OffsetStripline1B1A', 'single', 'inner', False, False),
    ('SurfaceCoplanarWaveguideWithLowerGnd1B', 'single', 'outer', True, False),
    ('CoatedCoplanarWaveguideWithLowerGnd1B', 'single', 'outer', True, True),
    ('OffsetCoplanarWaveguide1B1A', 'single', 'inner', True, False),
    ('DiffEdgeCoupledSurfaceMicrostrip1B', 'diff', 'outer', False, False),
    ('DiffEdgeCoupledCoatedMicrostrip1B', 'diff', 'outer', False, True),
    ('DiffOffsetStripline1B1A', 'diff', 'inner', False, False),
    ('DiffSurfaceCoplanarWaveguideWithLowerGnd1B', 'diff', 'outer', True, False),
    ('DiffCoatedCoplanarWaveguideWithLowerGnd1B', 'diff', 'outer', True, True),
    ('DiffOffsetCoplanarWaveguide1B1A', 'diff', 'inner', True, False),
]
SPEC = {m: (k, l, c, cv) for m, k, l, c, cv in STRUCTS}

# (mark, regime, count)
PLAN = [
    # A. 内层 4 个结构：真实叠层流形（最高优先级）
    ('OffsetStripline1B1A', 'stackup', 80),
    ('OffsetCoplanarWaveguide1B1A', 'stackup', 80),
    ('DiffOffsetStripline1B1A', 'stackup', 80),
    ('DiffOffsetCoplanarWaveguide1B1A', 'stackup', 80),
    # B. 2 层板（大 H）
    ('SurfaceMicrostrip1B', 'layer2', 35),
    ('CoatedMicrostrip1B', 'layer2', 35),
    ('SurfaceCoplanarWaveguideWithLowerGnd1B', 'layer2', 30),
    ('CoatedCoplanarWaveguideWithLowerGnd1B', 'layer2', 30),
    ('DiffEdgeCoupledSurfaceMicrostrip1B', 'layer2', 35),
    ('DiffEdgeCoupledCoatedMicrostrip1B', 'layer2', 35),
    ('DiffSurfaceCoplanarWaveguideWithLowerGnd1B', 'layer2', 20),
    ('DiffCoatedCoplanarWaveguideWithLowerGnd1B', 'layer2', 20),
    # C. 厚铜 / 2oz 阻焊
    ('CoatedMicrostrip1B', 'oz2', 40),
    ('DiffEdgeCoupledCoatedMicrostrip1B', 'oz2', 40),
    ('OffsetStripline1B1A', 'oz2', 40),
    ('DiffOffsetStripline1B1A', 'oz2', 40),
]
# 独立留出集：每结构 15 组，混合 regime（含 fill）
HOLDOUT_MIX = {
    'OffsetStripline1B1A': ['fill', 'stackup', 'oz2'],
    'OffsetCoplanarWaveguide1B1A': ['fill', 'stackup'],
    'DiffOffsetStripline1B1A': ['fill', 'stackup', 'oz2'],
    'DiffOffsetCoplanarWaveguide1B1A': ['fill', 'stackup'],
    'SurfaceMicrostrip1B': ['fill', 'layer2'],
    'CoatedMicrostrip1B': ['fill', 'layer2', 'oz2'],
    'SurfaceCoplanarWaveguideWithLowerGnd1B': ['fill', 'layer2'],
    'CoatedCoplanarWaveguideWithLowerGnd1B': ['fill', 'layer2'],
    'DiffEdgeCoupledSurfaceMicrostrip1B': ['fill', 'layer2'],
    'DiffEdgeCoupledCoatedMicrostrip1B': ['fill', 'layer2', 'oz2'],
    'DiffSurfaceCoplanarWaveguideWithLowerGnd1B': ['fill', 'layer2'],
    'DiffCoatedCoplanarWaveguideWithLowerGnd1B': ['fill', 'layer2'],
}
HOLDOUT_PER = 15
REVERSE_MARKS = ['CoatedMicrostrip1B', 'OffsetStripline1B1A',
                 'DiffEdgeCoupledCoatedMicrostrip1B', 'DiffOffsetStripline1B1A',
                 'SurfaceCoplanarWaveguideWithLowerGnd1B',
                 'DiffCoatedCoplanarWaveguideWithLowerGnd1B']


# --------------------------------------------------------------------------- #
#  参数生成器
# --------------------------------------------------------------------------- #
def _logu(a, b, x):
    return math.exp(math.log(a) + (math.log(b) - math.log(a)) * x)


def _u(a, b, x):
    return a + (b - a) * x


def _pick(seq, x):
    return seq[min(int(x * len(seq)), len(seq) - 1)]


def gen_fill(kind, layer, coplanar, coated, n):
    p = {'H1': round(_logu(4.0, 60.0, n()), 4), 'Er1': round(_u(3.9, 4.6, n()), 3)}
    if layer == 'inner':
        p['H2'] = round(_logu(4.0, 60.0, n()), 4)
        p['Er2'] = round(_u(3.9, 4.6, n()), 3)
    w1 = round(_u(3.0, 30.0, n()), 3)
    p['W1'] = w1
    p['W2'] = round(w1 - _pick(DELTAS, n()), 3)
    if kind == 'diff':
        p['S1'] = round(_u(3.0, 30.0, n()), 3)
    if coplanar:
        p['D1'] = round(_u(3.0, 30.0, n()), 3)
    p['T1'] = _pick(COPPERS, n())
    if coated:
        p['C1'] = round(_u(0.6, 1.2, n()), 3)
        p['C2'] = round(_u(0.4, 0.8, n()), 3)
        if kind == 'diff':
            p['C3'] = round(_u(0.6, 1.2, n()), 3)
        p['CEr'] = round(_u(3.5, 4.2, n()), 3)
    return p


def gen_stackup(kind, layer, coplanar, coated, n):
    total = _logu(20.0, 75.0, n())
    frac = _logu(0.04, 0.30, n())
    p = {'H1': round(total * (1 - frac), 4), 'H2': round(total * frac, 4),
         'Er1': round(_u(4.00, 4.60, n()), 3), 'Er2': round(_u(4.00, 4.60, n()), 3)}
    w1 = round(_logu(3.0, 20.0, n()), 3)
    p['W1'] = w1
    p['W2'] = round(w1 - _pick([0.5, 0.7, 1.0], n()), 3)
    if kind == 'diff':
        p['S1'] = round(_logu(3.0, 20.0, n()), 3)
    if coplanar:
        p['D1'] = round(_logu(3.0, 20.0, n()), 3)
    p['T1'] = _pick([0.6, 1.2], n())
    return p


def gen_layer2(kind, layer, coplanar, coated, n):
    p = {'H1': round(_logu(20.0, 130.0, n()), 4), 'Er1': round(_u(4.00, 4.70, n()), 3)}
    w1 = round(_logu(5.0, 160.0, n()), 3)
    p['W1'] = w1
    p['W2'] = round(w1 - _pick(DELTAS, n()), 3)
    if kind == 'diff':
        p['S1'] = round(_logu(4.0, 40.0, n()), 3)
    if coplanar:
        p['D1'] = round(_logu(5.0, 80.0, n()), 3)
    p['T1'] = _pick([1.6, 2.4], n())
    if coated:
        p['C1'] = round(_u(0.8, 1.4, n()), 3)
        p['C2'] = round(_u(0.5, 0.9, n()), 3)
        if kind == 'diff':
            p['C3'] = round(_u(0.8, 1.4, n()), 3)
        p['CEr'] = round(_u(3.6, 4.0, n()), 3)
    return p


def gen_oz2(kind, layer, coplanar, coated, n):
    p = {'H1': round(_logu(4.0, 60.0, n()), 4), 'Er1': round(_u(3.90, 4.60, n()), 3)}
    if layer == 'inner':
        p['H2'] = round(_logu(4.0, 60.0, n()), 4)
        p['Er2'] = round(_u(3.90, 4.60, n()), 3)
    w1 = round(_logu(3.0, 40.0, n()), 3)
    p['W1'] = w1
    p['W2'] = round(w1 - _pick([1.0, 1.2], n()), 3)
    if kind == 'diff':
        p['S1'] = round(_logu(3.0, 40.0, n()), 3)
    if coplanar:
        p['D1'] = round(_logu(3.0, 40.0, n()), 3)
    p['T1'] = _pick([1.2, 2.4], n())
    if coated:
        p['C1'] = round(_u(1.0, 1.4, n()), 3)
        p['C2'] = round(_u(0.6, 1.0, n()), 3)
        if kind == 'diff':
            p['C3'] = round(_u(1.0, 1.4, n()), 3)
        p['CEr'] = round(_u(3.6, 4.0, n()), 3)
    return p


GENERATORS = {'fill': gen_fill, 'stackup': gen_stackup,
              'layer2': gen_layer2, 'oz2': gen_oz2}
NV = len(BASES)


def halton(i, b):
    f, r = 1.0, 0.0
    while i > 0:
        f /= b
        r += f * (i % b)
        i //= b
    return r


def design(regime, spec, count, offset):
    """Halton 低差异序列（确定性 → 断点续采时样本一致）。"""
    kind, layer, coplanar, coated = spec
    gen = GENERATORS[regime]
    out = []
    for k in range(count):
        rnd = [halton(offset + k + 1, BASES[j]) for j in range(NV)]
        it = iter(rnd)
        out.append(gen(kind, layer, coplanar, coated, lambda it=it: next(it)))
    return out


# --------------------------------------------------------------------------- #
def load_done(path):
    done = set()
    for r in C.load(path):
        if r.get('z'):
            done.add((r['mark'], r.get('key', json.dumps(r['params'], sort_keys=True))))
    return done


def append(path, rows):
    C.ensure_dirs()
    with open(path, 'a', encoding='utf-8', newline='\n') as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + '\n')


class Runner:
    def __init__(self, args):
        self.args = args
        self.api = JlcApi()
        self.n = 0
        self.bad = 0
        self.t0 = time.time()
        self.total = 0

    def sleep(self):
        time.sleep(random.uniform(self.args.min_delay, self.args.max_delay))

    def _call(self, fn):
        try:
            return fn()
        except JlcApiError as exc:
            print('  error: %s' % exc, file=sys.stderr)
            self.api.close()
            time.sleep(5)
            self.api = JlcApi()
            return None

    def forward(self, mark, spec, regime, count, offset, path, done):
        kind, layer, coplanar, coated = spec
        for p in design(regime, spec, count, offset):
            key = json.dumps(p, sort_keys=True)
            if (mark, key) in done:
                continue
            res = self._call(lambda: self.api.calculate(mark, dict(p, dCalculateMode=3)))
            if res is None:
                continue
            append(path, [{'mark': mark, 'kind': kind, 'layer': layer,
                           'coplanar': coplanar, 'coated': coated, 'regime': regime,
                           'key': key, 'params': p, 'z': res.impedance,
                           'er_eff': res.er_eff, 'status': res.status}])
            self.n += 1
            if res.status != 0:
                self.bad += 1
            if self.n % 20 == 0:
                el = time.time() - self.t0
                print('[%4d/%d] %-40s %-8s 已用 %.0fs  预计还要 %.0fs'
                      % (self.n, self.total, mark, regime, el,
                         el / self.n * (self.total - self.n)), flush=True)
            self.sleep()

    def reverse(self, mark, spec, regime, count, offset, targets=(40, 50, 60, 90, 100)):
        kind, layer, coplanar, coated = spec
        for i, p in enumerate(design(regime, spec, count, offset)):
            target = targets[i % len(targets)]
            if kind == 'diff' and 'S1' not in p:
                p['S1'] = 8.0
            res = self._call(lambda: self.api.solve(mark, 'W2',
                                                    dict(p, dCalculateMode=3),
                                                    target, 2.0, 150.0))
            if res is None:
                continue
            append(C.FILE_REVERSE, [{'mark': mark, 'kind': kind, 'layer': layer,
                                     'regime': regime, 'params': p, 'target': target,
                                     'solved': res.solved, 'z': res.impedance,
                                     'status': res.status}])
            self.n += 1
            if self.n % 10 == 0:
                print('[rev %3d] %-40s target=%-5s -> W1=%s'
                      % (self.n, mark, target, res.solved.get('W1')), flush=True)
            self.sleep()


def plan_text(quick=False):
    rows = [(m, r, c) for m, r, c in PLAN]
    hold_n = HOLDOUT_PER * len(HOLDOUT_MIX) if not quick else 0
    rev_n = len(REVERSE_MARKS) * 10 if not quick else 0
    total = sum(c for _, _, c in rows) + hold_n + rev_n
    lines = ['计划：']
    for m, r, c in rows:
        lines.append('  %-10s %-42s %3d' % (r, m, c))
    lines.append('  %-10s %-42s %3d' % ('holdout', '(12 个结构混合)', hold_n))
    lines.append('  %-10s %-42s %3d' % ('reverse', '(6 个结构对拍)', rev_n))
    lines.append('合计 %d 个请求，按 1.74 s/个约 %.0f 分钟'
                 % (total, total * 1.74 / 60))
    return '\n'.join(lines), total


def main(argv=None):
    ap = argparse.ArgumentParser(description='采样官网数据')
    ap.add_argument('--plan', action='store_true', help='只打印计划')
    ap.add_argument('--regime', default=None,
                    choices=['fill', 'stackup', 'layer2', 'oz2', 'test', 'reverse'])
    ap.add_argument('--scale', type=float, default=1.0, help='缩放每项样本数')
    ap.add_argument('--min-delay', type=float, default=0.9)
    ap.add_argument('--max-delay', type=float, default=1.6)
    args = ap.parse_args(argv)
    quick = args.regime == 'test' and args.scale < 1

    text, total = plan_text(quick)
    if args.plan or args.regime is None:
        print(text)
        if args.plan:
            return 0
        args.regime = 'all'

    C.ensure_dirs()
    runner = Runner(args)
    only = args.regime
    try:
        if only in ('all', 'fill', 'stackup', 'layer2', 'oz2'):
            done = load_done(OUT_EXTRA)
            for i, (mark, regime, count) in enumerate(PLAN):
                if only != 'all' and regime != only:
                    continue
                cnt = max(1, int(round(count * args.scale)))
                runner.forward(mark, SPEC[mark], regime, cnt,
                               10000 + i * 613, OUT_EXTRA, done)
        if only in ('all', 'test'):
            done = load_done(C.FILE_HOLDOUT)
            per = max(1, int(round(HOLDOUT_PER * args.scale)))
            for i, (mark, regimes) in enumerate(HOLDOUT_MIX.items()):
                for j, regime in enumerate(regimes):
                    base = per // len(regimes)
                    cnt = base if j < len(regimes) - 1 else per - base * (len(regimes) - 1)
                    if cnt <= 0:
                        continue
                    runner.forward(mark, SPEC[mark], regime, cnt,
                                   900000 + i * 7919 + j * 131, C.FILE_HOLDOUT, done)
        if only in ('all', 'reverse'):
            runner.total += len(REVERSE_MARKS) * 10
            for i, mark in enumerate(REVERSE_MARKS):
                spec = SPEC[mark]
                regime = 'stackup' if spec[1] == 'inner' else 'fill'
                runner.reverse(mark, spec, regime, 10, 700000 + i * 3571)
    finally:
        runner.api.close()
    el = time.time() - runner.t0
    print('新增 %d 组（失败 %d），耗时 %.0f 秒（%.1f 分钟）'
          % (runner.n, runner.bad, el, el / 60))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
