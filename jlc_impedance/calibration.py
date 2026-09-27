"""离线解析模型的**校准层**。

解析公式在「铜厚趋 0」时和嘉立创后台（SI9000）只差 0.3%~0.6%，
误差几乎全部来自铜厚 / 梯形线 / 多介质 / 耦合。于是用采样数据拟合一个修正因子：

    Z_calibrated = Z_base · exp( Σ β_i · φ_i(params) )

* ``φ_i`` 是几何无量纲量（对数比、厚径比、梯形比、间距比…）的基函数，见 :func:`features`；
* ``β_i`` 是用 750 组官网正算数据、前向选择 + 岭回归 + 5 折交叉验证拟合出来的，
  存放在 ``_coefs.py``（由 ``tools/fit_calibration.py`` 生成）。

没有校准数据时 ``CALIBRATION`` 为空，:func:`apply_correction` 原样返回基底值。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

try:                                                    # 生成的系数表
    from ._coefs import CALIBRATION
except ImportError:                                     # pragma: no cover
    CALIBRATION: Dict[str, Dict[str, list]] = {}

try:                                                    # 生成的核方法支持集
    from ._krrs import KRRS
except ImportError:                                     # pragma: no cover
    KRRS: Dict[str, dict] = {}


def compact_inputs(p: Dict[str, float]) -> List[float]:
    """核方法用的紧凑物理输入（与 :func:`features` 同一套无量纲量）。"""
    w = (float(p["W1"]) + float(p["W2"])) / 2.0
    h1 = float(p["H1"])
    v = [math.log(w / h1), math.log(float(p["T1"]) / h1),
         (float(p["W1"]) - float(p["W2"])) / h1, math.log(float(p["Er1"]))]
    h2 = p.get("H2")
    if h2:
        v += [math.log(float(h2) / h1), math.log(w / float(h2)), math.log(float(p["Er2"]))]
    else:
        v += [0.0, 0.0, 0.0]
    v.append(math.log(float(p["S1"]) / h1) if p.get("S1") else 0.0)
    v.append(math.log(float(p["D1"]) / h1) if p.get("D1") else 0.0)
    return v


def krr_predict(store: dict, p: Dict[str, float]) -> float:
    """用存下来的支持集算 RBF 核岭回归的预测值（残差修正项）。"""
    x = compact_inputs(p)
    mu, sd = store["mu"], store["sd"]
    xs = [(x[j] - mu[j]) / sd[j] for j in range(len(mu))]
    g = store["gamma"]
    s = store.get("bias", 0.0)
    for xt, a in zip(store["X"], store["alpha"]):
        s += a * math.exp(-g * sum((xs[k] - xt[k]) ** 2 for k in range(len(xs))))
    return s


def features(p: Dict[str, float]) -> Dict[str, float]:
    """基函数库（拟合脚本与推理时必须完全一致）。"""
    f: Dict[str, float] = {}
    w = (float(p["W1"]) + float(p["W2"])) / 2.0
    h1, er1, t1 = float(p["H1"]), float(p["Er1"]), float(p["T1"])
    x = math.log(w / h1)
    y = math.log(t1 / h1)
    f["1"] = 1.0
    f["x"] = x
    f["x2"] = x * x
    f["y"] = y
    f["y2"] = y * y
    f["xy"] = x * y
    f["tap"] = (float(p["W1"]) - float(p["W2"])) / h1
    f["tapx"] = f["tap"] * x
    f["tapy"] = f["tap"] * y
    f["e"] = math.log(er1)
    f["e2"] = f["e"] ** 2
    f["xe"] = x * f["e"]
    f["ye"] = y * f["e"]
    if p.get("H2"):
        h2, er2 = float(p["H2"]), float(p["Er2"])
        x1 = math.log(w / h2)
        ww = math.log(h2 / h1)
        f["x1"] = x1
        f["x1sq"] = x1 * x1
        f["w"] = ww
        f["w2"] = ww * ww
        f["xw"] = x * ww
        f["x1w"] = x1 * ww
        f["yw"] = y * ww
        f["er2"] = math.log(er2)
        f["x1er2"] = x1 * f["er2"]
    if p.get("S1"):
        s = math.log(float(p["S1"]) / h1)
        f["s"] = s
        f["ssq"] = s * s
        f["xs"] = x * s
        f["ys"] = y * s
        r = float(p["S1"]) / h1
        f["r"] = r
        f["eR"] = math.exp(-r)
        f["reR"] = r * math.exp(-r)
        if p.get("H2"):
            r2 = float(p["S1"]) / float(p["H2"])
            f["r2"] = r2
            f["eR2"] = math.exp(-r2)
            f["reR2"] = r2 * math.exp(-r2)
    if p.get("D1"):
        d = math.log(float(p["D1"]) / h1)
        f["d"] = d
        f["dd"] = d * d
        f["xd"] = x * d
        f["yd"] = y * d
    if p.get("C2"):
        c = math.log(float(p["C2"]) / h1)
        f["c"] = c
        f["cc"] = c * c
        f["xc"] = x * c
        f["cer"] = math.log(float(p["CEr"]))
        f["cerc"] = f["cer"] * c
    return f


def width_cv(impedance_type: str) -> Optional[float]:
    """该结构「反算线宽」的**独立留出集**相对误差（%）。

    优先用核方法那一级存下来的实测值（整条流水线 线性+核），
    没有再退回线性级的交叉验证值。
    """
    store = KRRS.get(impedance_type)
    if store:
        v = store.get("width_holdout")
        if v is not None:
            return float(v)
    cal = CALIBRATION.get(impedance_type)
    if not cal:
        return None
    v = cal.get("width_holdout", cal.get("width_cv"))
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return float(v)


def quality_note(impedance_type: str) -> str:
    """给离线结果加一句可信度说明。"""
    cv = width_cv(impedance_type)
    if cv is None:
        return "离线模型：无校准数据，误差可能很大，建议用在线模式"
    if cv <= 2.0:
        return "离线模型：反算线宽典型误差 ±%.1f%%" % cv
    if cv <= 5.0:
        return "离线模型：反算线宽典型误差 ±%.1f%%（建议用在线模式核对）" % cv
    return "离线模型：此结构可达 ±%.0f%%，强烈建议用在线模式" % cv


def apply_correction(impedance_type: str, params: Dict[str, float],
                     z_base: float) -> Tuple[float, float]:
    """返回 ``(修正后的 Z, 修正系数)``。

    两级：对数线性（前向选择出来的特征） + RBF 核岭回归修残差。
    """
    cal = CALIBRATION.get(impedance_type)
    if not cal or z_base <= 0:
        return z_base, 1.0
    f = features(params)
    k = 0.0
    for name, beta in zip(cal["names"], cal["beta"]):
        v = f.get(name)
        if v is not None:
            k += beta * v
    store = KRRS.get(impedance_type)
    if store:
        try:
            k += krr_predict(store, params)
        except (ValueError, KeyError):
            pass
    # 修正系数限制在合理范围内，避免外插发疯
    k = max(-1.0, min(1.0, k))
    factor = math.exp(k)
    return z_base * factor, factor
