"""离线模型的候选机器学习后端。

统一接口：都拟合 ``y = ln(Z_官网 / Z_base)``，都提供::

    predict(x)        -> 均值
    predict_var(x)    -> 方差（GP 才有，其余返回 None）

包含：

* :class:`KRR`      核岭回归（RBF 核，闭式解 ``α = (K+λI)⁻¹y``）
* :class:`GP`       高斯过程（同一个解，但给出**预测方差**，可以判断是否外推）
* :class:`LinearStack`  线性对数模型 + 核方法修残差（两级堆叠）
* :func:`hierarchical_shrink`  多任务：把各结构的系数向"全体平均"收缩

全部纯 Python（不依赖 numpy/sklearn）。
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
#  基础工具
# --------------------------------------------------------------------------- #
def standardize_fit(X: Sequence[Sequence[float]]):
    n, d = len(X), len(X[0])
    mu = [sum(r[j] for r in X) / n for j in range(d)]
    sd = [math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / max(n - 1, 1)) or 1.0 for j in range(d)]
    return mu, sd


def standardize_apply(X, mu, sd):
    return [[(r[j] - mu[j]) / sd[j] for j in range(len(mu))] for r in X]


def _chol(A: Sequence[Sequence[float]]) -> Optional[List[List[float]]]:
    """Cholesky 分解 A = L·Lᵗ（A 对称正定）。"""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = A[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    return None
                L[i][j] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    return L


def _chol_solve(L, b):
    n = len(L)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _logdet(L) -> float:
    return 2.0 * sum(math.log(max(L[i][i], 1e-300)) for i in range(len(L)))


def median_gamma(Xs: Sequence[Sequence[float]], sample: int = 60) -> float:
    """RBF 长度尺度的中位数启发式：γ = 1/(2·median(dist²))。"""
    n = len(Xs)
    step = max(1, n // sample)
    ds = []
    for i in range(0, n, step):
        for j in range(0, n, step):
            if i < j:
                ds.append(sum((Xs[i][k] - Xs[j][k]) ** 2 for k in range(len(Xs[0]))))
    ds.sort()
    med = ds[len(ds) // 2] if ds else 1.0
    return 1.0 / (2.0 * (med or 1.0))


# --------------------------------------------------------------------------- #
#  核岭回归
# --------------------------------------------------------------------------- #
class KRR:
    name = 'krr'

    def __init__(self, reg: float = 1e-2, gamma: Optional[float] = None):
        self.reg, self.gamma = reg, gamma
        self.Xs: List[List[float]] = []
        self.alpha: List[float] = []
        self.mu = self.sd = None

    def fit(self, X, y):
        self.mu, self.sd = standardize_fit(X)
        self.Xs = standardize_apply(X, self.mu, self.sd)
        if self.gamma is None:
            self.gamma = median_gamma(self.Xs)
        n = len(self.Xs)
        K = [[math.exp(-self.gamma * sum((self.Xs[i][k] - self.Xs[j][k]) ** 2
                                         for k in range(len(self.Xs[0]))))
              for j in range(n)] for i in range(n)]
        for i in range(n):
            K[i][i] += self.reg
        L = _chol(K)
        self.alpha = _chol_solve(L, list(y)) if L else [0.0] * n
        return self

    def predict(self, x):
        xs = [(x[j] - self.mu[j]) / self.sd[j] for j in range(len(self.mu))]
        s = 0.0
        for xt, a in zip(self.Xs, self.alpha):
            s += a * math.exp(-self.gamma * sum((xs[k] - xt[k]) ** 2 for k in range(len(xs))))
        return s

    def predict_var(self, x):
        return None


# --------------------------------------------------------------------------- #
#  高斯过程（RBF 核；用边际似然调噪声，给出预测方差）
# --------------------------------------------------------------------------- #
class GP:
    name = 'gp'

    def __init__(self, gamma: Optional[float] = None, noise: Optional[float] = None,
                 signal: float = 1.0):
        self.gamma, self.noise, self.signal = gamma, noise, signal
        self.mu = self.sd = None
        self.Xs: List[List[float]] = []
        self.L = None
        self.alpha: List[float] = []
        self.y: List[float] = []
        self.ymean = 0.0

    def _kmat(self, A, B=None, add_noise=0.0):
        B = A if B is None else B
        d = len(A[0])
        K = [[self.signal * math.exp(-self.gamma * sum((a[k] - b[k]) ** 2 for k in range(d)))
              for b in B] for a in A]
        if add_noise:
            for i in range(len(K)):
                K[i][i] += add_noise
        return K

    def _nll(self, noise):
        K = self._kmat(self.Xs, add_noise=noise)
        L = _chol(K)
        if L is None:
            return 1e18
        a = _chol_solve(L, self.y)
        n = len(self.y)
        return 0.5 * sum(yi * ai for yi, ai in zip(self.y, a)) + 0.5 * _logdet(L) + 0.5 * n * math.log(2 * math.pi)

    def fit(self, X, y, tune_noise=True):
        self.mu, self.sd = standardize_fit(X)
        self.Xs = standardize_apply(X, self.mu, self.sd)
        if self.gamma is None:
            self.gamma = median_gamma(self.Xs)
        self.ymean = sum(y) / len(y)
        self.y = [v - self.ymean for v in y]
        n = len(self.y)
        # 幅度必须按 y 的方差来定，否则方差尺度全错（曾因写死 signal=1 而失效）
        var_y = sum(v * v for v in self.y) / max(n - 1, 1)
        self.signal = max(var_y, 1e-12)
        if self.noise is None:
            self.noise = 1e-3 * self.signal
        if tune_noise:
            # 一维黄金分割搜索（按 signal 的比例）
            gr = (math.sqrt(5) - 1) / 2
            a, b = math.log(1e-6 * self.signal), math.log(0.5 * self.signal)
            c, d = b - gr * (b - a), a + gr * (b - a)
            for _ in range(20):
                if self._nll(math.exp(c)) < self._nll(math.exp(d)):
                    b, d = d, c
                    c = b - gr * (b - a)
                else:
                    a, c = c, d
                    d = a + gr * (b - a)
            self.noise = math.exp((a + b) / 2)
        K = self._kmat(self.Xs, add_noise=self.noise)
        self.L = _chol(K)
        self.alpha = _chol_solve(self.L, self.y) if self.L else [0.0] * len(self.y)
        return self

    def _kvec(self, xs, X):
        d = len(xs)
        return [self.signal * math.exp(-self.gamma * sum((xs[k] - xt[k]) ** 2 for k in range(d)))
                for xt in X]

    def predict(self, x):
        m, _ = self.predict_with_var(x)
        return m

    def predict_with_var(self, x):
        xs = [(x[j] - self.mu[j]) / self.sd[j] for j in range(len(self.mu))]
        k = self._kvec(xs, self.Xs)
        m = self.ymean + sum(ki * ai for ki, ai in zip(k, self.alpha))
        v = 0.0
        if self.L is not None:
            z = _chol_solve(self.L, k)
            v = self.signal - sum(zi * zi for zi in z)
        return m, max(v, 0.0)

    def predict_var(self, x):
        return self.predict_with_var(x)[1]


# --------------------------------------------------------------------------- #
#  堆叠：线性模型 + 核方法修残差
# --------------------------------------------------------------------------- #
class LinearStack:
    """``y = 线性(特征) + KRR(残差)``，两级堆叠。

    线性级用调用方给的 ``(names, beta)``（生产里前向选择出来的那组），
    第二级用紧凑物理输入拟合线性级的残差。
    """

    name = 'stack'

    def __init__(self, names, beta, residual_model, feature_fn):
        self.names, self.beta = names, beta
        self.rm = residual_model
        self.feature_fn = feature_fn

    def _linear(self, params):
        f = self.feature_fn(params)
        return sum(b * f.get(n, 0.0) for b, n in zip(self.beta, self.names))

    def predict(self, x):
        # 这里 x 是 (params, compact_inputs)
        params, ci = x
        return self._linear(params) + self.rm.predict(ci)

    def predict_var(self, x):
        return self.rm.predict_var(x[1])


# --------------------------------------------------------------------------- #
#  多任务：分层收缩
# --------------------------------------------------------------------------- #
class MLP:
    """单隐层 tanh 网络（Adam + L2 + 早停），纯标准库。

    存在的意义是**作为对照**：留出集实测它比线性/核方法差 3~12 倍
    （8 输入 × 12 隐层 ≈ 120 个参数，而训练折里只有 ~48 个样本）。
    见 ``tools/experiments/compare_backends.py``。
    """

    name = 'mlp'

    def __init__(self, hidden: int = 12, epochs: int = 600, lr: float = 0.02,
                 l2: float = 1e-4, seed: int = 0, val_frac: float = 0.2):
        self.hidden, self.epochs, self.lr = hidden, epochs, lr
        self.l2, self.seed, self.val_frac = l2, seed, val_frac
        self.mu = self.sd = None
        self._fn = None

    def fit(self, X, y):
        n, d = len(X), len(X[0])
        self.mu, self.sd = standardize_fit(X)
        Xs = standardize_apply(X, self.mu, self.sd)
        self._fn = _train_mlp(Xs, list(y), self.hidden, self.epochs, self.lr,
                              self.l2, self.seed, self.val_frac)
        return self

    def predict(self, x):
        xs = [(x[j] - self.mu[j]) / self.sd[j] for j in range(len(self.mu))]
        return self._fn(xs)

    def predict_var(self, x):
        return None


def _train_mlp(X, y, hidden, epochs, lr, l2, seed, val_frac):
    """返回 ``f(x) -> y`` 的闭包；全批量 Adam + 验证集早停。"""
    n, d = len(X), len(X[0])
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    nv = max(1, int(n * val_frac))
    val, tr = idx[:nv], idx[nv:]
    rnd = random.Random(seed + 1)
    W1 = [[rnd.gauss(0, 1.0 / math.sqrt(d)) for _ in range(d)] for _ in range(hidden)]
    b1 = [0.0] * hidden
    W2 = [rnd.gauss(0, 1.0 / math.sqrt(hidden)) for _ in range(hidden)]
    b2 = [0.0]
    mW1 = [[0.0] * d for _ in range(hidden)]
    vW1 = [[0.0] * d for _ in range(hidden)]
    mb1, vb1 = [0.0] * hidden, [0.0] * hidden
    mW2, vW2 = [0.0] * hidden, [0.0] * hidden
    mb2, vb2 = [0.0], [0.0]
    ab1, ab2 = 1 - 1e-8, 1 - 1e-8
    step, best_err, best, bad = 0, 1e18, None, 0

    def fwd(xi):
        h = [math.tanh(sum(W1[j][k] * xi[k] for k in range(d)) + b1[j])
             for j in range(hidden)]
        return h, sum(W2[j] * h[j] for j in range(hidden)) + b2[0]

    for ep in range(epochs):
        rnd.shuffle(tr)
        for s in range(0, len(tr), 32):
            batch = tr[s:s + 32]
            gW1 = [[0.0] * d for _ in range(hidden)]
            gb1 = [0.0] * hidden
            gW2 = [0.0] * hidden
            gb2 = 0.0
            for i in batch:
                h, o = fwd(X[i])
                delta = 2.0 * (o - y[i]) / len(batch)
                for j in range(hidden):
                    gj = delta * W2[j] * (1 - h[j] * h[j])
                    gW2[j] += delta * h[j]
                    gb1[j] += gj
                    for k in range(d):
                        gW1[j][k] += gj * X[i][k]
                gb2 += delta
            step += 1
            for j in range(hidden):
                for k in range(d):
                    g = gW1[j][k] + l2 * W1[j][k]
                    mW1[j][k] = ab1 * mW1[j][k] + (1 - ab1) * g
                    vW1[j][k] = ab2 * vW1[j][k] + (1 - ab2) * g * g
                    W1[j][k] -= lr * (mW1[j][k] / (1 - ab1 ** step)) / (
                        math.sqrt(vW1[j][k] / (1 - ab2 ** step)) + 1e-8)
                g = gb1[j] + l2 * b1[j]
                mb1[j] = ab1 * mb1[j] + (1 - ab1) * g
                vb1[j] = ab2 * vb1[j] + (1 - ab2) * g * g
                b1[j] -= lr * (mb1[j] / (1 - ab1 ** step)) / (
                    math.sqrt(vb1[j] / (1 - ab2 ** step)) + 1e-8)
                g = gW2[j] + l2 * W2[j]
                mW2[j] = ab1 * mW2[j] + (1 - ab1) * g
                vW2[j] = ab2 * vW2[j] + (1 - ab2) * g * g
                W2[j] -= lr * (mW2[j] / (1 - ab1 ** step)) / (
                    math.sqrt(vW2[j] / (1 - ab2 ** step)) + 1e-8)
            g = gb2 + l2 * b2[0]
            mb2[0] = ab1 * mb2[0] + (1 - ab1) * g
            vb2[0] = ab2 * vb2[0] + (1 - ab2) * g * g
            b2[0] -= lr * (mb2[0] / (1 - ab1 ** step)) / (
                math.sqrt(vb2[0] / (1 - ab2 ** step)) + 1e-8)
        if ep % 25 == 0 or ep == epochs - 1:
            err = sum((fwd(X[i])[1] - y[i]) ** 2 for i in val) / len(val)
            if err < best_err - 1e-9:
                best_err, bad = err, 0
                best = ([r[:] for r in W1], b1[:], W2[:], b2[0])
            else:
                bad += 1
                if bad >= 6:
                    break
    W1, b1, W2, b = best

    def predict(xi):
        h = [math.tanh(sum(W1[j][k] * xi[k] for k in range(d)) + b1[j])
             for j in range(hidden)]
        return sum(W2[j] * h[j] for j in range(hidden)) + b
    return predict


def hierarchical_shrink(betas: Dict[str, List[float]], counts: Dict[str, int],
                        n0: float = 60.0) -> Dict[str, List[float]]:
    """把每个结构的系数向"全体平均"收缩（James–Stein 式）。

    权重 ``w = n/(n+n0)``：样本多的结构基本不动，样本少的向公共形状靠。
    前提是所有结构用**同一组特征名**。
    """
    names = sorted({k for b in betas.values() for k in b})
    pooled = {}
    total = sum(counts.values()) or 1
    for nm in names:
        pooled[nm] = sum(b.get(nm, 0.0) * counts[s] for s, b in betas.items()) / total
    out = {}
    for s, b in betas.items():
        w = counts[s] / (counts[s] + n0)
        out[s] = {nm: w * b.get(nm, 0.0) + (1 - w) * pooled[nm] for nm in names}
    return out
