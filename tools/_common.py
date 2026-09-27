"""工具脚本的共享层：数据加载、拟合、度量、生成文件。

设计约束
--------
**工具脚本之间一律不互相 import**（那会让某个脚本变成隐式库，是典型的
高耦合）。所有共享代码只放在本模块里，方向永远是 ``tools/* → _common``、
``tools/* → impedance_calculator``。

目录约定::

    data/     采样数据（jsonl）与叠层缓存
    reports/  脚本生成的报告
"""

import json
import math
import os
import random
import sys
from collections.abc import Iterable, Sequence
from typing import Any, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
REPORTS = os.path.join(ROOT, 'reports')
TOOLS = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from impedance_calculator import analytic, calibration                        # noqa: E402
from impedance_calculator.calibration import compact_inputs, features         # noqa: E402

# ---- 数据文件（唯一真相）----
FILE_TRAIN = [os.path.join(DATA, 'calibration.jsonl'),
              os.path.join(DATA, 'calibration_extra.jsonl')]
FILE_HOLDOUT = os.path.join(DATA, 'holdout.jsonl')
FILE_REVERSE = os.path.join(DATA, 'reverse.jsonl')
FILE_ZEROCOPPER = os.path.join(DATA, 'zerocopper.jsonl')
FILE_STACKUPS = os.path.join(DATA, 'stackups_cache.json')

# ---- 生成物 ----
OUT_COEFS = os.path.join(ROOT, 'impedance_calculator', '_coefs.py')
OUT_KRRS = os.path.join(ROOT, 'impedance_calculator', '_krrs.py')
OUT_CALIB_REPORT = os.path.join(REPORTS, 'calibration_report.md')

#: 实际工作区（Ω）
PRACTICAL = (40.0, 110.0)
#: 介质比这还薄就是不可能的几何（真实叠层最薄 ~3 mil）
H_MIN_MIL = 3.0


def ensure_dirs() -> None:
    for d in (DATA, REPORTS):
        os.makedirs(d, exist_ok=True)


def default_template_dir() -> str:
    """KiCad 模板的输出目录。

    优先复用仓库旁的 ``PCB TEMPLATE/``（本地开发时两个目录在一起）；
    单独 clone 本模块时它不存在，就退回到包内的 ``kicad_templates/``。
    """
    sibling = os.path.join(os.path.dirname(ROOT), 'PCB TEMPLATE')
    if os.path.isdir(sibling):
        return sibling
    return os.path.join(ROOT, 'kicad_templates')


# --------------------------------------------------------------------------- #
#  数据
# --------------------------------------------------------------------------- #
def load(paths) -> list[dict[str, Any]]:
    """读一个或多个 jsonl；``paths`` 可以是字符串或列表。"""
    if isinstance(paths, str):
        paths = [paths]
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def physically_valid(r: dict[str, Any]) -> bool:
    """滤掉采样里不可能的几何（介质比 3 mil 还薄）。"""
    p = r['params']
    if p['H1'] < H_MIN_MIL:
        return False
    if p.get('H2') and p['H2'] < H_MIN_MIL:
        return False
    return True


def prepare(rows: Iterable[dict[str, Any]], verbose: bool = True) -> list[dict[str, Any]]:
    """算出每条的 ``f``（特征）与 ``ly = ln(Z_官网 / Z_base)``。"""
    out, dropped = [], 0
    for r in rows:
        if not r.get('z') or r.get('status') != 0:
            continue
        if not physically_valid(r):
            dropped += 1
            continue
        p = r['params']
        r['f'] = features(p)
        zb, _ = analytic.estimate(r['mark'], p, calibrated=False)
        if not zb or zb <= 0 or not (5 < r['z'] < 400):
            continue
        r['ly'] = math.log(r['z'] / zb)
        out.append(r)
    if dropped and verbose:
        print('  (剔除非物理几何样本 %d 条)' % dropped)
    return out


# --------------------------------------------------------------------------- #
#  线性回归（纯标准库）
# --------------------------------------------------------------------------- #
def solve_normal(X: Sequence[Sequence[float]], y: Sequence[float],
                 ridge: float = 1e-6) -> list[float] | None:
    """岭回归 ``(XᵗX + λI)β = Xᵗy``，高斯消元求解。"""
    n, p = len(X), len(X[0])
    A = [[0.0] * p for _ in range(p)]
    b = [0.0] * p
    for i in range(n):
        xi, yi = X[i], y[i]
        for r in range(p):
            xr = xi[r]
            if xr == 0.0:
                continue
            for c in range(r, p):
                A[r][c] += xr * xi[c]
            b[r] += xr * yi
    for r in range(p):
        for c in range(r):
            A[r][c] = A[c][r]
        A[r][r] += ridge * (1.0 + A[r][r])
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-14:
            return None
        A[col], A[piv] = A[piv], A[col]
        b[col], b[piv] = b[piv], b[col]
        pv = A[col][col]
        for r in range(col + 1, p):
            fac = A[r][col] / pv
            if fac:
                for c in range(col, p):
                    A[r][c] -= fac * A[col][c]
                b[r] -= fac * b[col]
    beta = [0.0] * p
    for r in range(p - 1, -1, -1):
        beta[r] = (b[r] - sum(A[r][c] * beta[c] for c in range(r + 1, p))) / A[r][r]
    return beta


def fit_beta(rows: Sequence[dict[str, Any]], names: Sequence[str]) -> list[float] | None:
    return solve_normal([[r['f'].get(n, 0.0) for n in names] for r in rows],
                        [r['ly'] for r in rows])


def predict(beta: Sequence[float], names: Sequence[str], feats: dict[str, float]) -> float:
    return sum(b * feats.get(n, 0.0) for b, n in zip(beta, names))


def folds(n: int, k: int = 5, seed: int = 0) -> list[list[int]]:
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    return [idx[i::k] for i in range(k)]


def cv_ln_error(rows: Sequence[dict[str, Any]], names: Sequence[str],
                k: int = 5, seed: int = 0) -> float:
    errs: list[float] = []
    for f in folds(len(rows), k, seed):
        test = set(f)
        tr = [r for i, r in enumerate(rows) if i not in test]
        beta = fit_beta(tr, names)
        if beta is None:
            return 1e9
        for i in f:
            errs.append(predict(beta, names, rows[i]['f']) - rows[i]['ly'])
    return math.sqrt(sum(e * e for e in errs) / len(errs))


def forward_select(rows: Sequence[dict[str, Any]], pool: Sequence[str],
                   max_terms: int = 10, k: int = 5) -> tuple[list[str], list[float] | None, float]:
    """前向选择：以 5 折 CV 误差为准逐个加特征。"""
    chosen = ['1']
    best = cv_ln_error(rows, chosen, k)
    while len(chosen) < max_terms:
        cand, cbest = None, best
        for name in pool:
            if name in chosen:
                continue
            e = cv_ln_error(rows, chosen + [name], k)
            if e < cbest - 1e-6:
                cbest, cand = e, name
        if cand is None:
            break
        chosen.append(cand)
        best = cbest
    return chosen, fit_beta(rows, chosen), best


# --------------------------------------------------------------------------- #
#  误差度量（端到端：反算线宽）
# --------------------------------------------------------------------------- #
def cal_z(mark: str, params: dict[str, float], names: Sequence[str],
          beta: Sequence[float], krr_store: dict | None = None) -> float:
    """校准后的阻抗。

    ``krr_store`` 给出时叠加核级残差修正，即**出厂模型的完整两级校准**
    （与 :func:`impedance_calculator.calibration.apply_correction` 一致）；
    不给就只算线性级，供拟合期内部评估用（此时 beta 是折内新拟合的）。
    """
    zb, _ = analytic.estimate(mark, params, calibrated=False)
    f = features(params)
    k = sum(b * f.get(n, 0.0) for b, n in zip(beta, names))
    if krr_store:
        k += calibration.krr_predict(krr_store, params)
    return zb * math.exp(max(-1.0, min(1.0, k)))


def invert_width(mark: str, base: dict[str, float], target: float, delta: float,
                 names: Sequence[str], beta: Sequence[float],
                 hi: float = 250.0, steps: int = 60,
                 krr_store: dict | None = None) -> float | None:
    """二分反算线宽 W1，使校准模型给出 target。"""
    lo = delta + 0.2

    def z_of(w: float) -> float:
        return cal_z(mark, dict(base, W1=w, W2=max(w - delta, 0.05)), names, beta,
                     krr_store)

    flo, fhi = z_of(lo) - target, z_of(hi) - target
    if flo * fhi > 0:
        return None
    for _ in range(steps):
        mid = (lo + hi) / 2.0
        if (z_of(mid) - target) * flo > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def width_errors(rows: Sequence[dict[str, Any]], names: Sequence[str],
                 beta: Sequence[float], z_range: tuple[float, float] | None = None,
                 steps: int = 60,
                 krr_store: dict | None = None) -> tuple[list[float], int]:
    """在留出数据上反算线宽，返回 ``(百分比误差列表, 未收敛数)``。

    传入 ``krr_store`` 时评估的是完整的两级模型。
    """
    out, skipped = [], 0
    for r in rows:
        p = r['params']
        if z_range and not (z_range[0] <= r['z'] <= z_range[1]):
            continue
        w = invert_width(r['mark'], p, r['z'], p['W1'] - p['W2'], names, beta,
                         steps=steps, krr_store=krr_store)
        if w:
            out.append(100.0 * (w - p['W1']) / p['W1'])
        else:
            skipped += 1
    return out, skipped


def width_cv(rows: Sequence[dict[str, Any]], names: Sequence[str],
             k: int = 5, seed: int = 0, steps: int = 60) -> list[float]:
    """训练集内部 5 折（对用了 CV 挑特征的模型偏乐观，仅供参考）。"""
    errs: list[float] = []
    for f in folds(len(rows), k, seed):
        test = set(f)
        tr = [r for i, r in enumerate(rows) if i not in test]
        beta = fit_beta(tr, names)
        if beta is None:
            continue
        e, _ = width_errors([rows[i] for i in f], names, beta, steps=steps)
        errs += e
    return errs


def rms(v: Sequence[float]) -> float:
    return math.sqrt(sum(x * x for x in v) / len(v)) if v else float('nan')


def median(v: Sequence[float]) -> float:
    s = sorted(v)
    n = len(s)
    if not n:
        return float('nan')
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def stats(v: Sequence[float]) -> tuple[float, float, int]:
    return rms(v), (max(abs(x) for x in v) if v else float('nan')), len(v)


def pearson(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    if n < 3:
        return float('nan')
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return float('nan')
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / math.sqrt(va * vb)


# --------------------------------------------------------------------------- #
#  生成文件
# --------------------------------------------------------------------------- #
def write_coefs(result: dict[str, dict[str, Any]],
                quality: dict[str, dict[str, Any]]) -> None:
    """写 ``impedance_calculator/_coefs.py``（线性级）。"""
    lines = [
        '"""离线模型的线性校准系数 —— **自动生成，请勿手改**。',
        '',
        '生成：``python tools/fit_calibration.py``',
        '公式：``Z = Z_base · exp(Σ βᵢ·φᵢ)``，基函数见',
        '``impedance_calculator/calibration.py`` 的 ``features()``。',
        '"""',
        '',
        '# flake8: noqa',
        'CALIBRATION = {',
    ]
    for mark, d in sorted(result.items()):
        q = quality.get(mark, {})
        lines.append('    %r: {' % mark)
        lines.append('        "names": %r,' % (d['names'],))
        lines.append('        "beta": %r,' % ([round(b, 10) for b in d['beta']],))
        for key in ('width_holdout', 'width_train_cv'):
            v = q.get(key)
            lines.append('        "%s": %s,' % (
                key, 'None' if v is None or v != v else '%.3f' % v))
        lines.append('        "samples": %d,' % q.get('samples', 0))
        lines.append('    },')
    lines.append('}')
    lines.append('')
    with open(OUT_COEFS, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines))


def write_krrs(stores: dict[str, dict[str, Any]],
               quality: dict[str, float] | None = None) -> None:
    """写 ``impedance_calculator/_krrs.py``（RBF 核岭回归支持集）。"""
    quality = quality or {}
    lines = [
        '"""离线模型的 RBF 核岭回归支持集 —— **自动生成，请勿手改**。',
        '',
        '生成：``python tools/fit_calibration.py``',
        '预测：``bias + Σ αᵢ·exp(-γ‖x - Xᵢ‖²)``，其中',
        '``x = impedance_calculator.calibration.compact_inputs(params)`` 再按 mu/sd 标准化。',
        '',
        '``width_holdout``：整条流水线（线性 + 核）在**独立留出集**上的反算',
        '线宽相对误差（%），即离线模式真实可信度。',
        '"""',
        '',
        '# flake8: noqa',
        'KRRS = {',
    ]
    for mark, s in sorted(stores.items()):
        wq = quality.get(mark)
        lines.append('    %r: {' % mark)
        lines.append('        "mu": %r,' % [round(v, 6) for v in s['mu']])
        lines.append('        "sd": %r,' % [round(v, 6) for v in s['sd']])
        lines.append('        "gamma": %.8g,' % s['gamma'])
        lines.append('        "bias": 0.0,')
        lines.append('        "width_holdout": %s,' % ('None' if wq is None else '%.3f' % wq))
        lines.append('        "X": [')
        for row in s['X']:
            lines.append('            [%s],' % ','.join('%.5g' % v for v in row))
        lines.append('        ],')
        lines.append('        "alpha": [%s],' % ','.join('%.6g' % a for a in s['alpha']))
        lines.append('    },')
    lines.append('}')
    lines.append('')
    with open(OUT_KRRS, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('\n'.join(lines))
