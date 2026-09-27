"""符号回归：用遗传规划（GP）找出**闭式公式**，而不是黑盒。

动机：解析基底在铜厚/梯形/多介质上差 1~27%，那部分修正**也许本身就有简单形式**
（探针实验里铜厚修正系数稳定在 0.45 左右，很像一个常数）。符号回归试的就是这件事。

做法
----
* 表达式树：``+ - * /`` + 保护性 ``sq sqrt log inv``，深度 ≤ 3；
* 末端：8 个无量纲几何量 + 常数；
* **线性缩放**：每个个体先算出来，再用最小二乘解 ``y ≈ a·f(x) + b``
  （这一步让 GP 收敛快得多，不然纯 GP 基本搜不出来）；
* 锦标赛选择 + 子树交叉 + 变异 + 精英保留。

用法::

    python tools/symbolic_regression.py                 # 全部结构
    python tools/symbolic_regression.py OffsetStripline1B1A
"""

from __future__ import annotations

import math
import os
import random
import sys
from typing import Callable, List, Optional, Sequence, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C
from impedance_calculator import analytic

# --------------------------------------------------------------------------- #
VAR_NAMES = ['x', 'y', 'tap', 'e', 'w', 'x1', 'er2', 's', 'd', 'c']


def features_vec(p: dict) -> List[float]:
    """符号回归的输入向量（与 calibration.features 里的量一致）。"""
    w = (float(p['W1']) + float(p['W2'])) / 2.0
    h1, er1, t1 = float(p['H1']), float(p['Er1']), float(p['T1'])
    v = [math.log(w / h1), math.log(t1 / h1), (float(p['W1']) - float(p['W2'])) / h1,
         math.log(er1)]
    ww = math.log(float(p['H2']) / h1) if p.get('H2') else 0.0
    x1 = math.log(w / float(p['H2'])) if p.get('H2') else 0.0
    v += [ww, x1, math.log(float(p['Er2'])) if p.get('Er2') else 0.0]
    v.append(math.log(float(p['S1']) / h1) if p.get('S1') else 0.0)
    v.append(math.log(float(p['D1']) / h1) if p.get('D1') else 0.0)
    v.append(math.log(float(p['C2']) / h1) if p.get('C2') else 0.0)
    return v


def clamp(v, lo=1e-9, hi=1e9):
    if v != v or v in (float('inf'), float('-inf')):
        return 0.0
    return max(-hi, min(hi, v))


def ev(t, x: Sequence[float]) -> float:
    k = t[0]
    if k == 'c':
        return t[1]
    if k == 'v':
        return x[t[1]]
    if k == 'add':
        return clamp(ev(t[1], x) + ev(t[2], x))
    if k == 'sub':
        return clamp(ev(t[1], x) - ev(t[2], x))
    if k == 'mul':
        return clamp(ev(t[1], x) * ev(t[2], x))
    if k == 'div':
        d = ev(t[2], x)
        return clamp(ev(t[1], x) / (d if abs(d) > 1e-6 else 1e-6))
    a = ev(t[1], x)
    if k == 'sq':
        return clamp(a * a)
    if k == 'sqrt':
        return math.sqrt(abs(a))
    if k == 'log':
        return math.log(abs(a) + 1e-6)
    if k == 'inv':
        return clamp(1.0 / (a if abs(a) > 1e-6 else 1e-6))
    return 0.0


def to_str(t) -> str:
    k = t[0]
    if k == 'c':
        return '%.3g' % t[1]
    if k == 'v':
        return VAR_NAMES[t[1]]
    if k in ('add', 'sub', 'mul', 'div'):
        op = {'add': '+', 'sub': '-', 'mul': '*', 'div': '/'}[k]
        return '(%s %s %s)' % (to_str(t[1]), op, to_str(t[2]))
    fn = {'sq': 'sq', 'sqrt': 'sqrt', 'log': 'log', 'inv': 'inv'}[k]
    return '%s(%s)' % (fn, to_str(t[1]))


BIN = ['add', 'sub', 'mul', 'div']
UN = ['sq', 'sqrt', 'log', 'inv']


def rand_tree(depth, rng, nvar):
    if depth <= 0 or rng.random() < 0.25:
        if rng.random() < 0.3:
            return ('c', rng.uniform(-3, 3))
        return ('v', rng.randrange(nvar))
    if rng.random() < 0.72:
        return (rng.choice(BIN), rand_tree(depth - 1, rng, nvar), rand_tree(depth - 1, rng, nvar))
    return (rng.choice(UN), rand_tree(depth - 1, rng, nvar))


def size(t):
    return 1 + (0 if t[0] in ('c', 'v') else (size(t[1]) + (size(t[2]) if len(t) > 2 else 0)))


def rand_subtree(t, rng):
    """随机取一个子树（返回父节点引用 + 索引）。"""
    if t[0] in ('c', 'v'):
        return None, None
    n = 1 + (len(t) - 1)   # 子节点数
    i = rng.randrange(n)
    return t, i + 1


def get_sub(t, path):
    for i in path:
        t = t[i]
    return t


def set_sub(t, path, new):
    if not path:
        return new
    node = t
    for i in path[:-1]:
        node = node[i]
    node = list(node)
    node[path[-1]] = new
    return tuple(node)


def all_paths(t, prefix=()):
    yield prefix
    if t[0] in ('c', 'v'):
        return
    for i in range(1, len(t)):
        yield from all_paths(t[i], prefix + (i,))


def random_path(t, rng, max_size=1e9):
    paths = list(all_paths(t))
    rng.shuffle(paths)
    for p in paths:
        if size(get_sub(t, p)) <= max_size:
            return p
    return ()


# --------------------------------------------------------------------------- #
def linear_scale(z: Sequence[float], y: Sequence[float]):
    n = len(y)
    mz = sum(z) / n
    my = sum(y) / n
    den = sum((v - mz) ** 2 for v in z)
    if den < 1e-12:
        return 0.0, my, 1e18
    a = sum((z[i] - mz) * (y[i] - my) for i in range(n)) / den
    b = my - a * mz
    err = math.sqrt(sum((y[i] - (a * z[i] + b)) ** 2 for i in range(n)) / n)
    return a, b, err


def evaluate(tree, X, y):
    try:
        z = [ev(tree, x) for x in X]
    except Exception:                                       # noqa: BLE001
        return 0.0, 0.0, 1e18
    if any(v != v or v in (float('inf'), float('-inf')) for v in z):
        return 0.0, 0.0, 1e18
    return linear_scale(z, y)


def evolve(X, y, rng, pop_size=240, gens=30, depth=3, verbose=False):
    nvar = len(X[0])
    pop = [rand_tree(depth, rng, nvar) for _ in range(pop_size)]
    # 用单变量线性式播种，避免从纯噪声开始
    for i in range(min(nvar, len(pop) // 3)):
        pop[i] = ('v', i)
    scored = []
    for t in pop:
        a, b, e = evaluate(t, X, y)
        scored.append((e, t, a, b))
    scored.sort(key=lambda s: s[0])
    best = scored[0]
    for g in range(gens):
        new = scored[:4]                                     # 精英
        while len(new) < pop_size:
            # 锦标赛
            def tour():
                c = [rng.choice(scored) for _ in range(3)]
                return min(c, key=lambda s: s[0])[1]
            r = rng.random()
            if r < 0.55:                                     # 交叉
                t1, t2 = tour(), tour()
                p1 = random_path(t1, rng, max_size=12)
                p2 = random_path(t2, rng, max_size=12)
                child = set_sub(t1, p1, get_sub(t2, p2))
            elif r < 0.9:                                    # 变异
                t1 = tour()
                p = random_path(t1, rng, max_size=12)
                child = set_sub(t1, p, rand_tree(depth - 1, rng, nvar))
            else:
                child = rand_tree(depth, rng, nvar)
            if size(child) > 26:
                continue
            a, b, e = evaluate(child, X, y)
            new.append((e, child, a, b))
        scored = sorted(new, key=lambda s: s[0])[:pop_size]
        if scored[0][0] < best[0]:
            best = scored[0]
            if verbose:
                print('      gen %2d  err=%.5f  %s' % (g, best[0], to_str(best[1])))
    return best


def cv_of_form(tree, X, y, k=5, seed=0):
    """5 折 CV：只重解线性系数 (a,b)，评估"公式形式"的泛化。"""
    idx = list(range(len(y)))
    random.Random(seed).shuffle(idx)
    errs = []
    for f in [idx[i::k] for i in range(k)]:
        te = set(f)
        tr = [i for i in range(len(y)) if i not in te]
        a, b, _ = linear_scale([ev(tree, X[i]) for i in tr], [y[i] for i in tr])
        for i in f:
            errs.append(y[i] - (a * ev(tree, X[i]) + b))
    return errs, a, b


def main(argv=None):
    only = argv[0] if argv else None
    rows = C.prepare(C.load(C.FILE_TRAIN))
    by = {}
    for r in rows:
        by.setdefault(r['mark'], []).append(r)
    for mark, rs in sorted(by.items()):
        if only and mark != only:
            continue
        if len(rs) < 40:
            continue
        X = [features_vec(r['params']) for r in rs]
        y = [r['ly'] for r in rs]
        rng = random.Random(12345)
        e0, tree, a, b = evolve(X, y, rng, verbose=True)
        errs, aa, bb = cv_of_form(tree, X, y)
        cv = math.sqrt(sum(v * v for v in errs) / len(errs))
        print('=== %-42s n=%3d' % (mark, len(rs)))
        print('    公式: ln(Z/Z_base) = %.4f · %s %+.4f' % (a, to_str(tree), b))
        print('    训练误差=%.2f%%   5折CV=%.2f%%   （线性模型 CV 见 calibration_report.md）'
              % (100 * e0, 100 * cv))
        print()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
