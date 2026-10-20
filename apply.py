#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bloodborne Launcher 汉化工具 —— 第 2 步：写入中文

原理与安全性
------------
启动器是 .NET(WPF) 单文件程序，界面文字分布在两处：

  1) #US 字符串堆： [压缩长度前缀][UTF-16LE][尾字节]
     旧做法是把译文用零宽字符 U+FEFF 补齐到「原字符数」，让记录长度天然不变。
     这是错的：#US 里既有界面文字，也有被当成 XML 元素名/属性名/字典键用的
     标识符（如 Descendants("Metadata")、Attribute("Address")）。U+FEFF 虽然在
     TextBlock 里不可见，但 XmlConvert.VerifyNCName 会拒绝它，启动器点「开始」
     就抛 "The \uFEFF character cannot be included in a name"。
     现在改成写「正确的长度前缀 + 短字符串」，后面的空隙填 0。
     #US 靠 ldstr token 里的绝对偏移定位，记录之间不要求连续，缩短是安全的。

  2) BAML(XAML) 资源： [单字节长度][UTF-8]
     WPF 把侧栏/标签页标题、窗口标题编译进 BAML。汉字 UTF-8 占 3 字节。
     BAML 字符串表是「顺序读出」的，没有外部偏移，缩短一条会让后面全部错位，
     所以这里必须保持字节数不变，不足用空格补齐。

两种写法都不改变 exe 的长度与偏移：元数据、代码、资源索引全部保持原样。
词典里没有的字符串一律不动 —— 因此不会误改 XAML 枚举值(如 Display/Center/Auto)。

用法
----
    python apply.py                     # 预览：覆盖率、超长项、待翻译项，不改文件
    python apply.py --apply             # 写入（自动备份原 exe）
    python apply.py --apply --no-backup # 不备份
    python apply.py --restore           # 从备份还原

    python apply.py --exe "其它.exe"     # 处理别的副本
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bamlres                                        # noqa: E402

BACKUP_SUFFIX = ".zh-CN-backup"
RES_MAGIC = b"\xce\xca\xef\xbe"    # .NET .resources 魔数

# 已汉化标记（与 extract.py 一致）：稳定出现在 #US 里的中文词
MARKER = ("游戏文件夹".encode("utf-16-le"), "游戏文件夹".encode("utf-8"))

# ---- 识别「不能翻译的值」----------------------------------------------------
# 启动器把界面文字和内部标识符/设置键/枚举名混在一起：
#   .First(x => x.Name == "Close")        <- Close 是控件名
#   TextFormattingMode="Display"          <- Display 是枚举值
#   settings["Resolution Patch 1280x720 (16:9)"]   <- 下拉项被当键用
# 翻译这些会让代码找不到元素或解析失败。所以：
#   1) #US 层：用 unsafe-values.json（由 analyze.cs 从 IL 里挖出来的清单）整值排除
#   2) BAML 层：枚举词只有在「紧跟类型转换属性名」时才跳过，普通标签照常翻译
UNSAFE_FILE = "unsafe-values.json"

# WPF 里会被 TypeConverter 解析的属性名。这些属性后面的字符串是枚举/类型值。
CONV_PROPS = (b"TextFormattingMode", b"TextHintingMode", b"TextRenderingMode",
              b"RenderOptions", b"BitmapScalingMode", b"EdgeMode")

# 既是界面标签、又是 WPF 枚举名的词（如导航标签 "Display"）。
# 只有在紧跟上面那些属性名时才跳过。
BAML_ENUM_WORDS = {
    "Display", "Classic", "Auto", "Center", "Left", "Right", "Top", "Bottom",
    "Small", "Medium", "Large", "Bold", "Normal", "Italic", "Dark", "Light",
    "Visible", "Hidden", "Collapsed", "Stretch", "Fill", "Uniform", "None",
    "Wrap", "NoWrap", "Manual", "Automatic", "Overflow", "Default", "Main",
}


def load_unsafe_values():
    """读取 IL 分析得出的「标识符清单」。文件不存在就返回空集合。"""
    for p in (UNSAFE_FILE, os.path.join(os.path.dirname(os.path.abspath(__file__)), UNSAFE_FILE)):
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                if isinstance(d, list):
                    return set(d)
                if isinstance(d, dict):
                    return set(d.get("unsafe", [])) - set(d.get("allow", []))
            except Exception as e:
                print(f"  ! 读取 {p} 失败: {e}")
    return set()



def is_localized(data):
    return any(m in data for m in MARKER)

# 注意：文件名不能匹配 "zh-CN*.json"，否则会被当成 #US 词典重复载入
BAML_FILES = ["baml-zh-CN.json"]
US_GLOB = "zh-CN*.json"


# ---------------------------------------------------------------- #US 词典
def read_cint(data, i):
    b0 = data[i]
    if b0 & 0x80 == 0:
        return b0, 1
    if b0 & 0xC0 == 0x80:
        return ((b0 & 0x3F) << 8) | data[i + 1], 2
    return ((b0 & 0x1F) << 24) | (data[i + 1] << 16) | (data[i + 2] << 8) | data[i + 3], 4


def enc_cint(v):
    """ECMA-335 压缩无符号整数（read_cint 的逆运算）。"""
    if v < 0x80:
        return bytes([v])
    if v < 0x4000:
        return bytes([0x80 | (v >> 8), v & 0xFF])
    return bytes([0xC0 | (v >> 24), (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])


def find_us_prefix(buf, off, chars):
    """off 指向字符串数据，往前找这条 #US 记录的长度前缀。

    返回 (前缀起点, 声明长度, 前缀字节数)；找不到就返回 None。
    声明长度 = 字节数 + 1（含尾字节），必须正好对应 chars 个 UTF-16 字符。
    """
    for back in (1, 2, 3, 4):
        if off - back < 0:
            continue
        try:
            v, c = read_cint(buf, off - back)
        except IndexError:
            continue
        if off - back + c == off and v == chars * 2 + 1:
            return off - back, v, c
    return None


def rewrite_us_record(buf, off, chars, dst):
    """把 off 处的 #US 记录换成 dst，返回 (新字节, 写入起点, 原占用长度)。

    新记录 = [压缩长度][UTF-16LE][尾字节]，余下字节清零。
    不再用 U+FEFF 补齐：#US 靠 token 里的绝对偏移定位，记录之间不要求连续，
    缩短是安全的；而 U+FEFF 一旦落进被当作 XML 名字/属性名的字符串就会崩。
    """
    found = find_us_prefix(buf, off, chars)
    if found is None:
        return None, None, 0
    start, declared, plen = found
    old_total = plen + declared
    term = buf[off + declared - 1]              # 尾字节：0x00 普通 / 0x01 含特殊字符
    body = dst.encode("utf-16-le")
    rec = enc_cint(len(body) + 1) + body + bytes([term])
    if len(rec) > old_total:                    # 放不下（理论上不会发生）
        return None, None, old_total
    return rec + b"\x00" * (old_total - len(rec)), start, old_total


def verify_entries(data, entries):
    """校验 strings.json 是否与当前 exe 匹配：偏移处的 #US 长度前缀必须吻合。
    不吻合说明 exe 已被换成别的版本（例如启动器刚更新），此时绝不能写入。"""
    bad = 0
    for e in entries:
        off, n = e["offset"], e["chars"]
        hit = False
        for back in (1, 2):
            if off - back < 0:
                continue
            try:
                v, c = read_cint(data, off - back)
            except IndexError:
                continue
            if off - back + c == off and (v & 1) and (v >> 1) == n:
                hit = True
                break
        if not hit:
            bad += 1
    return bad


def plan_direct(data, entries, table, done):
    """直搜补漏：词典里有、但启发式扫描没收录的字符串。

    限定在主程序集的字符串区间内做 UTF-16 精确匹配，并且要求紧随其后是
    #US 尾字节(0x00/0x01)，证据足够才替换，避免误伤其它程序集。
    """
    if not entries:
        return []
    lo = max(0, min(e["offset"] for e in entries) - 65536)
    hi = min(len(data), max(e["offset"] for e in entries) + 65536)
    plan = []
    for k, v in table.items():
        if k in done or len(v) > len(k):
            continue
        raw = k.encode("utf-16-le")
        if len(raw) < 12:          # 太短的串容易误匹配，交给扫描清单
            continue
        off = lo
        while True:
            off = data.find(raw, off, hi)
            if off < 0:
                break
            end = off + len(raw)
            if end < len(data) and data[end] in (0x00, 0x01):
                plan.append((off, len(k), k, v))
            off += 2
    return plan


def load_dicts(pattern=US_GLOB):
    table = {}
    for f in sorted(glob.glob(pattern)):
        if f in BAML_FILES:
            continue
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"  ! 跳过 {f}: {e}")
            continue
        n = 0
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if isinstance(v, str) and v:
                table[k] = v
                n += 1
        if n:
            print(f"  载入 {f}: {n} 条")
    return table


def plan_us(entries, table):
    """返回 (plan, overflow, missing)。plan 元素 = (offset, chars, src, dst)"""
    plan, overflow, missing = [], [], {}
    for e in entries:
        src = e["text"]
        if src not in table:
            missing[src] = missing.get(src, 0) + 1
            continue
        dst = table[src]
        if len(dst) > e["chars"]:
            overflow.append((src, dst, e["chars"]))
            continue
        plan.append((e["offset"], e["chars"], src, dst))
    return plan, overflow, missing


# ---------------------------------------------------------------- BAML 词典
def load_baml_dict():
    table = {}
    for f in BAML_FILES:
        if not os.path.exists(f):
            continue
        d = json.load(open(f, encoding="utf-8"))
        table.update({k: v for k, v in d.items() if not k.startswith("_")})
    return table


def plan_baml(data, table):
    """BAML 字符串按 UTF-8 字节等长（不足补空格）。返回 (plan, bad)。
    plan 元素 = (offset, nbytes, src, dst)

    安全规则（比早期版本硬编码偏移稳健得多）：
      * 只扫描 _bamlres 解析出来的真实 BAML 数据块，绝不碰旁边的大图标/大图
        （作者新版往资源流里塞了 1.5 MB 图片，靠固定窗口是找不到 BAML 的）。
      * 单词既是界面标签又是 WPF 枚举名时（如 Display），只有当它**紧跟**
        类型转换属性名（TextFormattingMode 等）才跳过 —— 那是枚举值；
        作为导航标签出现的那次照常翻译。这样启动器换版本也不会因为
        偏移变化而误译或漏译。
    """
    blobs = _bamlres.baml_blobs(data)
    if not blobs:
        print("  ! 未找到 .g.resources 中的 BAML 块，跳过 XAML 部分")
        return [], []
    plan, bad, hits = [], [], {}
    for en, zh in table.items():
        raw, zhb = en.encode("utf-8"), zh.encode("utf-8")
        if len(zhb) > len(raw) or len(raw) >= 128:
            bad.append((en, zh, len(raw)))
            continue
        pat = bytes([len(raw)]) + raw
        is_enum_word = en in BAML_ENUM_WORDS
        for _, lo, hi in blobs:
            off = lo
            while True:
                off = data.find(pat, off, hi)
                if off < 0:
                    break
                skip = False
                if is_enum_word:
                    ctx = data[max(0, off - 48):off]
                    skip = any(p in ctx for p in CONV_PROPS)
                if not skip:
                    plan.append((off, len(raw), en, zh))
                    hits[en] = hits.get(en, 0) + 1
                off += len(pat)
    for en, zh, n in bad:
        print(f"  ! BAML 译文过长(限 {n} 字节): {en!r} -> {zh!r}")
    miss = [k for k in table if k not in hits]
    if miss:
        print(f"  · BAML 词典中 {len(miss)} 条未在文件里找到（可忽略）")
    return plan, bad


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("exe", nargs="?", default="Bloodborne Launcher.exe")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只预览）")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--restore", action="store_true", help="从备份还原 exe")
    ap.add_argument("--strings", default="strings.json")
    ap.add_argument("--todo", default="todo.json", help="导出待翻译清单")
    ap.add_argument("--min-len", type=int, default=0,
                    help="只翻译原文长度 ≥ 该值的条目（用于降级到保守方案）")
    ap.add_argument("--max-len", type=int, default=0,
                    help="只翻译原文长度 ≤ 该值的条目（压缩受限时优先砍掉长句）")
    ap.add_argument("--require-space", action="store_true",
                    help="只翻译含空格的条目（最保守，几乎不会碰到标识符）")
    ap.add_argument("--no-baml", action="store_true", help="跳过 XAML/BAML 层")
    ap.add_argument("--no-unsafe-filter", action="store_true",
                    help="不做标识符排除（危险，仅调试用）")
    args = ap.parse_args()

    exe, backup = args.exe, args.exe + BACKUP_SUFFIX

    if args.restore:
        if not os.path.exists(backup):
            print(f"找不到备份 {backup}")
            return 1
        shutil.copy2(backup, exe)
        print(f"已从备份还原：{exe}")
        return 0

    if not os.path.exists(exe):
        print(f"找不到 {exe}")
        return 1
    if not os.path.exists(args.strings):
        print(f"缺少 {args.strings}，请先运行 extract.py")
        return 1

    entries = json.load(open(args.strings, encoding="utf-8"))
    data = open(exe, "rb").read()
    print(f"目标 {exe}: {len(data):,} 字节")
    if is_localized(data):
        print("提示：这个 exe 已经是汉化版（检测到中文界面标记）。")
        print("      重复运行不会出错，但也不会有新变化。")
        print("      想完整重做一遍：先运行「还原英文.bat」，再运行「一键汉化.bat」。")
        print()

    bad = verify_entries(data, entries)
    if bad > max(5, len(entries) * 0.05):
        print(f"！清单与当前 exe 不匹配：{bad} / {len(entries)} 处校验失败。")
        print("  说明这个 exe 已经换成别的版本（例如启动器刚刚更新过），")
        print("  旧偏移继续写入会造成损坏，已中止。请先运行「一键汉化.bat」重新提取。")
        return 1

    print(f"字符串清单: {len(entries):,} 处（与 exe 校验通过）")
    print("载入中文词典：")
    table = load_dicts()

    # ---- 安全过滤：绝不翻译被代码当作标识符/键/枚举用的值 ----
    unsafe = set() if args.no_unsafe_filter else load_unsafe_values()
    if unsafe:
        hit = sorted(k for k in table if k in unsafe)
        print(f"  · 标识符排除：{len(hit)} 条不翻译"
              + (f"（如 {', '.join(repr(x) for x in hit[:6])} …）" if hit else ""))
        table = {k: v for k, v in table.items() if k not in unsafe}
    if args.min_len:
        n0 = len(table)
        table = {k: v for k, v in table.items() if len(k) >= args.min_len}
        print(f"  · 长度过滤：只保留原文 ≥{args.min_len} 字符的条目（{n0} -> {len(table)}）")
    if args.max_len:
        n0 = len(table)
        table = {k: v for k, v in table.items() if len(k) <= args.max_len}
        print(f"  · 长度过滤：只保留原文 ≤{args.max_len} 字符的条目（{n0} -> {len(table)}）")
    if args.require_space:
        n0 = len(table)
        table = {k: v for k, v in table.items() if " " in k}
        print(f"  · 保守模式：只保留含空格的条目（{n0} -> {len(table)}）")

    us_plan, overflow, missing = plan_us(entries, table)
    covered = {p[2] for p in us_plan}
    seen = {p[0] for p in us_plan}
    extra = [p for p in plan_direct(data, entries, table, covered) if p[0] not in seen]
    us_plan += extra
    for off, chars, src, dst in extra:
        missing.pop(src, None)
        covered.add(src)

    baml_table = load_baml_dict()
    if unsafe:
        baml_table = {k: v for k, v in baml_table.items() if k not in unsafe}
    if args.min_len:
        baml_table = {k: v for k, v in baml_table.items() if len(k) >= args.min_len}
    if args.max_len:
        baml_table = {k: v for k, v in baml_table.items() if len(k) <= args.max_len}
    if args.require_space:
        baml_table = {k: v for k, v in baml_table.items() if " " in k}
    baml_plan, baml_bad = ([], []) if args.no_baml else plan_baml(data, baml_table)

    tot_u = {e["text"] for e in entries}
    print()
    print(f"#US  可写入 {len(us_plan):,} 处（其中直搜补漏 {len(extra):,} 处）")
    print(f"BAML 可写入 {len(baml_plan):,} 处（词典 {len(baml_table)} 条）")

    if overflow:
        print(f"\n译文过长被拒 {len(overflow)} 条（需改短）：")
        for s, d, n in overflow[:25]:
            print(f"  [{n} 字] {s[:48]!r} -> {d!r} ({len(d)} 字)")
    if missing:
        print(f"\n尚未翻译 {len(missing)} 条唯一文本（示例）：")
        for s in list(missing)[:12]:
            print(f"  {s[:70]}")

    # 导出待翻译清单（按出现次数排序），方便下次启动器更新后补译
    todo = sorted(missing.items(), key=lambda kv: -kv[1])
    json.dump([{"text": t, "count": c, "zh": ""} for t, c in todo],
              open(args.todo, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n待翻译清单已导出 -> {args.todo}（{len(todo)} 条）")

    if not args.apply:
        print("\n（预览模式，未修改任何文件。加 --apply 写入。）")
        return 0

    if not args.no_backup and not is_localized(data):
        shutil.copy2(exe, backup)
        print(f"\n已备份原版（英文）文件 -> {backup}")

    buf = bytearray(data)
    touched = []          # (start, end)

    # 1) #US：写正确的长度前缀，短了就把余下字节清零（绝不再用 U+FEFF 补齐）
    n_us = 0
    us_skipped = []
    written = []            # (前缀起点, 原占用长度, 译文)
    for off, chars, src, dst in us_plan:
        rec, start, old_total = rewrite_us_record(buf, off, chars, dst)
        if rec is None:
            us_skipped.append(src)
            continue
        buf[start:start + old_total] = rec
        touched.append((start, start + old_total))
        written.append((start, old_total, dst))
        n_us += 1

    # 2) BAML：UTF-8，按字节数等长，不足补空格；跳过与 #US 重叠的位置
    n_baml = 0
    for off, nbytes, src, dst in baml_plan:
        end = off + 1 + nbytes
        if any(off < te and end > ts for ts, te in touched):
            continue
        payload = dst.encode("utf-8").ljust(nbytes, b" ")
        buf[off + 1:off + 1 + nbytes] = payload
        n_baml += 1

    out = bytes(buf)
    assert len(out) == len(data), "文件长度改变了！"

    # 3) 回读校验：每条写入的 #US 记录都必须原样读回，且不得残留 U+FEFF。
    #    U+FEFF 是历史上用的补齐字符，一旦它落进被当作 XML 名字/属性名的字符串，
    #    启动器就会抛 "cannot be included in a name"。
    bad = []
    for start, old_total, dst in written:
        v, plen = read_cint(buf, start)
        got = buf[start + plen:start + plen + v - 1].decode("utf-16-le", "replace")
        if got != dst or "\ufeff" in got:
            bad.append((dst, got))
    if bad:
        print(f"  ! 错误：{len(bad)} 条 #US 记录回读不一致，已放弃写入")
        for d, g in bad[:5]:
            print(f"    {d!r} 但读回 {g!r}")
        return 1

    open(exe, "wb").write(out)

    print(f"完成：写入 #US {n_us:,} 处 + BAML {n_baml:,} 处，文件长度未变。")
    if us_skipped:
        print(f"  跳过 {len(us_skipped)} 条（新记录放不进原位置）："
              + ", ".join(repr(s[:24]) for s in us_skipped[:5]))
    print("提示：可运行 apply.py --restore 还原英文。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
