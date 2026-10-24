#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
海康威视录像机 OSD 配置工具(简洁版)
====================================================================
只做三件事:改 OSD 名称、看通道预览、确认效果。不动位置,不动其他配置。

使用:
  1. 填录像机 IP / 端口 / 用户名 / 密码 / 通道号,点「读取当前 OSD」:
     读回当前通道名称并填入名称栏,右侧自动显示该通道画面。
  2. 在「OSD 名称」栏改好新名称,点「应用修改」:工具先读回设备原
     配置,只改名称后下发(位置等其余配置原样保留),成功后右侧画面
     自动重新显示,可直接核对新名称效果。
  3. 预览优先播放 MJPEG 视频流(httpPreview 接口),设备不支持时
     自动降级为连续抓图;可在预览区切换子码流/主码流或停止。

原理: 通过海康 ISAPI(HTTP Digest 认证)读写设备配置,接口自动探测
  (兼容 NVR 的 InputProxy 路径);抓图/视频接口同样自动探测。
  若读取失败,可用「诊断接口」查看各候选接口的探测结果。

运行:  python hikvision_osd_tool_lite.py
  - 需要 Python 3.8+,无需安装任何第三方库
  - 电脑需与录像机在同一局域网

版本: v1.1 (简洁版,功能基线为完整版 v1.3)
  - 密码框为可下拉选择的密码框,也可直接输入自填
  - 开源版本已移除内部常用密码预设(见密码框处注释,自行添加)
  v1.0 (简洁版,基于完整版 v1.2)
  - 去除: 台账导入/摄像头定位、名称与时间的位置设置、Y轴翻转/检测
  - 保留: 读/改 OSD 名称(只改名)、通道列表、诊断接口、嵌入式预览
====================================================================
"""

__version__ = "1.1"

import ssl
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime

# ---------------- 可调参数 ----------------
FALLBACK_CANVAS = (704, 576)  # 设备未上报归一化尺寸时的 fallback
TIMEOUT = 10              # HTTP 超时(秒)
NS_HIK = "http://www.hikvision.com/ver20/XMLSchema"
ET.register_namespace("", NS_HIK)


# ============================ XML 小工具 ============================
def _local(tag):
    """去掉命名空间,只取本地标签名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(parent, *names):
    """在直接子节点中按本地名(忽略大小写)查找第一个。"""
    want = {n.lower() for n in names}
    for ch in parent:
        if _local(ch.tag).lower() in want:
            return ch
    return None


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _int_or_none(el):
    try:
        return int(_text(el))
    except (ValueError, TypeError):
        return None


def _find_overlay(root, kind):
    """查找名称 / 时间叠加节点,兼容两种固件风格:
    A) <channelNameOverlay> / <DateTimeOverlay>
    B) <Overlay><overlayType>channelName|dateTime</overlayType>...
    kind: 'name' 或 'time'
    """
    # 风格 A
    if kind == "name":
        el = _find(root, "channelNameOverlay")
    else:
        el = _find(root, "DateTimeOverlay", "date-timeOverlay")
    if el is not None:
        return el
    # 风格 B
    for ov in root.iter():
        if _local(ov.tag).lower() != "overlay":
            continue
        otype = _text(_find(ov, "overlayType")).lower()
        if kind == "name" and "channelname" in otype:
            return ov
        if kind == "time" and "datetime" in otype:
            return ov
    return None


def _get_pos(ov):
    if ov is None:
        return None
    x = _int_or_none(_find(ov, "positionX"))
    y = _int_or_none(_find(ov, "positionY"))
    if x is None or y is None:
        return None
    return (x, y)


def _set_pos(ov, x, y):
    xe = _find(ov, "positionX")
    ye = _find(ov, "positionY")
    if xe is None or ye is None:
        raise RuntimeError("叠加配置中缺少 positionX / positionY 节点")
    xe.text = str(x)
    ye.text = str(y)


def _tobytes(root):
    # 按文档自身的命名空间序列化为默认命名空间,避免 ElementTree 生成
    # ns0: 前缀——部分固件的 XML 解析器会因此报 "Invalid XML Content"。
    # 同时统一为设备常见的双引号声明头。
    if root.tag.startswith("{"):
        ET.register_namespace("", root.tag[1:].split("}")[0])
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return xml.replace(b"<?xml version='1.0' encoding='utf-8'?>",
                       b'<?xml version="1.0" encoding="UTF-8"?>')


def _check_put_response(data):
    """检查 PUT 返回的 ResponseStatus,statusCode=1 表示成功。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return  # 非 XML 响应,HTTP 200 即视为成功
    if _local(root.tag) == "ResponseStatus":
        code = _text(_find(root, "statusCode"))
        if code and code != "1":
            s = _text(_find(root, "statusString"))
            sub = _text(_find(root, "subStatusCode"))
            raise RuntimeError(f"设备返回错误 statusCode={code} {s} {sub}".strip())


# ============================ ISAPI 客户端 ============================
class HikClient:
    """基于 urllib 的 ISAPI 客户端(HTTP Digest 认证)。"""

    def __init__(self, host, port=80, username="admin", password="",
                 use_https=False, timeout=TIMEOUT):
        scheme = "https" if use_https else "http"
        self.base = f"{scheme}://{host}:{port}"
        self.timeout = timeout
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.base, username, password)
        handlers = [urllib.request.HTTPDigestAuthHandler(pm)]
        if use_https:
            ctx = ssl._create_unverified_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def _req(self, path, method, data=None):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/xml; charset=UTF-8")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("认证失败:用户名或密码错误(或该用户无权限)")
            if e.code == 404:
                raise RuntimeError("接口不存在:请检查通道号,或该固件不支持此 ISAPI 接口")
            body = e.read().decode("utf-8", "ignore")[:300]
            msg = f"HTTP {e.code}: {body}"
            if data is not None:
                # 下发失败时把请求报文存盘,便于发给开发者分析
                safe = "".join(c if c.isalnum() else "_" for c in path)[:50]
                fname = f"PUT失败_{safe}_{datetime.now():%Y%m%d_%H%M%S}.xml"
                try:
                    with open(fname, "wb") as f:
                        f.write(data)
                    msg += f"\n已将本次下发内容保存为 {fname},可发给开发者分析。"
                except OSError:
                    pass
            raise RuntimeError(msg)
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接设备: {e.reason}")

    def get(self, path):
        _, data = self._req(path, "GET")
        return data

    def put(self, path, xml_bytes):
        _, data = self._req(path, "PUT", xml_bytes)
        _check_put_response(data)
        return data


# ============================ 接口自动发现 ============================
# 不同固件/机型支持的 OSD 接口路径不同,按顺序探测,命中即用:
CHANNEL_CANDIDATES = [
    "/ISAPI/System/Video/inputs/channels/{ch}",        # 摄像机 / 部分录像机
    "/ISAPI/ContentMgmt/InputProxy/channels/{ch}",     # NVR(常见)
]
OVERLAY_CANDIDATES = [
    "/ISAPI/System/Video/inputs/channels/{ch}/overlays",
    "/ISAPI/ContentMgmt/InputProxy/channels/{ch}/video/overlays",
]


def _parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _looks_like_channel(root):
    return (root is not None
            and _local(root.tag) in ("VideoInputChannel", "InputProxyChannel")
            and _find(root, "name") is not None)


def _looks_like_overlay(root):
    return (root is not None
            and (_find_overlay(root, "name") is not None
                 or _find_overlay(root, "time") is not None))


def _try_get(client, path):
    try:
        return client.get(path), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def discover_osd_paths(client, ch):
    """探测该通道可用的接口,返回 (ch_path, ov_path, attempts)。
    attempts 为 [(url, 结果描述)] 列表,供诊断展示。"""
    attempts = []
    ch_path, ov_path = None, None
    for tmpl in CHANNEL_CANDIDATES:
        p = tmpl.format(ch=ch)
        data, err = _try_get(client, p)
        if _looks_like_channel(_parse_xml(data) if data else None):
            ch_path = p
            attempts.append((p, "OK"))
            break
        attempts.append((p, err or "返回内容不是通道配置"))
    for tmpl in OVERLAY_CANDIDATES:
        p = tmpl.format(ch=ch)
        data, err = _try_get(client, p)
        if _looks_like_overlay(_parse_xml(data) if data else None):
            ov_path = p
            attempts.append((p, "OK"))
            break
        attempts.append((p, err or "返回内容不是叠加配置"))
    return ch_path, ov_path, attempts


def _discovery_error(ch, attempts):
    detail = "\n".join(f"  {url}\n    → {res}" for url, res in attempts)
    return RuntimeError(
        f"通道 {ch} 未找到可用的 OSD 接口,各候选接口探测结果:\n{detail}")


# ============================ OSD 读写逻辑 ============================
def read_osd(client, ch):
    """读取通道名称与 OSD 叠加配置,返回字典(含 attempts 诊断信息)。"""
    info = {
        "name": "", "overlay_name": "",
        "name_xy": None, "time_xy": None,
        "name_enabled": "?", "time_enabled": "?",
        "canvas": FALLBACK_CANVAS, "canvas_src": "默认",
        "ch_path": None, "ov_path": None, "attempts": [],
    }
    ch_path, ov_path, attempts = discover_osd_paths(client, ch)
    info["ch_path"], info["ov_path"], info["attempts"] = ch_path, ov_path, attempts
    if ch_path is None and ov_path is None:
        raise _discovery_error(ch, attempts)

    # 1) 通道名称
    if ch_path:
        root = ET.fromstring(client.get(ch_path))
        info["name"] = _text(_find(root, "name"))

    # 2) OSD 叠加配置
    if ov_path:
        root = ET.fromstring(client.get(ov_path))
        nss = _find(root, "normalizedScreenSize")
        if nss is not None:
            w = _int_or_none(_find(nss, "normalizedScreenWidth"))
            h = _int_or_none(_find(nss, "normalizedScreenHeight"))
            if w and h:
                info["canvas"] = (w, h)
                info["canvas_src"] = "设备上报"

        cno = _find_overlay(root, "name")
        if cno is not None:
            info["name_xy"] = _get_pos(cno)
            info["overlay_name"] = _text(_find(cno, "name"))
            info["name_enabled"] = _text(_find(cno, "enabled")) or "?"
        dto = _find_overlay(root, "time")
        if dto is not None:
            info["time_xy"] = _get_pos(dto)
            info["time_enabled"] = _text(_find(dto, "enabled")) or "?"
    return info


def apply_osd(client, ch, new_name, x, y, tx, ty, change_pos=True):
    """读回原配置 → 改 OSD 名称(必改)与名称/时间位置(可选) → 下发。
    (x, y) 为名称位置,(tx, ty) 为时间位置,两者相互独立。
    change_pos=False 时只改名称,位置保持不动。"""
    summary = []

    ch_path, ov_path, attempts = discover_osd_paths(client, ch)
    if ch_path is None or ov_path is None:
        raise _discovery_error(ch, attempts)
    summary.append(f"使用接口: {ch_path} / {ov_path}")

    # ---- 1) 通道名称(读回 → 只改 name → 下发) ----
    root = ET.fromstring(client.get(ch_path))
    name_el = _find(root, "name")
    if name_el is None:
        raise RuntimeError("设备返回的通道配置中没有 name 节点")
    old_name = _text(name_el)
    name_el.text = new_name
    client.put(ch_path, _tobytes(root))
    summary.append(f"通道名称: {old_name!r} → {new_name!r}")

    # ---- 2) OSD 叠加(读回 → 只改位置/叠加名称 → 下发) ----
    root = ET.fromstring(client.get(ov_path))

    cno = _find_overlay(root, "name")
    dto = _find_overlay(root, "time")

    if dto is not None:
        if change_pos:
            old = _get_pos(dto)
            _set_pos(dto, tx, ty)
            summary.append(f"时间叠加位置: {old} → {(tx, ty)}")
        else:
            summary.append("时间叠加位置: 保持不变")
    if cno is not None:
        if change_pos:
            old = _get_pos(cno)
            _set_pos(cno, x, y)
            summary.append(f"名称叠加位置: {old} → {(x, y)}")
        else:
            summary.append("名称叠加位置: 保持不变")
        nm = _find(cno, "name")  # 部分固件叠加内自带 name,存在才同步
        if nm is not None:
            nm.text = new_name
    if cno is None and dto is None:
        raise RuntimeError("未在设备返回的叠加配置中找到名称 / 时间叠加节点")

    client.put(ov_path, _tobytes(root))
    summary.append("其余配置保持原样,已下发。")
    return summary


def diagnose(client, ch):
    """探测设备能力与各候选接口,返回多行文本供展示/回传分析。"""
    lines = [f"工具版本: v{__version__}"]
    # 1) 设备信息
    data, err = _try_get(client, "/ISAPI/System/deviceInfo")
    root = _parse_xml(data) if data else None
    if root is not None and _local(root.tag) == "DeviceInfo":
        kv = {k: _text(_find(root, k)) for k in
              ("deviceName", "model", "firmwareVersion", "deviceType")}
        lines.append("设备: " + ", ".join(f"{k}={v}" for k, v in kv.items() if v))
    else:
        lines.append(f"设备信息: 获取失败({err or '解析失败'})")
    # 2) 通道列表(两种体系各试一次)
    for list_path, tag in (("/ISAPI/System/Video/inputs/channels", "VideoInputChannel"),
                           ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel")):
        data, err = _try_get(client, list_path)
        root = _parse_xml(data) if data else None
        ids = []
        if root is not None:
            for el in root.iter():
                if _local(el.tag) == tag:
                    i = _text(_find(el, "id"))
                    if i:
                        ids.append(i)
        lines.append(f"通道列表 {list_path}: "
                     + (", ".join(ids) if ids else f"无({err or '解析失败'})"))
    # 3) 候选接口逐个探测
    ch_path, ov_path, attempts = discover_osd_paths(client, ch)
    lines.append(f"通道 {ch} 候选接口探测:")
    for url, res in attempts:
        lines.append(f"  {url}")
        lines.append(f"    → {res}")
    lines.append(f"结论: 通道名称接口={'可用' if ch_path else '不可用'}, "
                 f"OSD 叠加接口={'可用' if ov_path else '不可用'}")
    return lines


# ============================ 台账查询 ============================


def list_nvr_channels(client):
    """列出录像机通道[(id, 名称, 摄像机IP)],供核对通道号。"""
    out = []
    for list_path, tag in (
            ("/ISAPI/ContentMgmt/InputProxy/channels", "InputProxyChannel"),
            ("/ISAPI/System/Video/inputs/channels", "VideoInputChannel")):
        data, _ = _try_get(client, list_path)
        root = _parse_xml(data) if data else None
        if root is None:
            continue
        for el in root.iter():
            if _local(el.tag) != tag:
                continue
            cid = _text(_find(el, "id"))
            nm = _text(_find(el, "name"))
            ip = ""
            desc = _find(el, "sourceInputPortDescriptor")
            if desc is not None:
                ip = _text(_find(desc, "ipAddress"))
            if cid:
                out.append((cid, nm, ip))
        if out:
            break
    return out



# ============================ 通道抓图与 JPEG 解码 ============================
# 抓图接口返回 JPEG,而 tkinter 只能显示 PNG/GIF/PPM,标准库也没有 JPEG
# 解码器。方案:Windows 上优先用系统自带的 GDI+ 把 JPEG 转成 PNG(快);
# 其他情况用下面这个纯 Python 基线 JPEG 解码器兜底(704x576 约 1 秒)。
import math

_JPEG_ZIGZAG = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)

# IDCT 系数矩阵 _IDCT_M[u][x] = 0.5*C(u)*cos((2x+1)*u*pi/16), C(0)=1/sqrt(2)
_IDCT_M = tuple(
    tuple((0.353553390593 if u == 0 else 0.5)
          * math.cos((2 * x + 1) * u * math.pi / 16.0)
          for x in range(8))
    for u in range(8))


class _HuffTable:
    __slots__ = ("lut", "slow")

    def __init__(self, counts, symbols):
        self.slow = {}
        self.lut = {}
        code = 0
        idx = 0
        by_len = []
        for length in range(1, 17):
            lst = []
            for _ in range(counts[length - 1]):
                sym = symbols[idx]
                idx += 1
                self.slow[(length, code)] = sym
                lst.append((code, sym))
                code += 1
            by_len.append(lst)
            code <<= 1
        for length in range(1, 10):
            shift = 9 - length
            for code, sym in by_len[length - 1]:
                base = code << shift
                for suffix in range(1 << shift):
                    self.lut[base | suffix] = (sym, length)


class _BitReader:
    __slots__ = ("data", "pos", "acc", "nbits")

    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.acc = 0
        self.nbits = 0

    def _fill(self, need):
        while self.nbits < need:
            if self.pos < len(self.data):
                self.acc = (self.acc << 8) | self.data[self.pos]
                self.pos += 1
            else:
                self.acc <<= 8
            self.nbits += 8

    def peek(self, n):
        self._fill(n)
        return (self.acc >> (self.nbits - n)) & ((1 << n) - 1)

    def take(self, n):
        v = self.peek(n)
        self.nbits -= n
        self.acc &= (1 << self.nbits) - 1
        return v


def _huff_decode(reader, table):
    ent = table.lut.get(reader.peek(9))
    if ent is not None:
        reader.take(ent[1])
        return ent[0]
    code = 0
    for length in range(1, 17):
        code = (code << 1) | reader.take(1)
        sym = table.slow.get((length, code))
        if sym is not None:
            return sym
    raise ValueError("JPEG 数据损坏:无效的 Huffman 码")


def _receive_extend(reader, n):
    if n == 0:
        return 0
    v = reader.take(n)
    if v < (1 << (n - 1)):
        v += 1 - (1 << n)
    return v


def _decode_block(reader, dc_tab, ac_tab, quant, dc_pred):
    coef = [0] * 64
    diff = _receive_extend(reader, _huff_decode(reader, dc_tab))
    dc_pred += diff
    coef[0] = dc_pred * quant[0]
    k = 1
    while k < 64:
        rs = _huff_decode(reader, ac_tab)
        r, s = rs >> 4, rs & 0x0F
        if s == 0:
            if r == 15:
                k += 16
                continue
            break
        k += r
        if k > 63:
            break
        coef[_JPEG_ZIGZAG[k]] = _receive_extend(reader, s) * quant[k]
        k += 1
    return coef, dc_pred


def _idct_block(coef):
    """8x8 IDCT + 电平偏移,返回 64 个 0..255 像素(行优先)。"""
    M = _IDCT_M
    tmp = [0.0] * 64
    for r in range(8):
        b = r * 8
        a0, a1, a2, a3, a4, a5, a6, a7 = coef[b:b + 8]
        tmp[b] = a0 * M[0][0] + a1 * M[1][0] + a2 * M[2][0] + a3 * M[3][0] \
            + a4 * M[4][0] + a5 * M[5][0] + a6 * M[6][0] + a7 * M[7][0]
        tmp[b + 1] = a0 * M[0][1] + a1 * M[1][1] + a2 * M[2][1] + a3 * M[3][1] \
            + a4 * M[4][1] + a5 * M[5][1] + a6 * M[6][1] + a7 * M[7][1]
        tmp[b + 2] = a0 * M[0][2] + a1 * M[1][2] + a2 * M[2][2] + a3 * M[3][2] \
            + a4 * M[4][2] + a5 * M[5][2] + a6 * M[6][2] + a7 * M[7][2]
        tmp[b + 3] = a0 * M[0][3] + a1 * M[1][3] + a2 * M[2][3] + a3 * M[3][3] \
            + a4 * M[4][3] + a5 * M[5][3] + a6 * M[6][3] + a7 * M[7][3]
        tmp[b + 4] = a0 * M[0][4] + a1 * M[1][4] + a2 * M[2][4] + a3 * M[3][4] \
            + a4 * M[4][4] + a5 * M[5][4] + a6 * M[6][4] + a7 * M[7][4]
        tmp[b + 5] = a0 * M[0][5] + a1 * M[1][5] + a2 * M[2][5] + a3 * M[3][5] \
            + a4 * M[4][5] + a5 * M[5][5] + a6 * M[6][5] + a7 * M[7][5]
        tmp[b + 6] = a0 * M[0][6] + a1 * M[1][6] + a2 * M[2][6] + a3 * M[3][6] \
            + a4 * M[4][6] + a5 * M[5][6] + a6 * M[6][6] + a7 * M[7][6]
        tmp[b + 7] = a0 * M[0][7] + a1 * M[1][7] + a2 * M[2][7] + a3 * M[3][7] \
            + a4 * M[4][7] + a5 * M[5][7] + a6 * M[6][7] + a7 * M[7][7]
    out = bytearray(64)
    for c in range(8):
        t0 = tmp[c]
        t1 = tmp[8 + c]
        t2 = tmp[16 + c]
        t3 = tmp[24 + c]
        t4 = tmp[32 + c]
        t5 = tmp[40 + c]
        t6 = tmp[48 + c]
        t7 = tmp[56 + c]
        for y in range(8):
            v = int(t0 * M[0][y] + t1 * M[1][y] + t2 * M[2][y] + t3 * M[3][y]
                    + t4 * M[4][y] + t5 * M[5][y] + t6 * M[6][y] + t7 * M[7][y]
                    + 128.5)
            out[y * 8 + c] = 0 if v < 0 else (255 if v > 255 else v)
    return out


def _split_scan(data, start):
    """把 SOS 后的熵编码数据按重启标记切段,并去掉 0xFF00 填充。"""
    segs = []
    cur = bytearray()
    i = start
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0xFF:
            if i + 1 >= n:
                break
            nb = data[i + 1]
            if nb == 0x00:
                cur.append(0xFF)
                i += 2
                continue
            if nb == 0xFF:
                i += 1
                continue
            if 0xD0 <= nb <= 0xD7:
                segs.append(bytes(cur))
                cur = bytearray()
                i += 2
                continue
            break
        cur.append(b)
        i += 1
    segs.append(bytes(cur))
    return segs


def decode_jpeg_rgb(data):
    """解码基线 JPEG,返回 (宽, 高, RGB 字节串)。不支持渐进式/算术编码。"""
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        raise ValueError("不是 JPEG 数据")
    quant = {}
    huff = {}
    comps = None
    width = height = 0
    restart_interval = 0
    scan_info = None
    scan_start = None
    pos = 2
    n = len(data)
    while pos + 3 < n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if marker == 0xD9:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            pos += 2
            continue
        seglen = (data[pos + 2] << 8) | data[pos + 3]
        seg = data[pos + 4: pos + 2 + seglen]
        if marker == 0xDB:  # DQT
            i = 0
            while i < len(seg):
                pq, tq = seg[i] >> 4, seg[i] & 0x0F
                i += 1
                if pq:
                    quant[tq] = [(seg[i + 2 * j] << 8) | seg[i + 2 * j + 1]
                                 for j in range(64)]
                    i += 128
                else:
                    quant[tq] = list(seg[i:i + 64])
                    i += 64
        elif marker == 0xC4:  # DHT
            i = 0
            while i < len(seg):
                tc, th = seg[i] >> 4, seg[i] & 0x0F
                counts = list(seg[i + 1:i + 17])
                total = sum(counts)
                symbols = list(seg[i + 17:i + 17 + total])
                huff[(tc, th)] = _HuffTable(counts, symbols)
                i += 17 + total
        elif marker == 0xDD:  # DRI
            restart_interval = (seg[0] << 8) | seg[1]
        elif marker in (0xC0, 0xC1):  # SOF0/1
            if seg[0] != 8:
                raise ValueError("仅支持 8 位精度的 JPEG")
            height = (seg[1] << 8) | seg[2]
            width = (seg[3] << 8) | seg[4]
            comps = []
            for j in range(seg[5]):
                cid, hv, tq = seg[6 + 3 * j], seg[7 + 3 * j], seg[8 + 3 * j]
                comps.append({"id": cid, "h": hv >> 4, "v": hv & 0x0F,
                              "tq": tq, "dc": None, "ac": None,
                              "pred": 0, "plane": None, "cw": 0, "ch": 0})
        elif marker == 0xC2:
            raise ValueError("不支持渐进式 JPEG")
        elif marker == 0xDA:  # SOS
            ns = seg[0]
            for j in range(ns):
                cid, tt = seg[1 + 2 * j], seg[2 + 2 * j]
                for c in comps:
                    if c["id"] == cid:
                        c["dc"] = huff[(0, tt >> 4)]
                        c["ac"] = huff[(1, tt & 0x0F)]
            scan_start = pos + 2 + seglen
            break
        pos += 2 + seglen
    if comps is None or scan_start is None:
        raise ValueError("JPEG 结构不完整")
    if len(comps) not in (1, 3):
        raise ValueError(f"不支持 {len(comps)} 分量的 JPEG")

    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    for c in comps:
        c["cw"] = -(-width * c["h"] // hmax)
        c["ch"] = -(-height * c["v"] // vmax)
        c["plane"] = bytearray(c["cw"] * c["ch"])
    mcux = -(-width // (8 * hmax))
    mcuy = -(-height // (8 * vmax))

    segs = _split_scan(data, scan_start)
    seg_idx = 0
    reader = _BitReader(segs[0])
    mcu = 0
    for my in range(mcuy):
        for mx in range(mcux):
            if restart_interval and mcu and mcu % restart_interval == 0:
                seg_idx += 1
                if seg_idx >= len(segs):
                    raise ValueError("JPEG 数据不完整:重启段不足")
                reader = _BitReader(segs[seg_idx])
                for c in comps:
                    c["pred"] = 0
            for c in comps:
                qt = quant[c["tq"]]
                for by in range(c["v"]):
                    for bx in range(c["h"]):
                        coef, c["pred"] = _decode_block(
                            reader, c["dc"], c["ac"], qt, c["pred"])
                        block = _idct_block(coef)
                        ox = (mx * c["h"] + bx) * 8
                        oy = (my * c["v"] + by) * 8
                        plane, cw, chh = c["plane"], c["cw"], c["ch"]
                        for yy in range(8):
                            py = oy + yy
                            if py >= chh:
                                break
                            src = yy * 8
                            dst = py * cw + ox
                            wpx = min(8, cw - ox)
                            if wpx > 0:
                                plane[dst:dst + wpx] = block[src:src + wpx]
            mcu += 1

    if len(comps) == 1:
        plane, cw = comps[0]["plane"], comps[0]["cw"]
        rgb = bytearray(width * height * 3)
        for y in range(height):
            srow = y * cw
            drow = y * width * 3
            for x in range(width):
                v = plane[srow + x]
                rgb[drow + 3 * x] = v
                rgb[drow + 3 * x + 1] = v
                rgb[drow + 3 * x + 2] = v
        return width, height, bytes(rgb)

    yp, cbp, crp = comps[0], comps[1], comps[2]
    cr_r = [int(round(1.402 * (i - 128))) for i in range(256)]
    cb_b = [int(round(1.772 * (i - 128))) for i in range(256)]
    cb_g = [int(round(-0.344136 * (i - 128))) for i in range(256)]
    cr_g = [int(round(-0.714136 * (i - 128))) for i in range(256)]
    rgb = bytearray(width * height * 3)
    for y in range(height):
        yrow = y * yp["cw"]
        cbrow = (y * cbp["ch"] // height) * cbp["cw"]
        crrow = (y * crp["ch"] // height) * crp["cw"]
        drow = y * width * 3
        for x in range(width):
            yv = yp["plane"][yrow + x]
            cb = cbp["plane"][cbrow + (x * cbp["cw"] // width)]
            cr = crp["plane"][crrow + (x * crp["cw"] // width)]
            r = yv + cr_r[cr]
            g = yv + cb_g[cb] + cr_g[cr]
            b = yv + cb_b[cb]
            o = drow + 3 * x
            rgb[o] = 0 if r < 0 else (255 if r > 255 else r)
            rgb[o + 1] = 0 if g < 0 else (255 if g > 255 else g)
            rgb[o + 2] = 0 if b < 0 else (255 if b > 255 else b)
    return width, height, bytes(rgb)


def _jpeg_convert_gdiplus(jpeg, max_w=0, max_h=0):
    """Windows 专用:用系统 GDI+ 解码图片,可等比缩放到 max_w x max_h 以内。
    返回 (PNG字节, 原图宽, 原图高),失败返回 None。"""
    import ctypes
    import os
    import tempfile

    class _StartupInput(ctypes.Structure):
        _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                    ("DebugEventCallback", ctypes.c_void_p),
                    ("SuppressBackgroundThread", ctypes.c_int),
                    ("SuppressExternalCodecs", ctypes.c_int)]

    class _Guid(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    # PNG 编码器 CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}
    png_clsid = _Guid(0x557CF406, 0x1A04, 0x11D3,
                      (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00,
                                           0xF8, 0x1E, 0xF3, 0x2E))
    gdiplus = ctypes.WinDLL("gdiplus")
    gdiplus.GdipLoadImageFromFile.argtypes = [
        ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipSaveImageToFile.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(_Guid), ctypes.c_void_p]
    gdiplus.GdipGetImageWidth.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gdiplus.GdipGetImageHeight.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gdiplus.GdipGetImageThumbnailImage.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p]
    token = ctypes.c_void_p(0)
    inp = _StartupInput(1, None, 0, 0)
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) != 0:
        return None
    src_path = dst_path = None
    try:
        fd, src_path = tempfile.mkstemp(suffix=".img")
        with os.fdopen(fd, "wb") as f:
            f.write(jpeg)
        dst_path = src_path + ".png"
        image = ctypes.c_void_p(0)
        if gdiplus.GdipLoadImageFromFile(src_path, ctypes.byref(image)) != 0:
            return None
        try:
            w = ctypes.c_uint(0)
            h = ctypes.c_uint(0)
            gdiplus.GdipGetImageWidth(image, ctypes.byref(w))
            gdiplus.GdipGetImageHeight(image, ctypes.byref(h))
            ow, oh = w.value, h.value
            save_img = image
            thumb = ctypes.c_void_p(0)
            if max_w and (ow > max_w or oh > max_h):
                scale = min(max_w / ow, max_h / oh)
                tw = max(1, int(ow * scale))
                th = max(1, int(oh * scale))
                if gdiplus.GdipGetImageThumbnailImage(
                        image, tw, th, ctypes.byref(thumb), None, None) == 0 \
                        and thumb:
                    save_img = thumb
            try:
                if gdiplus.GdipSaveImageToFile(
                        save_img, dst_path, ctypes.byref(png_clsid), None) != 0:
                    return None
            finally:
                if thumb:
                    gdiplus.GdipDisposeImage(thumb)
        finally:
            gdiplus.GdipDisposeImage(image)
        with open(dst_path, "rb") as f:
            return f.read(), ow, oh
    finally:
        gdiplus.GdiplusShutdown(token)
        for fp in (src_path, dst_path):
            if fp:
                try:
                    os.unlink(fp)
                except OSError:
                    pass


def _downscale_rgb_nn(rgb, w, h, max_w, max_h):
    """RGB 字节最近邻等比缩小到 max_w x max_h 以内,返回 (字节, 新宽, 新高)。"""
    scale = min(max_w / w, max_h / h)
    dw = max(1, int(w * scale))
    dh = max(1, int(h * scale))
    out = bytearray(dw * dh * 3)
    for y in range(dh):
        srow = min(h - 1, y * h // dh) * w * 3
        drow = y * dw * 3
        for x in range(dw):
            s = srow + min(w - 1, x * w // dw) * 3
            d = drow + x * 3
            out[d:d + 3] = rgb[s:s + 3]
    return bytes(out), dw, dh


def frame_to_display(jpeg, max_w=512, max_h=384):
    """把一帧图片转成 tkinter PhotoImage 可加载的数据,并缩放到
    max_w x max_h 以内。返回 (数据字节, 原图宽, 原图高)。
    Windows 走 GDI+(PNG),否则纯 Python 解码(PPM)。"""
    import sys
    if sys.platform.startswith("win"):
        try:
            r = _jpeg_convert_gdiplus(jpeg, max_w, max_h)
            if r:
                return r
        except Exception:  # noqa: BLE001
            pass
        if jpeg[:8] == b"\x89PNG\r\n\x1a\n":
            raise ValueError("设备返回 PNG 图片,GDI+ 转换失败")
    if jpeg[:8] == b"\x89PNG\r\n\x1a\n":
        raise ValueError("设备返回 PNG 图片,当前环境无法解码显示")
    w, h, rgb = decode_jpeg_rgb(jpeg)
    if w > max_w or h > max_h:
        rgb, dw, dh = _downscale_rgb_nn(rgb, w, h, max_w, max_h)
        return b"P6\n%d %d\n255\n" % (dw, dh) + rgb, w, h
    return b"P6\n%d %d\n255\n" % (w, h) + rgb, w, h


# ============================ 通道抓图 ============================
SNAPSHOT_CANDIDATES = [
    "/ISAPI/Streaming/channels/{cid}/picture",                 # 摄像机/多数 NVR
    "/ISAPI/ContentMgmt/StreamingProxy/channels/{cid}/picture",  # NVR 代理路径
    "/PSIA/Streaming/channels/{cid}/picture",                   # 老设备 PSIA
    "/Streaming/channels/{cid}/picture",
]
_SNAPSHOT_PATH_CACHE = {}


def fetch_snapshot(client, ch, stream=2):
    """抓取通道当前画面,返回图片字节。stream: 1=主码流, 2=子码流。
    cid 按海康惯例 = 通道号*100 + 码流号(如通道1子码流 = 102)。
    抓图接口自动探测,命中的路径按 (设备,通道,码流) 缓存。"""
    cid = ch * 100 + stream
    key = (client.base, ch, stream)
    cached = _SNAPSHOT_PATH_CACHE.get(key)
    order = ([cached] if cached else []) +         [p.format(cid=cid) for p in SNAPSHOT_CANDIDATES if p != cached]
    last_err = None
    for path in order:
        try:
            data = client.get(path)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
            _SNAPSHOT_PATH_CACHE[key] = path
            return data
        last_err = RuntimeError("抓图接口返回的不是图片数据")
    if cached:
        _SNAPSHOT_PATH_CACHE.pop(key, None)
    raise RuntimeError(f"抓图失败(通道{ch}): {last_err}")



# ============================ 预览取流(视频优先) ============================
MJPEG_CANDIDATES = [
    "/ISAPI/Streaming/channels/{cid}/httpPreview",
    "/ISAPI/ContentMgmt/StreamingProxy/channels/{cid}/httpPreview",
]
_PREVIEW_MODE_CACHE = {}


class _PreviewSource:
    """预览后台取流线程:先试 MJPEG 视频流(httpPreview 接口),设备不支持
    就自动降级为连续抓图(效果类似低帧率视频)。UI 侧轮询 latest 取最新帧。
    latest = (显示字节, 原图宽, 原图高);seq 每出一帧加一。"""

    def __init__(self, client, ch, stream=2, box=(512, 384)):
        self.client = client
        self.ch = ch
        self.stream = stream
        self.box = box
        self.mode = None        # "mjpeg" / "snapshot"
        self.latest = None
        self.seq = 0
        self.error = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._resp = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def refresh_now(self):
        self._wake.set()

    # ---------- 内部 ----------
    def _publish(self, jpeg):
        try:
            disp, w, h = frame_to_display(jpeg, *self.box)
        except Exception:  # noqa: BLE001
            return False
        self.latest = (disp, w, h)
        self.seq += 1
        self.error = None
        return True

    def _run(self):
        cid = self.ch * 100 + self.stream
        key = (self.client.base, self.ch, self.stream)
        if _PREVIEW_MODE_CACHE.get(key) != "snapshot":
            got = self._run_mjpeg(cid)
            if self._stop.is_set():
                return
            # 视频出过帧→记住;从未出帧→记住用抓图,下次直接抓图
            _PREVIEW_MODE_CACHE[key] = "mjpeg" if got else "snapshot"
        self._run_snapshot()

    def _open_stream(self, path):
        req = urllib.request.Request(self.client.base + path)
        return self.client.opener.open(req, timeout=6)

    def _run_mjpeg(self, cid):
        import time
        got = False
        for tpl in MJPEG_CANDIDATES:
            if self._stop.is_set() or got:
                break
            try:
                resp = self._open_stream(tpl.format(cid=cid))
            except Exception:  # noqa: BLE001
                continue
            self._resp = resp
            buf = bytearray()
            deadline = time.monotonic() + 8  # 8 秒没出一帧就放弃该路径
            try:
                while not self._stop.is_set():
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        i = buf.find(b"\xff\xd8")
                        if i < 0:
                            if len(buf) > 2:
                                del buf[:-2]
                            break
                        j = buf.find(b"\xff\xd9", i + 2)
                        if j < 0:
                            if i:
                                del buf[:i]
                            break
                        frame = bytes(buf[i:j + 2])
                        del buf[:j + 2]
                        if 500 <= len(frame) <= 3000000 \
                                and self._publish(frame):
                            got = True
                            self.mode = "mjpeg"
                            deadline = None
                    if deadline and time.monotonic() > deadline:
                        break
                    if len(buf) > 4000000:
                        del buf[:-2]
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001
                    pass
                self._resp = None
        return got

    def _run_snapshot(self):
        self.mode = "snapshot"
        while not self._stop.is_set():
            try:
                data = fetch_snapshot(self.client, self.ch, self.stream)
                self._publish(data)
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                if self._wake.wait(2):
                    self._wake.clear()
                continue
            if self._wake.wait(0.05):
                self._wake.clear()


# ============================ 图形界面 ============================
class OSDTool:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import scrolledtext
        self.tk = tk
        self.root = root
        root.title(f"海康 OSD 工具(简洁版) v{__version__}")
        root.resizable(False, False)

        # ---- 步骤2: 录像机连接 ----
        f1 = tk.LabelFrame(root, text="步骤1: 录像机连接", padx=8, pady=6)
        f1.grid(row=1, column=0, padx=10, pady=(10, 4), sticky="ew")
        tk.Label(f1, text="录像机 IP:").grid(row=0, column=0, sticky="e")
        self.e_ip = tk.Entry(f1, width=18)
        self.e_ip.grid(row=0, column=1, padx=4)
        tk.Label(f1, text="端口:").grid(row=0, column=2, sticky="e")
        self.e_port = tk.Entry(f1, width=6)
        self.e_port.insert(0, "80")
        self.e_port.grid(row=0, column=3, padx=4)
        self.var_https = tk.BooleanVar(value=False)
        tk.Checkbutton(f1, text="HTTPS", variable=self.var_https).grid(row=0, column=4)

        tk.Label(f1, text="用户名:").grid(row=1, column=0, sticky="e", pady=4)
        self.e_user = tk.Entry(f1, width=18)
        self.e_user.insert(0, "admin")
        self.e_user.grid(row=1, column=1, padx=4)
        tk.Label(f1, text="密码:").grid(row=1, column=2, sticky="e")
        from tkinter import ttk
        # 常用密码预设:开源版本不含任何真实密码。
        # 如需预设,在 values 中填入自己的密码,例如 values=["password1", "password2"]。
        self.e_pwd = ttk.Combobox(f1, width=16, show="*",
                                  values=[])
        self.e_pwd.grid(row=1, column=3, padx=4, columnspan=2, sticky="w")

        tk.Label(f1, text="通道号:").grid(row=2, column=0, sticky="e")
        self.e_ch = tk.Entry(f1, width=8)
        self.e_ch.insert(0, "1")
        self.e_ch.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        tk.Button(f1, text="读取当前 OSD", command=self.on_read,
                  width=12).grid(row=2, column=2, pady=4)
        tk.Button(f1, text="通道列表", command=self.on_channels,
                  width=10).grid(row=2, column=3, pady=4)
        tk.Button(f1, text="诊断接口", command=self.on_diagnose,
                  width=10).grid(row=2, column=4, pady=4)
        # ---- 步骤3: OSD 设置 ----
        f2 = tk.LabelFrame(root, text="步骤2: OSD 名称", padx=8, pady=6)
        f2.grid(row=2, column=0, padx=10, pady=4, sticky="ew")
        tk.Label(f2, text="OSD 名称:").grid(row=0, column=0, sticky="e")
        self.e_name = tk.Entry(f2, width=32)
        self.e_name.grid(row=0, column=1, columnspan=5, sticky="w", padx=4)

        self.lbl_status = tk.Label(f2, text="尚未读取", fg="gray", anchor="w")
        self.lbl_status.grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))
        self.btn_apply = tk.Button(root, text="应用修改", command=self.on_apply,
                                   width=20, height=2)
        self.btn_apply.grid(row=3, column=0, pady=6)

        # ---- 日志 ----
        f3 = tk.LabelFrame(root, text="日志", padx=8, pady=6)
        f3.grid(row=4, column=0, padx=10, pady=(4, 10), sticky="ew")
        self.log_area = scrolledtext.ScrolledText(f3, width=64, height=9,
                                                  state="disabled")
        self.log_area.pack()
        # ---- 右侧: 通道预览(读取 OSD 时自动显示,应用后自动刷新) ----
        fp = tk.Frame(root)
        fp.grid(row=0, column=1, rowspan=6, sticky="n", padx=(0, 10), pady=6)
        tk.Label(fp, text="通道预览").pack(anchor="w")
        bar = tk.Frame(fp)
        bar.pack(fill="x", pady=2)
        self.pv_stream = tk.IntVar(value=2)
        tk.Radiobutton(bar, text="子码流", variable=self.pv_stream, value=2,
                       command=self._pv_stream_changed).pack(side="left")
        tk.Radiobutton(bar, text="主码流", variable=self.pv_stream, value=1,
                       command=self._pv_stream_changed).pack(side="left")
        tk.Button(bar, text="停止", command=self._stop_preview,
                  width=6).pack(side="left", padx=6)
        self.pv_status = tk.Label(bar, text="未开始", fg="gray")
        self.pv_status.pack(side="left")
        self.pv_img = tk.Label(fp, text="点「读取当前 OSD」后\n这里显示通道画面",
                               bg="black", fg="white", width=64, height=24)
        self.pv_img.pack()
        tk.Label(fp, text="优先播放视频流;设备不支持时自动改为连续抓图",
                 fg="gray").pack(anchor="w", pady=2)
        self.pv_src = None
        self._pv_seq = 0
        self._pv_poll_id = None
        self._pv_target = None
        self._pv_photo = None

        tk.Label(root, text=f"v{__version__}", fg="gray").grid(
            row=5, column=0, sticky="e", padx=10, pady=(0, 6))

        self.log(f"海康 OSD 工具(简洁版) v{__version__} 就绪。"
                 "请填写连接信息后点击「读取当前 OSD」。")

    # ---------- 辅助 ----------
    def log(self, msg):
        def _w():
            self.log_area.config(state="normal")
            self.log_area.insert("end",
                                 f"{datetime.now():%H:%M:%S} {msg}\n")
            self.log_area.see("end")
            self.log_area.config(state="disabled")
        self.root.after(0, _w)

    def make_client(self):
        host = self.e_ip.get().strip()
        port_s = self.e_port.get().strip()
        user = self.e_user.get().strip()
        pwd = self.e_pwd.get()
        ch_s = self.e_ch.get().strip()
        if not host:
            raise ValueError("请填写录像机 IP")
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError("端口必须是数字")
        if not 1 <= port <= 65535:
            raise ValueError("端口范围 1-65535")
        if not user:
            raise ValueError("请填写用户名")
        if not ch_s.isdigit() or int(ch_s) < 1:
            raise ValueError("通道号必须是正整数")
        client = HikClient(host, port, user, pwd,
                           use_https=self.var_https.get())
        return client, int(ch_s)

    def _bg(self, task):
        def w():
            try:
                task()
            except Exception as e:  # noqa: BLE001
                self.log(f"出错: {e}")
                self.root.after(0, lambda: self._errbox(str(e)))
        threading.Thread(target=w, daemon=True).start()

    def _errbox(self, msg):
        from tkinter import messagebox
        messagebox.showerror("出错", msg)

    # ---------- 读取 ----------
    def on_read(self):
        try:
            client, ch = self.make_client()
        except ValueError as e:
            from tkinter import messagebox
            messagebox.showwarning("输入有误", str(e))
            return
        self.log(f"正在读取通道 {ch} 的 OSD 配置…")
        self._start_preview(client, ch)

        def task():
            info = read_osd(client, ch)
            self.root.after(0, lambda: self.show_read(info, ch))
        self._bg(task)

    def show_read(self, info, ch):
        self.e_name.delete(0, "end")
        self.e_name.insert(0, info["name"])
        self.lbl_status.config(
            text=f"通道{ch} 已读取: 当前名称 {info['name']!r}(本工具只改名称)",
            fg="black")
        self.log(f"读取成功: 名称={info['name']!r}")
        self.log(f"使用接口: {info['ch_path'] or '通道名称接口不可用'} / "
                 f"{info['ov_path'] or 'OSD 叠加接口不可用'}")
        if info["overlay_name"] and info["overlay_name"] != info["name"]:
            self.log(f"提示:叠加内的名称为 {info['overlay_name']!r},"
                     f"与通道名称不一致,应用时将一并同步。")

    def on_diagnose(self):
        try:
            client, ch = self.make_client()
        except ValueError as e:
            from tkinter import messagebox
            messagebox.showwarning("输入有误", str(e))
            return
        self.log(f"开始诊断通道 {ch} 的接口可用性…")

        def task():
            for line in diagnose(client, ch):
                self.log(line)
            self.log("诊断完成。若 OSD 接口不可用,请把以上日志发给开发者分析。")
        self._bg(task)

    # ---------- 摄像头定位 ----------
    def on_channels(self):
        from tkinter import messagebox
        try:
            client, _ = self.make_client()
        except ValueError as e:
            messagebox.showwarning("输入有误", str(e))
            return
        self.log("正在获取录像机通道列表…")

        def task():
            rows = list_nvr_channels(client)
            if not rows:
                self.log("未能获取通道列表。")
                return
            for cid, nm, ip in rows:
                self.log(f"  通道 {cid}: 名称={nm!r}"
                         + (f" 摄像机IP={ip}" if ip else ""))
            self.log(f"共 {len(rows)} 个通道。可对照摄像机IP/名称确定通道号后手动填写。")
        self._bg(task)

    # ---------- 应用 ----------
    # ---------- 嵌入式通道预览 ----------
    def _start_preview(self, client, ch):
        self._stop_preview()
        src = _PreviewSource(client, ch, self.pv_stream.get())
        self.pv_src = src
        self._pv_target = (client.base, ch)
        self._pv_seq = 0
        src.start()
        self.pv_status.config(text="正在连接画面…", fg="gray")
        self._pv_poll()
        self.log(f"预览: 通道{ch} 开始取流(优先视频,不支持自动转抓图)")

    def _stop_preview(self):
        if getattr(self, "_pv_poll_id", None) is not None:
            try:
                self.root.after_cancel(self._pv_poll_id)
            except Exception:  # noqa: BLE001
                pass
            self._pv_poll_id = None
        if getattr(self, "pv_src", None) is not None:
            self.pv_src.stop()
            self.pv_src = None
            self.pv_status.config(text="已停止", fg="gray")

    def _pv_stream_changed(self):
        src = getattr(self, "pv_src", None)
        if src is not None:
            self._start_preview(src.client, src.ch)

    def _pv_poll(self):
        self._pv_poll_id = None
        src = getattr(self, "pv_src", None)
        if src is None:
            return
        try:
            if src.seq != self._pv_seq and src.latest is not None:
                self._pv_seq = src.seq
                disp, w, h = src.latest
                photo = self.tk.PhotoImage(data=disp)
                self._pv_photo = photo
                self.pv_img.config(image=photo, text="", width=0, height=0)
                mode = "视频" if src.mode == "mjpeg" else "抓图"
                self.pv_status.config(
                    text=f"{mode} · {w}×{h} · {datetime.now():%H:%M:%S}",
                    fg="gray")
            elif src.error and src.latest is None:
                self.pv_status.config(text=f"取流失败: {src.error[:40]}",
                                      fg="red")
        except Exception:  # noqa: BLE001
            pass
        self._pv_poll_id = self.root.after(150, self._pv_poll)

    def _refresh_preview_after_apply(self, client, ch):
        # 应用成功后用最新连接参数重启预览,确保看到的是下发后的画面
        self._start_preview(client, ch)
        self.log("预览: 已按应用结果重新显示画面")

    def on_apply(self):
        try:
            client, ch = self.make_client()
        except ValueError as e:
            from tkinter import messagebox
            messagebox.showwarning("输入有误", str(e))
            return
        new_name = self.e_name.get()
        if not new_name.strip():
            from tkinter import messagebox
            messagebox.showwarning("输入有误", "OSD 名称不能为空")
            return
        self.log(f"开始应用: 通道{ch} 名称={new_name!r}"
                 "(只改名称,位置保持不变)…")

        def task():
            summary = apply_osd(client, ch, new_name, 0, 0, 0, 0,
                                change_pos=False)
            for s in summary:
                self.log(s)
            # 下发后重新读取确认
            info = read_osd(client, ch)
            self.root.after(0, lambda: self.show_read(info, ch))
            self.root.after(0, lambda: self.log("应用完成,已重新读取确认。"))
            self.root.after(0, lambda: self._refresh_preview_after_apply(
                client, ch))
        self._bg(task)


def main():
    import tkinter as tk
    root = tk.Tk()
    OSDTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()
