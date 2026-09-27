"""嘉立创阻抗计算器后台客户端（与网页发的请求完全一致）。

网页 https://tools.jlc.com/jlcTools/index.html#/impedanceCalculatenew
并不在浏览器里算阻抗，它只做两件事：

1. ``POST /api/jlcTools/impedance/calc`` 把参数交给后台（后台调 Polar SI9000 模型算）；
2. 通过 ``wss://tools.jlc.com/api/jlcTools/webSocket/<uuid>`` 收结果。

正算（几何 → 阻抗）::

    POST /impedance/calc
    {
      "accessId":            "<随机 uuid>",
      "impedance_calc_mark": "CoatedMicrostrip1B",
      "paramMd5":            "",
      "impedance_calc_arg":  {"H1":3.91,"Er1":4.1,"W1":8,"W2":7.5,"T1":1.6,...,
                              "dCalculateMode":3},
      "uuid":                "<同一个 uuid>"
    }

反算（阻抗 → 几何）只是把 ``impedance_calc_mark`` 换成 ``"W2_CoatedMicrostrip1B"``
（``W2`` / ``S1`` / ``D1`` 之一），并在参数里多加
``"Zo": 50, "MinW2": 1, "MaxW2": 60``，后台会把解写在
``impedance_calc_result.jBackCalc`` 里。

本模块只用标准库（urllib + socket），因此可以直接跑。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_BASE = "https://tools.jlc.com/api/jlcTools"
ORIGIN = "https://tools.jlc.com"


class JlcApiError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
#  极简 WebSocket 客户端（RFC6455，仅用到文本帧）
# --------------------------------------------------------------------------- #
class _WebSocket:
    def __init__(self, url: str, timeout: float = 30.0, origin: str = ORIGIN):
        if not url.startswith("wss://"):
            raise ValueError("只支持 wss://")
        hostport, _, path = url[len("wss://"):].partition("/")
        host = hostport.split(":")[0]
        port = int(hostport.split(":")[1]) if ":" in hostport else 443
        key = base64.b64encode(os.urandom(16)).decode()
        raw = socket.create_connection((host, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(timeout)
        req = (
            "GET /%s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: %s\r\n"
            "User-Agent: Mozilla/5.0\r\n\r\n" % (path, hostport, key, origin)
        )
        self.sock.sendall(req.encode())
        self._buf = b""
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise JlcApiError("WebSocket 握手被关闭")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise JlcApiError("WebSocket 握手失败: %r" % head[:160])

    # -- 内部 --
    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise JlcApiError("WebSocket 连接被关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_text(self, text: str) -> None:
        data = text.encode()
        head = bytearray([0x81])
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        mask = os.urandom(4)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout: Optional[float] = None) -> str:
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(ln)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:                       # text
                return payload.decode("utf-8", "replace")
            if opcode == 0x8:                       # close
                raise JlcApiError("服务端关闭了 WebSocket")
            if opcode == 0x9:                       # ping → pong
                self.sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# --------------------------------------------------------------------------- #
#  结果
# --------------------------------------------------------------------------- #
@dataclass
class CalcResult:
    ok: bool
    impedance: Optional[float] = None       # dImpedance，Ω
    er_eff: Optional[float] = None          # 有效介电常数
    delay: Optional[float] = None           # ps/inch
    inductance: Optional[float] = None      # nH/inch
    solved: Dict[str, float] = field(default_factory=dict)  # 反算得到的几何
    status: int = -1
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def width(self) -> Optional[float]:
        """反算出的线宽 W1（mil）。"""
        return self.solved.get("W1")

    @property
    def spacing(self) -> Optional[float]:
        """反算出的线距 S1 或线铜距离 D1（mil）。"""
        return self.solved.get("S1", self.solved.get("D1"))


# --------------------------------------------------------------------------- #
#  客户端
# --------------------------------------------------------------------------- #
class JlcApi:
    """嘉立创阻抗计算器后台。

    一次 :class:`JlcApi` 会话持有一条 WebSocket；所有 ``calculate_*`` 都是
    同步的（发请求 → 等这一条 accessId 的回包）。用完记得 :meth:`close`，
    或者直接用上下文管理器::

        with JlcApi() as api:
            print(api.calculate("CoatedMicrostrip1B", {...}).impedance)
    """

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.uuid = str(_uuid.uuid4())
        self._ws: Optional[_WebSocket] = None
        self._cache: Dict[str, Any] = {}

    # ---------- HTTP ---------- #
    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.base + "/" + path.lstrip("/"), data=data,
            headers={"Content-Type": "application/json", "Origin": ORIGIN,
                     "User-Agent": "Mozilla/5.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise JlcApiError("HTTP %s: %s" % (exc.code, exc.read()[:200])) from None

    # ---------- WebSocket ---------- #
    def _socket(self) -> _WebSocket:
        if self._ws is None:
            self._ws = _WebSocket(self.base.replace("https://", "wss://")
                                  + "/webSocket/" + self.uuid, self.timeout)
        return self._ws

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- 查询 ---------- #
    def pictures(self) -> List[Dict[str, Any]]:
        """官网 12 个 SI9000 结构（含参数默认值与说明）。"""
        if "pictures" not in self._cache:
            r = self.post("impedance/selectPageImpedancePicture",
                          {"pageNum": 1, "pageSize": 9999})
            self._cache["pictures"] = r.get("body", {}).get("list", [])
        return self._cache["pictures"]

    def templates(self, layers: int, thickness: float, outer_cu: float = 1.0,
                  inner_cu: float = 0.5, board_type: int = 1) -> List[Dict[str, Any]]:
        """按层数/板厚/铜厚查询嘉立创的叠层方案（如 JLC04161H-7628）。"""
        body = {"pageNum": 1, "pageSize": 99999, "plateLayerNumber": layers,
                "plateThickness": thickness, "cuprumThickness": outer_cu,
                "boardType": board_type}
        if layers != 2:
            body["innerCopperThickness"] = inner_cu
        r = self.post("impedance/selectPageImpedanceDefaultTemplate", body)
        return (r.get("body") or r.get("data") or {}).get("list", [])

    def config_copper(self) -> List[Dict[str, Any]]:
        """铜厚 / 蚀刻线宽增量（W1-W2）配置表。"""
        if "copper" not in self._cache:
            r = self.post("impedance/impedance-config/copper-trace-width/list",
                          {"pageNum": 1, "pageSize": 999})
            self._cache["copper"] = r.get("data", [])
        return self._cache["copper"]

    def config_coverlay(self) -> List[Dict[str, Any]]:
        """阻焊厚度 C1/C2/C3 配置表。"""
        if "coverlay" not in self._cache:
            r = self.post("impedance/impedance-config/coverlay/list",
                          {"pageNum": 1, "pageSize": 999})
            self._cache["coverlay"] = r.get("data", [])
        return self._cache["coverlay"]

    def limits(self) -> Dict[str, Dict[str, float]]:
        """各参数的取值范围。"""
        if "limits" not in self._cache:
            r = self.post("impedance/selectImpedanceDefaultValue",
                          {"pageNum": 1, "pageSize": 9999, "usePurpose": 1})
            self._cache["limits"] = {
                d["impedanceName"]: {"min": d["minValue"], "max": d["maxValue"]}
                for d in r.get("data", [])
            }
        return self._cache["limits"]

    # ---------- 计算 ---------- #
    def _request(self, mark: str, arg: Dict[str, Any]) -> CalcResult:
        access_id = str(_uuid.uuid4())
        ws = self._socket()
        self.post("impedance/calc", {
            "accessId": access_id,
            "impedance_calc_mark": mark,
            "paramMd5": "",
            "impedance_calc_arg": arg,
            "uuid": self.uuid,
        })
        deadline = time.time() + max(self.timeout, 60.0)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise JlcApiError("等待阻抗结果超时")
            msg = ws.recv(timeout=min(remaining, 20.0))
            try:
                payload = json.loads(msg)
            except ValueError:
                continue
            if payload.get("accessId") != access_id:
                continue        # 可能是别的条目的回包
            res = payload.get("impedance_calc_result") or {}
            return CalcResult(
                ok=(payload.get("impedance_calc_status") == 0),
                impedance=res.get("dImpedance"),
                er_eff=res.get("dErEff"),
                delay=res.get("dDelay"),
                inductance=res.get("dInductance"),
                solved={k: float(v) for k, v in (res.get("jBackCalc") or {}).items()},
                status=payload.get("impedance_calc_status", -1),
                error=payload.get("impedance_calc_error_msg") or "",
                raw=payload,
            )

    def calculate(self, impedance_type: str, params: Dict[str, float]) -> CalcResult:
        """正算：给几何，返回阻抗。``params`` 里必须带齐该模型的参数。"""
        arg = {k: float(v) for k, v in params.items()}
        arg["dCalculateMode"] = 3
        return self._request(impedance_type, arg)

    def solve(self, impedance_type: str, param: str, params: Dict[str, float],
              target: float, lower: float = 1.0, upper: float = 200.0) -> CalcResult:
        """反算：给定目标阻抗，求 ``param``（W2 / S1 / D1）。"""

        param = param.upper()
        arg = {k: float(v) for k, v in params.items()}
        arg.update({
            "Zo": float(target),
            "Min" + param: float(lower),
            "Max" + param: float(upper),
            "dCalculateMode": 3,
        })
        return self._request("%s_%s" % (param, impedance_type), arg)
