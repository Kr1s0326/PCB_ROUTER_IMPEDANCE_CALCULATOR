"""嘉立创（JLC）阻抗计算器的 Python 复刻。

两个引擎::

    JlcApi            在线引擎：发与网页完全相同的请求给嘉立创后台，
                      结果与 https://tools.jlc.com/jlcTools 上的数字逐位一致。
    analytic          离线引擎：教科书公式（Hammerstad / IPC-2141 / Ghione-Naldi），
                      没有网络时用来估算，误差约 ±5%~±15%。

典型用法::

    from jlc_impedance import JlcApi, ImpedanceCalculator, stackup

    st = stackup.BUILTIN_STACKUPS["JLC04161H-7628"]
    with JlcApi() as api:
        calc = ImpedanceCalculator(st, api=api)
        print(calc.solve("L1", 50).as_dict())          # 外层 50Ω 单端
        print(calc.solve("L2", 50).as_dict())          # 内层 50Ω 单端
        print(calc.solve("L1", 100, kind="diff", spacing=8).as_dict())   # 外层 100Ω 差分
"""

from .api import CalcResult, JlcApi, JlcApiError
from .calculator import ImpedanceCalculator, Solution
from .stackup import BUILTIN_STACKUPS, Stackup
from .structures import BY_TYPE, STRUCTURES, Structure, pick

__all__ = [
    "JlcApi", "JlcApiError", "CalcResult",
    "ImpedanceCalculator", "Solution",
    "Stackup", "BUILTIN_STACKUPS",
    "Structure", "STRUCTURES", "BY_TYPE", "pick",
]

__version__ = "1.0.0"
