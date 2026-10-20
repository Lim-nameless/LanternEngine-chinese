#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_g.resources 解析：精确定位 WPF 的各个 .baml 数据块。

为什么需要
----------
早期版本用「从第一个 RES_MAGIC 起扫 62 万字节」的粗暴窗口来框 BAML。
启动器换版后，资源流里塞进了 backdrop.jpg / discord-logo.png / 三个 .ico，
真正的 BAML 被挤到 173 万字节之后，窗口完全够不着 —— 结果一条都写不进去
（侧边导航栏 Play/Display/Graphics/… 始终是英文）。

现在改成真正解析资源流的名字表，拿到每个资源的偏移，再两两相减得到
BAML 块的精确范围。无论作者以后往资源里塞多少图片，BAML 都能被找到。

名字表格式（实测，与 System.Resources v2 的 UTF-8 变体不同）
-----------------------------------------------------------
    [单字节长度 = 名字的字节数][UTF-16LE 名字][int32 数据偏移]
    偏移相对「数据区起点」= 名字表结束处
"""
import struct

RES_MAGIC = b"\xce\xca\xef\xbe"      # .NET .resources 魔数
_ZERO_RUN = 64                        # 判定 BAML 块结束用的零字节阈值


def _try_record(data, i):
    """把 i 当成名字表的一条记录来解析。成功返回 (名字, 数据偏移, 记录结束位置)。"""
    n = len(data)
    if i + 1 >= n:
        return None
    blen = data[i]
    if blen < 3 or blen % 2 or i + 1 + blen + 4 > n:
        return None
    raw = data[i + 1:i + 1 + blen]
    try:
        name = raw.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    if not name or any(not (32 <= ord(c) < 127) for c in name):
        return None
    off, = struct.unpack_from("<i", data, i + 1 + blen)
    if off < 0 or off > n:
        return None
    return name, off, i + 1 + blen + 4


def find_name_table(data, magic_at):
    """在资源流头部附近找出名字表，返回 [(名字, 数据偏移), ...] 或 None。

    名字表的每条记录首尾相接，所以只要某个起点能连出 3 条以上就认定命中。
    """
    limit = min(len(data), magic_at + 8192)
    for start in range(magic_at + 12, limit):
        rec = _try_record(data, start)
        if not rec:
            continue
        entries, cur = [], start
        while True:
            rec = _try_record(data, cur)
            if not rec:
                break
            entries.append(rec)
            cur = rec[2]
        if len(entries) >= 3:
            return [(nm, off) for nm, off, _ in entries], cur
    return None, None


def _last_end(data, start):
    """最后一个资源没有「下一条偏移」可参照，用零填充判定它的结束。"""
    n = len(data)
    limit = min(n, start + 65536)
    probe = data.find(b"\x00" * _ZERO_RUN, start + 256, limit)
    if probe >= 0:
        # 回退到这段连续 0 的真正起点
        while probe > start and data[probe - 1] == 0:
            probe -= 1
        if probe - start >= 64:
            return probe
    return limit


def baml_blobs(data):
    """返回 [(资源名, 起点, 结束)]，只含 .baml 条目。

    解析失败时退回旧行为（从资源流魔数起 62 万字节），保证老版本仍能用。
    """
    magic_at = data.find(RES_MAGIC)
    if magic_at < 0:
        return []
    entries, table_end = find_name_table(data, magic_at)
    if not entries:
        return [("(fallback)", magic_at, min(len(data), magic_at + 620000))]

    ordered = sorted({off for _, off in entries})
    blobs = []
    for name, off in entries:
        if not name.lower().endswith(".baml"):
            continue
        start = table_end + off
        if start >= len(data):
            continue
        nxt = [o for o in ordered if o > off]
        end = table_end + nxt[0] if nxt else _last_end(data, start)
        blobs.append((name, start, min(end, len(data))))
    return blobs


def scan_strings(data):
    """扫描所有 BAML 块里的 [单字节长度][UTF-8] 字符串记录。

    返回 [{"offset", "bytes", "text", "blob"}]，offset 指向长度字节。
    """
    import re
    pat = re.compile(rb"[A-Za-z][A-Za-z0-9 &/:_\-\.\?\!\+']*")
    out, seen = [], set()
    for name, start, end in baml_blobs(data):
        i = start
        while i < end:
            blen = data[i]
            rec_end = i + 1 + blen
            if 2 <= blen <= 80 and rec_end + 1 <= end:
                seg = data[i + 1:rec_end]
                if all(0x20 <= b <= 0x7E for b in seg) and pat.fullmatch(seg):
                    if i not in seen:
                        seen.add(i)
                        out.append({"offset": i, "bytes": blen,
                                    "text": seg.decode("ascii"), "blob": name})
                    i = rec_end
                    continue
            i += 1
    return out
