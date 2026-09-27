"""命令行入口：``python -m impedance_calculator ...``"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .api import JlcApi, JlcApiError
from .calculator import ImpedanceCalculator
from .stackup import BUILTIN_STACKUPS, Stackup
from .structures import STRUCTURES


def _load_stackup(args, api: Optional[JlcApi]) -> Stackup:
    key = args.stackup
    if key in BUILTIN_STACKUPS:
        return BUILTIN_STACKUPS[key]
    if api is None:
        raise SystemExit("内置叠层里没有 %r（可选：%s）。加 --online 可以按名字在线查叠层。"
                         % (key, ", ".join(BUILTIN_STACKUPS)))
    for tpl in api.templates(args.layers, args.thickness, args.outer_cu, args.inner_cu):
        name = tpl.get("receptionDisplayName") or tpl.get("appointName") or ""
        if key.upper() in name.upper():
            return Stackup.from_template(tpl)
    raise SystemExit("在线叠层里也没找到 %r" % key)


def _calc(args, api: Optional[JlcApi]) -> ImpedanceCalculator:
    st = _load_stackup(args, api)
    copper = coverlay = None
    if api is not None:
        copper = api.config_copper()
        coverlay = api.config_coverlay()
    return ImpedanceCalculator(st, api=api, copper_config=copper, coverlay_config=coverlay)


# --------------------------------------------------------------------------- #
def cmd_list(args, api):
    print("内置叠层：")
    for k, st in BUILTIN_STACKUPS.items():
        print("  %-16s %-34s %d 层  介质总厚 %.4f mm"
              % (k, st.name, st.copper_layers, st.total_dielectric_mm))
    if api is not None:
        print("\n官网叠层（%d 层 / %.2f mm / 外层 %goz / 内层 %goz）："
              % (args.layers, args.thickness, args.outer_cu, args.inner_cu))
        for tpl in api.templates(args.layers, args.thickness, args.outer_cu, args.inner_cu):
            print("  %-46s code=%s" % (tpl.get("receptionDisplayName"), tpl.get("laminatedConstructionCode")))
    print("\nSI9000 结构：")
    for s in STRUCTURES:
        print("  %-42s %-16s %s" % (s.impedance_type, s.name_cn, ",".join(s.params)))


def cmd_show(args, api):
    st = _load_stackup(args, api)
    copper = api.config_copper() if api is not None else None
    print(st.describe())
    print()
    print("各层 SI9000 参数（mil）：")
    for i in range(st.copper_layers):
        layer = "L%d" % (i + 1)
        g = st.si9000_geometry(layer)
        extra = ""
        if "H2" in g:
            extra = "  H2=%.4f Er2=%.2f" % (g["H2"], g["Er2"])
        print("  %-4s H1=%.4f Er1=%.2f%s  T1=%.2f  W1-W2=%.2f"
              % (layer, g["H1"], g["Er1"], extra, g["T1"], st.w2_delta(i, copper)))


def cmd_solve(args, api):
    calc = _calc(args, api)
    for layer in args.layer:
        for kind in args.kind:
            if kind == "diff" and args.spacing is None and args.width is not None:
                sol = calc.solve(layer, args.z0, kind=kind, coplanar=args.coplanar,
                                 gap=args.gap, width=args.width)
            else:
                sol = calc.solve(layer, args.z0, kind=kind, coplanar=args.coplanar,
                                 spacing=args.spacing, gap=args.gap)
            if args.json:
                print(json.dumps(sol.as_dict(), ensure_ascii=False))
            else:
                print(sol)


def cmd_forward(args, api):
    calc = _calc(args, api)
    for layer in args.layer:
        sol = calc.forward(layer, width=args.width, kind=args.kind,
                           coplanar=args.coplanar, spacing=args.spacing, gap=args.gap)
        if args.json:
            print(json.dumps(sol.as_dict(), ensure_ascii=False))
        else:
            print(sol)


def cmd_rules(args, api):
    """输出可以直接贴进 KiCad .kicad_dru 的 netclass 规则片段。"""
    calc = _calc(args, api)
    lines: List[str] = ["(version 1)", ""]
    for layer in args.layer:
        single = calc.solve(layer, args.z0_single)
        if single.width is None:
            continue
        lines.append("(rule \"JLC_%s_%gR_single\"" % (layer, args.z0_single))
        lines.append("  (constraint track_width (min %.4fmm) (opt %.4fmm) (max %.4fmm))"
                     % (single.width_mm * 0.9, single.width_mm, single.width_mm * 1.1))
        lines.append("  (condition \"A.NetClass == '%s_%gR'\")" % (layer, args.z0_single))
        lines.append(")")
        lines.append("")
        if args.z0_diff:
            diff = calc.solve(layer, args.z0_diff, kind="diff", spacing=args.spacing)
            if diff.width is not None:
                lines.append("(rule \"JLC_%s_%gR_diff\"" % (layer, args.z0_diff))
                lines.append("  (constraint track_width (min %.4fmm) (opt %.4fmm) (max %.4fmm))"
                             % (diff.width_mm * 0.9, diff.width_mm, diff.width_mm * 1.1))
                lines.append("  (constraint diff_pair_gap (min %.4fmm) (opt %.4fmm) (max %.4fmm))"
                             % (args.spacing * 0.0254 * 0.9, args.spacing * 0.0254,
                                args.spacing * 0.0254 * 1.1))
                lines.append("  (condition \"A.NetClass == '%s_%gR_diff'\")"
                             % (layer, args.z0_diff))
                lines.append(")")
                lines.append("")
    print("\n".join(lines))


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="impedance_calculator",
                                description="PCB 阻抗计算器：叠层 + 目标阻抗 → 线宽"
                                            "（默认离线引擎，加 --online 用嘉立创在线引擎）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--stackup", default="JLC04161H-7628", help="叠层名，如 JLC04161H-3313")
        sp.add_argument("--layers", type=int, default=4, help="在线查叠层时的层数")
        sp.add_argument("--thickness", type=float, default=1.6, help="在线查叠层时的板厚 mm")
        sp.add_argument("--outer-cu", type=float, default=1.0, help="外层铜厚 oz")
        sp.add_argument("--inner-cu", type=float, default=0.5, help="内层铜厚 oz")
        sp.add_argument("--online", action="store_true",
                        help="改用嘉立创在线引擎（默认走离线引擎）")
        return sp

    sp = sub.add_parser("list", help="列出叠层与 SI9000 结构")
    sp.add_argument("--layers", type=int, default=4)
    sp.add_argument("--thickness", type=float, default=1.6)
    sp.add_argument("--outer-cu", type=float, default=1.0)
    sp.add_argument("--inner-cu", type=float, default=0.5)
    sp.add_argument("--online", action="store_true")
    sp.set_defaults(func=cmd_list)

    sp = common(sub.add_parser("show", help="显示叠层解析结果与 SI9000 参数"))
    sp.set_defaults(func=cmd_show)

    sp = common(sub.add_parser("solve", help="反算：给阻抗求线宽/线距"))
    sp.add_argument("--layer", nargs="+", default=["L1"], help="层，如 L1 L2")
    sp.add_argument("--z0", type=float, required=True, help="目标阻抗 Ω")
    sp.add_argument("--kind", nargs="+", default=["single"], choices=["single", "diff"])
    sp.add_argument("--coplanar", action="store_true", help="共面结构")
    sp.add_argument("--spacing", type=float, default=None, help="差分线距 S1（mil）")
    sp.add_argument("--gap", type=float, default=None, help="共面线铜距 D1（mil）")
    sp.add_argument("--width", type=float, default=None,
                    help="差分时给定线宽，改为反算线距")
    sp.set_defaults(func=cmd_solve)

    sp = common(sub.add_parser("forward", help="正算：给线宽求阻抗"))
    sp.add_argument("--layer", nargs="+", default=["L1"])
    sp.add_argument("--width", type=float, required=True, help="线宽 W1（mil）")
    sp.add_argument("--kind", default="single", choices=["single", "diff"])
    sp.add_argument("--coplanar", action="store_true")
    sp.add_argument("--spacing", type=float, default=8.0)
    sp.add_argument("--gap", type=float, default=10.0)
    sp.set_defaults(func=cmd_forward)

    sp = common(sub.add_parser("rules", help="生成 KiCad .kicad_dru 规则片段"))
    sp.add_argument("--layer", nargs="+", default=["L1"])
    sp.add_argument("--z0-single", type=float, default=50.0)
    sp.add_argument("--z0-diff", type=float, default=None)
    sp.add_argument("--spacing", type=float, default=8.0)
    sp.set_defaults(func=cmd_rules)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    api = JlcApi() if getattr(args, "online", False) else None
    try:
        args.func(args, api)
    except JlcApiError as exc:
        print("在线引擎出错：%s\n（去掉 --online 即走离线引擎）" % exc, file=sys.stderr)
        return 2
    finally:
        if api is not None:
            api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
