"""生成一份「常用叠层 → 推荐线宽/线距」速查表（数据来自嘉立创在线引擎）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _common as C

from impedance_calculator import ImpedanceCalculator, JlcApi
from impedance_calculator.stackup import BUILTIN_STACKUPS

MM = 0.0254
STACKS = ["JLC04161H-3313", "JLC04161H-7628", "JLC04161H-2116", "JLC0216A"]


def main() -> int:
    lines = []
    with JlcApi() as api:
        copper, coverlay = api.config_copper(), api.config_coverlay()
        for name in STACKS:
            st = BUILTIN_STACKUPS[name]
            calc = ImpedanceCalculator(st, api=api, copper_config=copper, coverlay_config=coverlay)
            lines.append("### %s  (%s)" % (name, st.name))
            lines.append("")
            lines.append("| 层 | 单端 50Ω 线宽 | 差分 90Ω 线宽@S1=6 | 差分 90Ω 线宽@S1=8 | 差分 100Ω 线宽@S1=6 | 差分 100Ω 线宽@S1=8 |")
            lines.append("|---|---|---|---|---|---|")
            for i in range(st.copper_layers):
                layer = "L%d" % (i + 1)
                cells = []
                s = calc.solve(layer, 50)
                cells.append("%.2f mil / %.3f mm" % (s.width, s.width * MM))
                for zo in (90, 100):
                    for sp in (6, 8):
                        d = calc.solve(layer, zo, kind="diff", spacing=sp)
                        cells.append("-" if d.width is None else
                                     "%.2f mil / %.3f mm" % (d.width, d.width * MM))
                lines.append("| %s | %s |" % (layer, " | ".join(cells)))
                print(name, layer, cells)
            lines.append("")
    out = os.path.join(C.REPORTS, "stackup_widths.md")
    with open(out, "w", encoding="utf-8", newline='\n') as fh:
        fh.write("\n".join(lines) + "\n")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
