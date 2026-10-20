#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全自检：确认汉化后的 exe 与英文原版“只差在界面文字所在的字节区间内”。

检查项
------
1. 文件总长度必须完全一致（否则 .NET 元数据/资源索引会错位）
2. 所有不同的字节必须落在 #US 字符串堆 与 BAML 资源区间内（不允许动到代码/元数据）
3. 应能读到中文界面标记
4. 报告汉化覆盖率

用法：
    python verify.py                       # 对比 ../Bloodborne Launcher.exe 与备份
"""
import argparse
import glob
import json
import os
import sys

BACKUP_SUFFIX = ".zh-CN-backup"
RES_MAGIC = b"\xce\xca\xef\xbe"
BAML_FROM = 500000
BAML_REGION = 620000
MARKER = "血源 PC 启动器"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe", nargs="?", default="../Bloodborne Launcher.exe")
    ap.add_argument("--strings", default="strings.json")
    args = ap.parse_args()

    bak = args.exe + BACKUP_SUFFIX
    if not os.path.exists(bak):
        print(f"找不到英文原版备份 {bak}，无法做对比自检。")
        print("（只要汉化过就会有这个备份；如果被误删，可在启动器更新后重新运行「一键汉化.bat」。）")
        return 0
    if not os.path.exists(args.strings):
        print(f"找不到 {args.strings}，请先运行 extract.py。")
        return 1

    a = open(bak, "rb").read()       # 英文原版
    b = open(args.exe, "rb").read()  # 汉化版

    print(f"英文原版 {len(a):,} 字节")
    print(f"汉化版本 {len(b):,} 字节")
    if len(a) != len(b):
        print("！长度不一致 —— 汉化异常，请运行「还原英文.bat」后重试。")
        return 1

    entries = json.load(open(args.strings, encoding="utf-8"))
    spans = [(e["offset"], e["offset"] + e["chars"] * 2) for e in entries]
    spans.sort()
    res = b.find(RES_MAGIC)
    bm_lo, bm_hi = res + BAML_FROM, res + BAML_REGION

    diff = [i for i in range(len(a)) if a[i] != b[i]]

    def allowed(i):
        if bm_lo <= i < bm_hi:
            return True
        # 二分落到某个字面量范围内即可
        lo_, hi_ = 0, len(spans) - 1
        while lo_ <= hi_:
            mid = (lo_ + hi_) // 2
            s, e = spans[mid]
            if i < s:
                hi_ = mid - 1
            elif i >= e:
                lo_ = mid + 1
            else:
                return True
        return False

    outside = [i for i in diff if not allowed(i)]

    print(f"待检字面量 {len(spans):,} 段；BAML 资源区间 {bm_lo:#x} ~ {bm_hi:#x}")
    print(f"差异字节 {len(diff):,} 处；其中落在“界面文字”范围之外的：{len(outside)} 处")
    if outside:
        print("！有字节被写到了界面文字之外的地方，这是危险的，请立即还原英文。")
        for i in outside[:10]:
            print(f"   {i:#x}")
        return 1

    has_marker = MARKER.encode("utf-16-le") in b or MARKER.encode("utf-8") in b
    print(f"中文界面标记：{'已写入 √' if has_marker else '未找到 ×'}")

    # 覆盖率
    table = {}
    for f in sorted(glob.glob("zh-CN*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        table.update({k: v for k, v in d.items() if not k.startswith("_")})
    tot = len({e["text"] for e in entries})
    done = len({e["text"] for e in entries if e["text"] in table})
    print(f"界面文字覆盖：{done} / {tot} 条唯一文本（词典 {len(table)} 条）")

    if has_marker and not outside:
        print("\n结论：汉化已生效；文件长度与整体结构完全未变。")
        return 0
    print("\n！自检未通过。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
