# PCB 阻抗计算器 · 离线引擎

> **给定叠层与目标阻抗，解出线宽**；也可以反过来：给定几何，算出阻抗。
> 默认走**离线引擎** —— 毫秒级、不联网、运行时零第三方依赖（纯标准库）。
> 另外附一个**在线引擎**，直接调用嘉立创的接口取真值，用于对拍与采样。

**验证情况：以嘉立创（JLCPCB）在线阻抗计算器为参考基准**，在它的真实叠层上
做了 222 组端到端校验：

| 口径 | 平均 | 中位 | ≤1% 占比 | ≤2% 占比 |
|---|---|---|---|---|
| **正算**｜同一线宽下 Z 的相对偏差 | **0.54 %** | 0.45 % | 89 % | 97 % |
| **反算**｜解出的线宽相对偏差 | **1.39 %** | 1.12 % | 45 % | 84 % |

覆盖 2 / 4 / 6 / 8 / 10 层 × 单端 50/75 Ω × 差分 90/100 Ω。另有 10 组落在
嘉立创工艺下限之外 —— **其引擎自己就返回 `status=6` 无解** —— 已从统计中剔除。

![离线引擎 vs 嘉立创在线引擎：2/4/6/8/10 层实测校验](reports/figures/validation.svg)

```
Python 3.9+  ·  运行时零依赖（纯标准库）  ·  52 个离线测试  ·  MIT
```

---

## 1. 快速开始

```bash
git clone <repo> && cd impedance_calculator
```

```bash
# 离线模型（不需要网络，毫秒级）
python -m jlc_impedance solve --stackup JLC04161H-7628 --layer L1 L2 --z0 50
#   L1  CoatedMicrostrip1B  线宽 W1 = 13.952 mil (0.3544 mm)  实际阻抗 = 50.000 Ω  [analytic]
#   L2  OffsetStripline1B1A 线宽 W1 = 11.067 mil (0.2811 mm)  实际阻抗 = 50.000 Ω  [analytic]

# 在线引擎（默认，与官网逐位一致）
python -m jlc_impedance solve --stackup JLC04161H-7628 --layer L2 --z0 50

# 生成 KiCad 设计规则片段
python -m jlc_impedance rules --stackup JLC04161H-3313 --layer L1 --z0-single 50 --z0-diff 100
```

```python
from jlc_impedance import JlcApi, ImpedanceCalculator, stackup

st = stackup.BUILTIN_STACKUPS["JLC04161H-7628"]
calc = ImpedanceCalculator(st)                       # 离线
print(calc.solve("L2", 50).as_dict())
# {'width_mil': 11.067, 'impedance_ohm': 50.0, 'engine': 'analytic', ...}

with JlcApi() as api:                                # 在线
    print(ImpedanceCalculator(st, api=api).solve("L2", 50).width)
```

---

## 2. 结果

### 2.1 离线模型精度（独立留出集，最严格口径）

留出集 180 组、**从不参与任何拟合与特征选择**。下表的数字由
`tools/fit_calibration.py` 自动算出来并写进 `_krrs.py`，离线模式会直接报给用户：

| 结构 | 反算线宽误差 | | 结构 | 反算线宽误差 |
|---|---|---|---|---|
| SurfaceMicrostrip1B | **0.29%** | | CoatedCoplanarWaveguideWithLowerGnd1B | 4.62% |
| CoatedMicrostrip1B | **1.62%** | | OffsetCoplanarWaveguide1B1A | 5.00% |
| SurfaceCoplanar… | 2.32% | | DiffCoatedCoplanar… | 5.08% |
| OffsetStripline1B1A | 3.02% | | DiffOffsetStripline1B1A | 5.20% |
| DiffEdgeCoupledSurfaceMicrostrip1B | 3.02% | | DiffSurfaceCoplanar… | 5.63% |
| DiffEdgeCoupledCoatedMicrostrip1B | 3.87% | | DiffOffsetCoplanar… | 11.64% |

**平均 4.28%（算术平均）、中位 4.62%**。

```
$ python -m jlc_impedance solve --stackup JLC04161H-7628 --layer L1 L2 --z0 50 --offline
L1  CoatedMicrostrip1B   线宽 W1 = 13.952 mil (0.3544 mm)  (离线模型：反算线宽典型误差 ±1.6%)
L2  OffsetStripline1B1A  线宽 W1 = 11.067 mil (0.2811 mm)  (离线模型：反算线宽典型误差 ±3.0%)
```

### 2.2 端到端校验（2 / 4 / 6 / 8 / 10 层，222 个有效用例）

> `tools/validate_layers.py` → [`reports/validation_report.html`](reports/validation_report.html)
> （自包含 HTML，内联图表）
> 校验图：`tools/make_validation_figure.py` → [`reports/figures/validation.svg`](reports/figures/validation.svg)

覆盖官网 9 个通用叠层 × 每一层铜 × 单端 50/75Ω、差分 90/100Ω：

| 层数 | 用例 | 平均线宽误差 | 中位 | 最大 |
|---|---|---|---|---|
| 2 层 | 8 | 0.82% | 0.78% | 1.64% |
| 4 层 | 32 | 1.17% | 1.15% | 2.17% |
| 6 层 | 48 | 1.62% | 1.40% | 7.12% |
| 8 层 | 64 | 1.66% | 1.09% | 5.48% |
| 10 层 | 70 | 1.16% | 1.09% | 4.34% |
| **合计** | **222** | **1.39%** | **1.12%** | 7.12% |

同一批案例的**正算**对比（把 JLC 解出的线宽原样喂给离线引擎，比阻抗）：

| 口径 | 平均 | 中位 | P90 | 最大 | ≤1% | ≤2% |
|---|---|---|---|---|---|---|
| 正算 Z 偏差 | **0.54%** | 0.45% | 1.05% | 2.22% | 89% | 97% |
| 反算线宽偏差 | **1.39%** | 1.12% | 2.61% | 7.12% | 45% | 84% |

![校验图](reports/figures/validation.svg)

**84% 的用例误差 ≤2%**。误差 >3% 的全部出现在 `W < 3 mil` 的**不可制造点**
（75Ω 落在薄介质内层，低于嘉立创工艺下限 ~3.5mil——官网自己也算不出来，
返回 `status=6`）。报告里这些行标了 ⚠ 并单列统计。

> ⚠️ **三个数字口径不同，别混用**：
> - **0.54% / 1.39%**（§2.2）＝官网**真实叠层**上的实测值 —— 最能代表实际使用；
> - **4.28%**（§2.1）＝均匀撒在**全参数空间**的留出集，含大量极端/边缘几何，
>   是「最严苛口径」，用来比较算法优劣。
>
> 对外说精度建议引用真实叠层的口径，并注明是相对嘉立创引擎（而非物理真值）。

### 2.3 引擎本身的可靠性（实测）

| 检验 | 结果 | 含义 |
|---|---|---|
| 同参数请求 3 次 × 12 结构 | 极差 **0.000000** | SI9000 完全确定，**噪音地板为零** |
| goal-seek 解出 W 后正算回代 | 偏差 **±0.01Ω** | 反算收敛到 0.02% |

噪音为零意味着：**看到的每一分误差都是模型误差**，精度天花板不在数据。

---

## 3. 架构：两个引擎，一个门面

```
                        ┌─── 在线引擎 api.py ────→ tools.jlc.com (SI9000)   ← 唯一真值
  calculator.py ────────┤     HTTP + 自写 WebSocket
  （目标阻抗 → 线宽）    │
                        └─── 离线引擎 ───→ 计算器（纯标准库、离线、毫秒级）

     离线引擎 = 物理基底 × exp( 线性修正 + 核方法残差修正 )
                │            │              │
     mom.py ────┘   _coefs.py┘    _krrs.py ─┘
     （T→0 精确场解） （10 项系数）  （RBF 支持集）
```

| 模块 | 职责 | 依赖 |
|---|---|---|
| `structures.py` | 12 个 SI9000 结构定义 | — |
| `stackup.py` | 叠层解析 → SI9000 参数（含官网的 H1/H2 规则） | — |
| `mom.py` | T→0 精确准静态场解（方法矩 + Galerkin） | — |
| `analytic.py` | 物理基底 + 正算 / 反算 | `mom`, `calibration` |
| `calibration.py` | 校准层（线性 + 核），对外只暴露一个函数 | `_coefs`, `_krrs` |
| `calculator.py` | 高层门面：叠层 + 阻抗 → 线宽 | 上面全部 |
| `api.py` | 在线引擎（HTTP + WebSocket） | — |
| `mlmodels.py` | 拟合后端：KRR / GP / MLP / 堆叠 / 分层收缩 | 仅 `tools/` 使用 |

**分层原则**：``jlc_impedance`` 是可直接交付的库（零外部依赖）；
``tools/`` 是生成与验证它的流程脚本；``tools/experiments/`` 是**被否掉的方案**
的对照实验，保留是为了让结论可复核。

---

## 4. 工程方法：把误差从 17% 压到 0.84%

每一步都有实测依据，不是拍脑袋。

### 4.1 先诊断误差来源（而不是直接上模型）

| 实验 | 结果 | 结论 |
|---|---|---|
| 铜厚 T→0 的表层微带线 | Hammerstad vs SI9000 差 **0.3~0.6%** | **教科书公式做基底没问题** |
| T: 0.01 → 1.6 mil | 阻抗掉 5.3% | SI9000 的铜厚修正只有教科书的 ~45% |
| 同平均线宽，梯形 vs 矩形 | 差 1.3% | 梯形必须单独建模 |
| 偏置带状线 T→0，H1/H2 互换 | 结果一致 | 结构本身对称，之前的不对称全来自铜厚 |

→ **误差全在铜厚 / 梯形 / 多介质 / 耦合上，不在基本公式上。**
所以策略是「物理基底 + 数据校准」，而不是「换个更强的模型」。

### 4.2 把物理算对：`mom.py`（T→0 精确场解）

带状线原来用「等效高度微带」硬凑，基底误差 13~17%。改为**方法矩**：

两块地之间的线电荷格林函数是**解析的**：

```
G(x,y) = 1/(4πε) · ln[ (cosh(πx/l) − cos(π(y+y')/l)) / (cosh(πx/l) − cos(π(y−y')/l)) ]
```

电荷在导体边缘 ~ `1/√(1−ξ²)`，所以用 `σ(ξ)=ΣcₙTₙ(ξ)/√(1−ξ²)` 做 Galerkin 展开，
配 Gauss-Chebyshev 求积（正好吸收那个权重）。差分对用**奇模**（电壁镜像反号）。

用官网 T=0.01 的实测值验证（`data/zerocopper.jsonl`）：

| | RMS | 最大 |
|---|---|---|
| 单端带状线 | **0.066%** | 0.126% |
| 差分带状线 | **0.105%** | 0.149% |

自洽性：`H1/H2` 互换给出**完全相同**的值（SI9000 自己差 0.09%）；
差分间距 →∞ 时 `Zdiff/(2Z0) = 1.0000`。**5 ms 一次，可放在二分反算里直接调。**

### 4.3 校准层：单靠线性不够，必须两级

```
Z = Z_base × exp( Σβᵢφᵢ )                    ← 线性级：10 项，前向选择 + 岭回归
                 × exp( Σαᵢexp(−γ‖x−Xᵢ‖²) )   ← 核级：RBF 核岭回归修残差
```

**为什么第二级必须有**（`tools/experiments/compare_backends.py`，
同一留出集、7 个后端）：

| 后端 | 留出集平均线宽误差 | 中位 | 相对线性 |
|---|---|---|---|
| linear（只有线性级） | 6.64% | 7.46% | — |
| krr（核岭回归） | 4.69% | 3.95% | −29% |
| gp（高斯过程） | 4.59% | 3.30% | −31% |
| **stack（线性 + 核修残差）** | **4.28%** | 4.24% | **−36%** |
| mlp（单隐层神经网络） | 21.7% | 16.91% | ❌ 差 3 倍 |
| shrink（多任务分层收缩） | 15.49% | 12.72% | ❌ 差 2.3 倍 |
| symbolic（符号回归） | 14.53% | 16.15% | ❌ 差 2.2 倍 |

（完整逐结构表见 [`reports/backend_comparison.md`](reports/backend_comparison.md)）

被否掉的方案与原因：

* **MLP 明显更差**：8 输入 × 12 隐层 ≈ 120 个参数，而训练折里只有 ~48 个样本。
* **GP 的预测方差不可用**：`|误差|` 与预测 sd 相关系数 **−0.09~−0.28**（负相关）；
  对真实外推（H1: 42→300mil）sd 仍为 **0**——因为特征全是**对数比值**，
  参数范围被压缩，真正的外推在特征空间里「并不远」。
* **多任务分层收缩失败**：直接对各结构的系数做收缩没有意义，
  它们的特征语义不同；要做得多任务得在共享隐空间里建模。
* **符号回归**能吐出可读公式（如 `0.0316·log((c−x)−(w−1.17)) − 0.052`），
  但精度不如核方法。保留在 `tools/experiments/symbolic_regression.py` 备查。

### 4.4 数据工程：采样分布比样本数量更重要

第一轮 750 组是纯空间填充（H1/H2 独立取），第二轮按用途补采 720 组：

| regime | 说明 | 数量 |
|---|---|---|
| `fill` | 空间填充（第一轮思路） | 750 |
| `stackup` | 内层沿**真实叠层流形**（`H1+H2` 固定、`H2` 占比小） | 320 |
| `layer2` | 外层大 H（20~130 mil，对应 1.6~3.2mm 板） | 240 |
| `oz2` | 厚铜 + 2oz 阻焊 | 160 |
| `test` | **独立留出集**（绝不参与拟合） | 180 |
| `reverse` | 官网 goal-seek 反算（对拍） | 60 |

量化依据（`tools/experiments/analyze_coverage.py`）：

```
补采前：内层结构只有 15% 的样本落在真实叠层区间（60 个样本里 ~9 个有用）
补采后：36%~43%，内层有效样本从 ~10 → ~100
```

同时剔除**非物理几何**（介质 < 3 mil，T/H 逼近 1），共 104 条。

### 4.5 评估口径：为什么必须有留出集

`forward_select` 在**全量数据**上挑特征，所以「训练集 5 折 CV」对
线性/堆叠模型**偏乐观**。实测差异：

| | 训练集 CV | 独立留出集 |
|---|---|---|
| linear | 6.29% | **6.64%** |
| krr | 6.78% | **4.69%** |

**结论完全反过来**。如果只看 CV，会得出「核方法不如线性」的错误结论，
从而砍掉真正有用的那一级。这是本项目最大的一个教训，也是留出集存在的唯一理由。

---

## 5. 精度与适用边界

| 项 | 数值 |
|---|---|
| 在线引擎 | **与官网逐位相同**（确定性，噪音 0） |
| 离线模型（留出集） | 平均 4.3%、中位 4.6% |
| 离线模型（真实叠层端到端） | 平均 1.39%、中位 1.12% |
| 离线模型（纯模型阻抗误差） | 平均 **0.54%**、最大 2.22% |

**适用边界**：

* 校准范围 H 3~130 mil、W 3~160 mil、T 0.1~2.4 mil、Er 3.9~4.7 —— 超出请用在线模式。
* 结构越「二维」（共面、内层差分），校准越吃力。最弱的
  `DiffOffsetCoplanarWaveguide1B1A`（10%）建议直接用在线引擎。
* 没有任何校准文件时模块仍能工作，退化成纯教科书公式。
* 阻抗控制最终以嘉立创工程确认的叠构为准。

---

## 6. 目录结构

```
impedance_calculator/
├── jlc_impedance/            库（零依赖，可直接 pip install）
│   ├── __main__.py           CLI
│   ├── api.py                在线引擎
│   ├── structures.py         12 个 SI9000 结构
│   ├── stackup.py            叠层解析 + 内置叠层
│   ├── mom.py                T→0 精确场解
│   ├── analytic.py           物理基底 + 正/反算
│   ├── calibration.py        校准层（运行时）
│   ├── calculator.py         高层门面
│   ├── mlmodels.py           拟合后端（KRR/GP/MLP/堆叠/收缩）
│   ├── _coefs.py             ┐ 生成物
│   └── _krrs.py              ┘（勿手改）
├── tools/                    生产流程
│   ├── _common.py            共享：数据/拟合/度量（唯一共享层）
│   ├── fetch_stackups.py     取官网叠层并缓存
│   ├── collect.py            采样（6 种 regime，可断点续采）
│   ├── check_engine.py       测官网重复性 / 正反算自洽 / 采 T=0.01 点
│   ├── fit_calibration.py    拟合 → _coefs.py + _krrs.py
│   ├── validate_layers.py    全层数校验 → HTML 报告
│   ├── make_validation_figure.py  校验图（手写 SVG，零依赖）
│   ├── make_width_table.py   叠层速查表
│   ├── make_kicad_templates.py / check_templates.py   生成并复验 KiCad 模板
│   ├── kicad_ref/            KiCad 参考模板（生成器不依赖外部目录）
│   └── experiments/          被否掉的方案的对照实验
├── data/                     采样数据（jsonl / json）
├── reports/                  生成的报告（HTML / Markdown / JSON）
│   └── figures/validation.svg   校验图（README 顶部引用）
├── examples/walkthrough.py   逐步讲解一次完整计算
└── tests/                    52 个离线测试（unittest，零依赖）
```

**耦合约束**：工具脚本**之间不互相 import**，共享代码只放在 `tools/_common.py`；
方向永远是 `tools/* → jlc_impedance`。这是本项目唯一一条硬性架构规矩。

---

## 7. 复现步骤

```bash
# 0) 离线测试（不需要网络）
python -m unittest discover -s tests -t .

# 1) 取官网叠层（6 个请求）
python tools/fetch_stackups.py --refresh

# 2) 采样（约 960 个请求 / 28 分钟，请求间隔 0.9~1.6s）
python tools/collect.py --plan     # 先看计划
python tools/collect.py

# 3) 测引擎可靠性 + 采 T=0.01 验证点（约 63 个请求）
python tools/check_engine.py

# 4) 拟合 → jlc_impedance/_coefs.py + _krrs.py + reports/calibration_report.md
python tools/fit_calibration.py

# 5) 全层数校验 → reports/validation_all.json + validation_report.html（232 个请求）
python tools/validate_layers.py

# 6) 校验图 → reports/figures/validation.svg（零依赖，手写 SVG）
python tools/make_validation_figure.py

# 7) 后端对照实验 → reports/backend_comparison.md
python tools/experiments/compare_backends.py --symbolic

# 8) 生成 2/4/6/8/10 层的 KiCad 叠层与设计规则模板（可选）
python tools/make_kicad_templates.py                 # 默认写到仓库旁的 PCB TEMPLATE/
python tools/make_kicad_templates.py --out DIR       # 或指定目录
python tools/check_templates.py                      # 复验生成结果

# 9) 看懂算法：逐步打印一次完整计算
python examples/walkthrough.py
```

所有中间产物都在 `data/`（原始数据）与 `reports/`（结论），**结论可复核**。

---

## 8. API 速查

```python
ImpedanceCalculator(stackup, api=None)      # api=None → 离线
  .solve(layer, z0, kind='single'|'diff', coplanar=False,
         spacing=None, gap=None, width=None)      # 阻抗 → 线宽/线距
  .forward(layer, width, kind='single', ...)      # 线宽 → 阻抗

Solution                                     # 结果对象
  .width .width_mm .spacing .gap .impedance .engine .note
  .as_dict()

JlcApi()                                     # 在线引擎（上下文管理器）
  .calculate(type, params)  .solve(type, param, params, target)
  .templates(layers, thickness, ...)  .config_copper()  .config_coverlay()
```

```bash
python -m jlc_impedance list                     # 列出叠层与 12 个结构
python -m jlc_impedance show --stackup <名>      # 叠层 → SI9000 参数
python -m jlc_impedance solve   --stackup <名> --layer L1 L2 --z0 50
python -m jlc_impedance forward --stackup <名> --layer L1 --width 8
python -m jlc_impedance rules   --stackup <名> --layer L1 --z0-single 50 --z0-diff 100
python -m jlc_impedance solve   --offline ...    # 强制离线
```

---

## 9. 设计取舍

| 决定 | 理由 | 代价 |
|---|---|---|
| **运行时零依赖（纯标准库）** | 任何环境都能跑，不需要 numpy/sklearn | 自己写线性代数（Cholesky / 岭回归 / 椭圆积分），约 200 行 |
| **区分在线与离线两个引擎** | 在线是唯一真值；离线是断网/批量的兜底 | 两套代码路径，需要一致性测试 |
| **离线 = 物理公式 + 数据校准，而非纯 ML** | 物理公式在 T→0 时误差 <0.6%，是最强的先验 | 需要手写基底并逐个验证 |
| **T→0 用真场解（MoM）而不是继续凑公式** | 凑出来的公式没有泛化保证；MoM 是物理，外推可信 | 5ms/次，比公式慢 100 倍（但仍在可接受范围） |
| **校准系数入库（`_coefs.py` / `_krrs.py`）** | 用户不需要采数据就能用 | 生成物 120KB，且与数据分布绑定 |
| **保留被否掉的实验** | 让「为什么不用 MLP」可复核，而不是一句主观判断 | 仓库里多几个脚本 |
| **留出集独立且永不参与拟合** | 防止自我欺骗（本项目已被它纠正过一次） | 少 180 组训练数据 |

---

## 10. 已知限制 / Roadmap

* **内层共面**（`DiffOffsetCoplanarWaveguide1B1A`）误差仍有 ~10%：
  四导体强二维问题，样本也最少。要根治得给它单独写多层介质的 MoM。
* **校准与数据分布绑定**：换板材（如 Rogers）需要重新采样。
* 目前只覆盖嘉立创，未接入其它板厂。
* `_krrs.py` 是明文大文件（120KB）；如需可压缩或改二进制，但会牺牲可读性。

---

## 11. 许可与合规

MIT，见 [LICENSE](LICENSE)。

本项目是**独立的阻抗计算实现**：物理基底（MoM 精确场解 / 共形映射）+ 用公开
接口返回的数据做校准。**不含嘉立创服务的任何代码或二进制**，运行时也不依赖
它 —— 离线引擎完全自洽。嘉立创的在线计算器在这里的角色是**参考基准与数据来源**。

脚本已内置节流（~0.8 req/s），请勿改成高频请求。
「嘉立创」「JLCPCB」「Polar SI9000」等字样仅用于说明事实与数据来源，
相关商标归各自所有者。计算结果仅供设计参考，最终以嘉立创工程确认为准。
