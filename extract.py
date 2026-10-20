#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bloodborne Launcher (Awakenchan/BloodBorneButPc) 汉化工具 —— 第 1 步：提取字符串

原理
----
该启动器是 .NET(WPF) 单文件应用，界面文字以 .NET 字符串字面量形式存放在程序集的
#US 堆中：  [压缩长度前缀] + [UTF-16LE 数据] + [可选尾字节]

.NET 字符串是 UTF-16，中文同样是 UTF-16（每个基本汉字 2 字节）。因此只要替换文本的
“字符数”与原文相同，写入字节数就完全一致：不改长度前缀、不移动任何数据 —— 最安全的汉化方式。

本脚本扫描 exe，用“长度前缀校验”精确定位字符串字面量，导出可翻译的 UI 文本。

用法：
    python extract.py [exe路径] [-o strings.json]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bamlres                                        # noqa: E402

# 视为“可打印”的 UTF-16 码位范围（覆盖界面文字常见符号：× ° – … ™ © 等）
RANGES = [
    (0x0020, 0x007E), (0x00A0, 0x024F), (0x2000, 0x206F), (0x20A0, 0x20BF),
    (0x2100, 0x214F), (0x2190, 0x21FF), (0x2500, 0x257F), (0x0400, 0x04FF),
    (0x3000, 0x30FF), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF),
]


def printable(u):
    for a, b in RANGES:
        if a <= u <= b:
            return True
    return False


# 制表/换行：字面量常以它开头（如 "\n\nYour settings already suit this PC."），
# 渲染时是排版用的空行，必须原样保留。
LEAD = {0x0009, 0x000A, 0x000D}


def ok_char(u):
    return printable(u) or u in LEAD


def read_cint(data, i):
    b0 = data[i]
    if b0 & 0x80 == 0:
        return b0, 1
    if b0 & 0xC0 == 0x80:
        return ((b0 & 0x3F) << 8) | data[i + 1], 2
    return ((b0 & 0x1F) << 24) | (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3], 4


def valid_prefix(data, off, nchars):
    """off 前是否为合法 #US 长度前缀，且字符数恰为 nchars。"""
    for back in (1, 2):
        if off - back < 0:
            continue
        try:
            v, consumed = read_cint(data, off - back)
        except IndexError:
            continue
        if off - back + consumed == off and (v >> 1) == nchars:
            tail = data[off + nchars * 2] if off + nchars * 2 < len(data) else 0
            if tail in (0x00, 0x01):
                return True
    return False


TECH_EXT = re.compile(r"\.(json|exe|ini|dll|txt|bin|py|zip|spv|log)$", re.I)


def is_tech(t):
    """技术 token：标识符、配置键、路径、常量、格式串等，不可翻译。"""
    # 下划线/反斜杠只在“短 token”里才说明它是标识符或路径；
    # 长句里出现 bloodborne_pc、C:\... 属于正常文案，不能整句丢弃。
    if len(t) <= 40 and ("_" in t or "\\" in t):
        return True
    if len(t) <= 40 and TECH_EXT.search(t):
        return True
    if "=" in t or "%" in t and not re.search(r"[a-z]{3}", t):
        return True
    if re.fullmatch(r"[a-z0-9_.\-/:]+", t):        # 全小写标识符 / 键名
        return True
    if len(t) <= 7 and " " not in t and re.fullmatch(r"[A-Z0-9_.\-/]+", t):
        return True                                # 短全大写常量/缩写（HDR/FPS/SSAO/CPU…）
    if t.startswith("/") or t.startswith("BBPC"):
        return True
    return False


def is_ui(t):
    """粗筛：看起来像界面文字。"""
    if len(t) < 2 or not re.search(r"[A-Za-z]", t):
        return False
    if is_tech(t):
        return False
    if " " in t:
        return True
    if re.match(r"^[A-Z][a-z]", t):                 # Title case 单词：Graphics / Settings
        return True
    if len(t) >= 3 and re.fullmatch(r"[A-Z]+[A-Z'’\-]*", t):   # 全大写标签：COMPLETION / SYSTEM
        return True
    if t.endswith(("?", ":")) and len(t) > 3:
        return True
    return False


CAND = re.compile(rb"(?:[\x20-\x7e]\x00){2,}")
RES_MAGIC = b"\xce\xca\xef\xbe"          # .NET .resources 魔数（.g.resources 流）
BAML_END = b"\x99\xfd"                    # BAML 字符串记录终结符

# 已汉化标记：只要在程序集里出现这个词，就说明已经汉化过了。
# 选「游戏文件夹」是因为它稳定出现在 #US 里多处（词条改了也不会全都没了）。
MARKER = ("游戏文件夹".encode("utf-16-le"), "游戏文件夹".encode("utf-8"))


def scan_baml(data):
    """扫描 WPF 的 .g.resources 中的 BAML 字符串表。

    这些字符串（侧边栏导航标签、窗口标题、搜索框占位符等）在 BAML 里以
    [单字节长度][UTF-8 数据] 存放，所以按“字节数”等长替换即可。

    具体块位置由 _bamlres 解析资源名表得出 —— 不再依赖写死的字节窗口，
    因此作者往资源里塞多少图片都不影响 BAML 的定位。
    """
    return _bamlres.scan_strings(data)


def decode_from(data, off, n):
    """从 off 起解出 n 个 UTF-16 字符；含不可接受字符则返回 None。"""
    if off + n * 2 >= len(data):
        return None
    out = []
    for k in range(n):
        u = data[off + k * 2] | (data[off + k * 2 + 1] << 8)
        if not ok_char(u):
            return None
        out.append(chr(u))
    return "".join(out)


def scan(data):
    """定位所有 #US 字符串字面量。

    思路：先用 ASCII 紧凑串快速找出“候选起点”（ASCII 串的高字节为 0 保证了对齐正确），
    再用该起点前 1~4 字节的长度前缀反推出完整字符数（前缀是权威的，可覆盖含 × ° – 等
    非 ASCII 符号的整串），逐字符校验可打印性与尾字节。

    注意：不少字面量以换行开头（"\\n\\nYour settings already suit this PC."），
    ASCII 锚点因此会晚 2~6 字节，所以锚点还要向前试探若干位置。
    """
    n_total = len(data)
    found = {}
    for m in CAND.finditer(data):
        base = m.start()
        for lead in (0, 2, 4, 6, 8):
            off = base - lead
            if off < 0:
                continue
            if lead and not ok_char(data[off] | (data[off + 1] << 8)):
                continue
            hit = None
            for back in (1, 2, 3, 4):
                j = off - back
                if j < 0:
                    continue
                try:
                    v, consumed = read_cint(data, j)
                except IndexError:
                    continue
                if j + consumed != off or not (v & 1):
                    continue
                n = v >> 1
                if n < 2 or n > 4000:
                    continue
                end = off + n * 2
                if end >= n_total or data[end] not in (0x00, 0x01):
                    continue
                text = decode_from(data, off, n)
                if text is None:
                    continue
                hit = text
                break
            if hit is not None:
                found[off] = hit
                break
    return sorted(found.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe", nargs="?", default="Bloodborne Launcher.exe")
    ap.add_argument("-o", "--out", default="strings.json")
    ap.add_argument("--baml-out", default="baml.json")
    args = ap.parse_args()

    data = open(args.exe, "rb").read()
    print(f"读取 {args.exe}: {len(data):,} 字节")

    if any(m in data for m in MARKER):
        print()
        print("提示：这个启动器已经是汉化版了（检测到中文界面标记）。")
        print("      本次不重新提取，保留现有 strings.json。")
        print("      启动器更新到新版本后（会变回英文），再运行本脚本即可重新汉化。")
        return 0

    ok = []
    pairs = scan(data)
    print(f"定位到字符串字面量: {len(pairs):,}")
    for off, text in pairs:
        if not is_ui(text):
            continue
        ok.append({"offset": off, "chars": len(text), "text": text})

    ok.sort(key=lambda r: r["offset"])
    json.dump(ok, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    uniq = len({r["text"] for r in ok})
    print(f"校验通过的 UI 字面量 {len(ok):,} 处（去重后 {uniq:,} 条唯一文本）-> {args.out}")

    baml = scan_baml(data)
    json.dump(baml, open(args.baml_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"BAML(XAML 资源)字符串 {len(baml):,} 条 -> {args.baml_out}")
    for r in baml[:6]:
        print(f"  {r['offset']:>10}  [{r['bytes']:>3}B]  {r['text']}")


if __name__ == "__main__":
    main()
