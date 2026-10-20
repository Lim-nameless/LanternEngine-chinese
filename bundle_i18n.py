#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bloodborne Launcher 汉化工具 v2 —— 适配 .NET 单文件“压缩包”版启动器

为什么需要 v2
-------------
v0.9.32 及以前：程序集原样放进单文件包，界面文字以 UTF-16 明文躺在 exe 里，
              在 exe 里做等长替换就行（extract.py + apply.py 的老流程）。

v0.9.34 起：启动器改用单文件压缩（EnableCompressionInSingleFile），
           应用程序集被 raw Deflate 压缩后放进包内 —— exe 里再也搜不到任何
           界面文字，老流程会报「#US 可写入 0 处」。

本脚本怎么绕过
--------------
1. 解析 exe 尾部的 .NET 单文件清单（bundle manifest），找到应用程序集条目
       条目格式：[偏移8][原始大小8][压缩大小8][类型1][名字长度1][名字]
2. 解压该程序集 —— 得到一个普通的 Bloodborne Launcher.dll
3. 复用 extract.py / apply.py 在 dll 上做等长中文替换
       #US  ：UTF-16，按字符数等长
       BAML ：UTF-8，按字节数等长
   同时自动排除被代码当作标识符/键/枚举值的字符串（见 apply.py 的说明）。
4. 重新压缩后写回原来的字节区间，并**把清单里这条的 compressedSize 改成新流长**。
       —— 这一步是必须的。.NET 单文件宿主严格要求压缩流精确占满 compressedSize，
          多补一个字节就会 BadImageFormatException(0x8007000B)。
5. 写盘后真启动一遍，检查启动器有没有写 crash.log：
       通过 → 完成
       不通过 → 自动回滚英文，换更保守的一档重来
       全部不通过 → 还原英文，保证启动器一定能用

结果：exe 的**文件长度不变**，只有那一段压缩字节 + 清单里 8 个字节被换掉。
      风险最小，且随时可一键还原。


如果某个版本的启动器又改回“原样存放”（不压缩），本脚本同样能处理 —— 直接
在 exe 里等长替换，无需重新压缩。

用法
----
    python bundle_i18n.py --check              # 只看结构（压缩？大小？是否已汉化）
    python bundle_i18n.py --dry-run            # 全流程演练，但不写 exe
    python bundle_i18n.py --apply              # 真正写入（自动备份）
    python bundle_i18n.py --restore            # 从备份还原英文
"""
import argparse
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 输出始终走 UTF-8：控制台代码页是 936(cp936) 时，✔ ✗ 这类符号编不出来会直接崩。
# 配合 .bat 里的 chcp 65001，中文与符号都能正常显示。
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_vendored_libs():
    """加载 _libs 下内置的 zopfli（abi3 轮子，Python 3.10+ 通用）。

    这样新电脑上**什么都不用装**：既不需要 Python 装 zopfli，也不需要联网。
    3.9 及更早用另一份专用轮子。
    """
    try:
        import zopfli  # noqa: F401
        return "已安装"
    except Exception:
        pass
    ver = sys.version_info[:2]
    cand = ["py39"] if ver <= (3, 9) else ["py3"]
    for d in cand:
        p = os.path.join(HERE, "_libs", d)
        if os.path.isdir(p):
            sys.path.insert(0, p)
            try:
                import zopfli  # noqa: F401
                return f"内置（_libs/{d}）"
            except Exception:
                sys.path.remove(p)
    return None


ZOPFLI_SRC = _load_vendored_libs()

BACKUP_SUFFIX = ".zh-CN-backup"
WORK_DIR = os.path.join(HERE, "_work")

# 应用程序集条目：类型 1 = Assembly，类型 2 = NativeBinary，
# 3 = deps.json，4 = runtimeconfig.json
TYPE_ASSEMBLY = 1

# 清单扫描窗口（从文件尾向前找清单起点）
SCAN_WINDOW = 250_000

MARKER = ("游戏文件夹".encode("utf-16-le"), "游戏文件夹".encode("utf-8"))


# ---------------------------------------------------------------- 工具
def human(n):
    return f"{n:,}"


def step(i, total, msg):
    print(f"\n[{i}/{total}] {msg}")


# ---------------------------------------------------------------- 单文件包
def parse_bundle(data):
    """解析 .NET 单文件清单。

    清单总是紧贴文件末尾：从某个起点开始，一条条记录首尾相接，正好走到 EOF。
    因此可以暴力试起点 —— 绝大多数候选会在第一条就解析失败，速度足够快。

    返回 (清单起点, [ (偏移, 原始大小, 压缩大小, 类型, 名字), ... ])
    """
    n = len(data)

    def try_parse(start):
        pos, ents = start, []
        while pos < n:
            if pos + 26 > n:
                return None
            off, size, csz = struct.unpack_from("<qqq", data, pos)
            typ = data[pos + 24]
            nlen = data[pos + 25]
            if typ > 6 or nlen == 0:
                return None
            name = data[pos + 26:pos + 26 + nlen]
            if len(name) != nlen or not all(32 <= b < 127 for b in name):
                return None
            if not (0 <= off <= n and 0 <= size <= n and 0 <= csz <= n):
                return None
            ents.append((off, size, csz, typ, name.decode("ascii")))
            pos += 26 + nlen
        return ents if pos == n else None

    lo = max(0, n - SCAN_WINDOW)
    for start in range(lo, n - 16):
        ents = try_parse(start)
        if ents and len(ents) >= 8:
            return start, ents
    return None, None


def is_compressed(entry):
    off, size, csz, typ, name = entry
    return bool(csz) and csz != size


def inflate_entry(blob, size):
    """raw Deflate 解压，要求恰好得到 size 字节。"""
    raw = zlib.decompressobj(-15).decompress(blob, size + 8)
    return raw


def find_app_entry(ents, stem):
    """定位应用程序集条目（Bloodborne Launcher.exe -> Bloodborne Launcher.dll）。"""
    want = stem + ".dll"
    for e in ents:
        if e[3] == TYPE_ASSEMBLY and e[4] == want:
            return e
    # 兜底：名字带 exe 主名的托管程序集
    for e in ents:
        if e[3] == TYPE_ASSEMBLY and want.lower() in e[4].lower():
            return e
    return None


def entry_record_pos(man, ents, name):
    """返回某个条目在清单里的记录位置（用于原地改写该条目的 compressedsize）。"""
    p = man
    for e in ents:
        if e[4] == name:
            return p
        p += 26 + len(e[4])
    return None


# ------------------------------------------------- 为什么必须改写 compressedsize
# 实测（v0.9.34-beta.3，.NET 10 宿主）：
#   把新的 deflate 流塞回原区间、后面补 0 —— 宿主会抛
#       System.BadImageFormatException: 试图加载格式不正确的程序。(0x8007000B)
#   补「合法的空 stored 块」（00 00 00 FF FF）也一样失败。
# 结论：宿主会把 compressedsize 指定的**整段**喂给解压器，并要求流**恰好**用完
#       这段输入；多出来的任何字节都被视为损坏。
# 由于清单里各条目的 offset 是绝对偏移、清单本身也不移动，所以正确做法是：
#       把这条的 compressedsize 改写成新压缩流的真实长度。
# 这样 exe 只有「压缩数据区 + 清单里 8 个字节」发生变化，其余字节原样不动。


# ---------------------------------------------------------------- 压缩
def deflate_raw(data, iterations=None):
    """用可用的最好压缩器生成 raw Deflate 流。

    上游用的是 zlib-ng，比 Python 自带的 zlib 稍强（同一份数据差约 1.8 KB，
    而这 1.8 KB 正好会让替换失败）。所以这里优先用 zopfli —— 它比 zlib-ng
    还小 1% 左右，天然留出余量。
    """
    import zopfli.zlib as zz
    if iterations:
        try:
            blob = zz.compress(data, iterations=iterations)
        except TypeError:
            blob = zz.compress(data)
    else:
        blob = zz.compress(data)
    return blob[2:-4]          # 去掉 zlib 的 2 字节头 + 4 字节 Adler32


def deflate_ok(data, csz, first=None):
    """压缩到能塞进 csz 就返回流，否则返回 None。"""
    if ZOPFLI_SRC is None:
        return None
    for it in ([first] if first else []) + [None, 60, 200]:
        stream = deflate_raw(data, it)
        if len(stream) <= csz:
            return stream
    return None


# ---------------------------------------------------------------- 子步骤
def run_script(script, args, cwd):
    """调用 extract.py / apply.py（用当前解释器）。

    显式指定 UTF-8 输出，避免子进程按系统代码页(cp936)打印中文导致乱码。
    """
    exe = sys.executable or "python"
    cmd = [exe, os.path.join(HERE, script)] + list(args)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    if out.strip():
        print("    " + out.strip().replace("\n", "\n    "))
    if p.returncode != 0:
        print("    " + err.strip().replace("\n", "\n    "))
    return p.returncode


def make_partial_dicts(max_len):
    """生成“只汉化较短文本”的降级词典目录。

    压缩超限时用得上：长句（帮助文字）占的压缩体积最多，砍掉它们能让压缩流
    变小；短标签/按钮是界面骨架，优先保留。
    """
    d = os.path.join(WORK_DIR, "partial")
    os.makedirs(d, exist_ok=True)
    kept = dropped = 0
    for f in glob.glob(os.path.join(HERE, "zh-CN*.json")) + [os.path.join(HERE, "baml-zh-CN.json")]:
        try:
            obj = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        new = {}
        for k, v in obj.items():
            if k.startswith("_") or not isinstance(v, str):
                new[k] = v
            elif len(k) <= max_len:
                new[k] = v
                kept += 1
            else:
                dropped += 1
        json.dump(new, open(os.path.join(d, os.path.basename(f)), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    return d, kept, dropped


def patch_assembly(asm_bytes, tag, dict_dir=None, extra_args=()):
    """把程序集交给 extract.py + apply.py 做等长汉化，返回补丁后的字节。"""
    dict_dir = dict_dir or HERE
    os.makedirs(WORK_DIR, exist_ok=True)
    target = os.path.join(WORK_DIR, f"{tag}.dll")
    strings = os.path.join(WORK_DIR, f"{tag}-strings.json")
    todo = os.path.join(WORK_DIR, f"{tag}-todo.json")
    with open(target, "wb") as f:
        f.write(asm_bytes)

    for old in glob.glob(target + BACKUP_SUFFIX):
        os.remove(old)

    rc = run_script("extract.py", [target, "-o", strings,
                                   "--baml-out", os.path.join(WORK_DIR, f"{tag}-baml.json")],
                    cwd=WORK_DIR)
    if rc != 0 or not os.path.exists(strings):
        print("    ! extract.py 未产出清单")
        return None

    # apply.py 从「工作目录」读取 zh-CN*.json 词典
    rc = run_script("apply.py", [target, "--apply", "--strings", strings, "--todo", todo]
                    + list(extra_args), cwd=dict_dir)
    if rc != 0:
        return None

    with open(target, "rb") as f:
        return f.read()


# ---------------------------------------------------------------- 试运行验证
def crash_log_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "bbport-launcher", "crash.log")


def _proc_pids(name_kw):
    """用 tasklist 找进程名匹配的 pid 集合。"""
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, encoding="mbcs", errors="replace", timeout=20)
    except Exception:
        return set()
    pids = set()
    for line in r.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and name_kw.lower() in parts[0].lower():
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def _windows_of(pids):
    """列出这些进程的可见窗口：[(标题, 宽, 高)]。纯 ctypes，不依赖第三方库。"""
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    out = []

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, lp):
        if not u.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1) if n else ctypes.create_unicode_buffer(1)
        if n:
            u.GetWindowTextW(hwnd, buf, n + 1)
        rc = RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rc))
        out.append((buf.value, rc.right - rc.left, rc.bottom - rc.top))
        return True

    try:
        u.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


# 错误对话框只有 ~700x330；正常主界面是 1500x1150 这一档。
MIN_OK_W, MIN_OK_H = 900, 600


def probe_launch(exe, seconds=9):
    """把汉化后的 exe 真跑一遍，确认它不只是「进程没死」，而是真的把主界面画出来了。

    两路信号：
      1) crash.log —— 启动器自己捕获到异常就会往这里写（最可靠）
      2) 窗口尺寸 —— 错误对话框很小；如果只出现小窗口，说明弹的是报错框
    返回 (是否正常, 说明)
    """
    crash = crash_log_path()
    try:
        os.remove(crash)
    except OSError:
        pass

    name = os.path.basename(exe)
    try:
        p = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, f"无法启动：{e}"

    pids = set()
    best = (0, 0, "")
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(1.0)
        if not pids:
            pids = _proc_pids(name) or ({p.pid} if p.poll() is None else set())
        for title, w, h in _windows_of(pids):
            if w * h > best[0] * best[1]:
                best = (w, h, title)

    alive = p.poll() is None
    try:
        p.kill()
        p.wait(timeout=10)
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True, timeout=15)
    except Exception:
        pass

    if os.path.exists(crash):
        try:
            txt = open(crash, encoding="utf-8", errors="replace").read()
        except OSError:
            return False, "启动器写入了 crash.log"
        keys = [l.strip() for l in txt.splitlines()
                if "Exception" in l or l.strip().startswith("--->")]
        return False, "启动器报错：" + (" / ".join(keys[:2]) if keys else "见 crash.log")

    w, h, title = best
    if w == 0:
        return (True, "没有捕获到窗口（可能启动较慢），但也没有崩溃日志") if alive else \
               (False, "进程启动后立即退出")
    if w < MIN_OK_W or h < MIN_OK_H:
        return False, f"只出现了小窗口 {w}x{h}「{title}」，看起来是错误对话框"

    return True, f"主界面已正常显示（{w}x{h}「{title}」），{seconds} 秒内无崩溃"


def allowed_spans(orig):
    """原始程序集里“允许被改动”的字节区间：#US 字面量 + .g.resources 资源流。

    #US 记录的实际范围是 [前缀起点, 数据尾+尾字节]，而 scan 给的 off 指向字符串
    数据本身，所以前后各放宽几个字节：前 4（压缩长度前缀最长 4 字节）、后 1（尾字节）。
    写入时缩短的字符串会把余下字节清零，但不会超出这条原记录的边界。
    """
    spans = []
    try:
        import extract as ex
        for off, text in ex.scan(orig):
            spans.append((max(0, off - 4), off + len(text) * 2 + 1))
        for r in ex.scan_baml(orig):
            spans.append((r["offset"], r["offset"] + 1 + r["bytes"]))
    except Exception as e:
        print(f"    · 区间自检不可用（{e}），跳过")
        return None
    spans.sort()
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def check_diff_inside(orig, patched, spans):
    """确认所有差异字节都落在允许区间内。返回 (差异总数, 越界数, 首个越界偏移)。"""
    if spans is None:
        return 0, 0, None
    n = min(len(orig), len(patched))
    total = 0
    outside = 0
    first = None
    idx = 0
    for i in range(n):
        if orig[i] != patched[i]:
            total += 1
            while idx < len(spans) and i >= spans[idx][1]:
                idx += 1
            if not (idx < len(spans) and spans[idx][0] <= i < spans[idx][1]):
                outside += 1
                if first is None:
                    first = i
    return total, outside, first


# ---------------------------------------------------------------- 主流程
def do_check(data, stem):
    man, ents = parse_bundle(data)
    if ents is None:
        print("！没能解析出 .NET 单文件清单。这个 exe 可能不是单文件包。")
        return 1
    print(f"  文件大小      : {human(len(data))} 字节")
    print(f"  清单起点      : {human(man)}（{len(ents)} 个条目）")
    app = find_app_entry(ents, stem)
    if app is None:
        print("！清单里找不到应用程序集条目。")
        return 1
    off, size, csz, typ, name = app
    print(f"  应用程序集    : {name}")
    print(f"    偏移        : {human(off)}")
    print(f"    原始大小    : {human(size)}")
    print(f"    压缩后大小  : {human(csz) if csz else '（未压缩，原样存放）'}")
    print(f"    存放方式    : {'raw Deflate 压缩' if is_compressed(app) else '原样存放'}")
    try:
        blob = data[off:off + (csz or size)]
        asm = inflate_entry(blob, size) if is_compressed(app) else blob
        print(f"    解压校验    : {'✔ ' + human(len(asm)) + ' 字节，MZ 头正常' if asm[:2] == b'MZ' else '✗ 异常'}")
    except Exception as e:
        print(f"    解压校验    : ✗ {e}")
        return 1
    loc = any(m in asm for m in MARKER)
    print(f"  汉化状态      : {'已汉化（中文界面标记存在）' if loc else '英文原版'}")
    return 0


def do_apply(args):
    exe = os.path.abspath(args.exe)
    stem = os.path.splitext(os.path.basename(exe))[0]
    backup = exe + BACKUP_SUFFIX

    if not os.path.exists(exe):
        print(f"找不到 {exe}")
        return 1

    print(f"目标：{exe}")
    print(f"      {human(os.path.getsize(exe))} 字节")

    # 前置检查：被占用时给出明确提示，而不是中途炸掉
    try:
        with open(exe, "r+b"):
            pass
    except PermissionError:
        print("\n！这个 exe 正被占用，无法写入。")
        print("  最常见的原因：启动器还开着。请完全关闭启动器（任务栏也检查一下），再运行一次。")
        return 1

    data = open(exe, "rb").read()

    step(1, 5, "解析 .NET 单文件清单")
    man, ents = parse_bundle(data)
    if ents is None:
        print("！没能解析出单文件清单，本工具无法处理这个 exe。")
        return 1
    app = find_app_entry(ents, stem)
    if app is None:
        print("！清单里找不到应用程序集条目。")
        return 1
    off, size, csz, typ, name = app
    compressed = is_compressed(app)
    print(f"    清单起点 {human(man)}，共 {len(ents)} 个条目")
    print(f"    应用程序集 {name}：偏移 {human(off)}，原始 {human(size)}，"
          f"压缩后 {human(csz) if csz else '未压缩'}（{'压缩存放' if compressed else '原样存放'}）")

    step(2, 5, "解压应用程序集")
    blob = data[off:off + (csz or size)]
    try:
        asm = inflate_entry(blob, size) if compressed else blob
    except Exception as e:
        print(f"！解压失败：{e}")
        return 1
    if len(asm) != size or asm[:2] != b"MZ":
        print(f"！解压结果异常：{human(len(asm))} 字节，头 {asm[:4].hex()}")
        return 1
    print(f"    ✔ 得到 {human(len(asm))} 字节的普通 .NET 程序集")

    if any(m in asm for m in MARKER):
        print("\n    这个启动器已经是汉化版了（检测到中文界面标记）。")
        print("    想重新汉化一遍：先运行「还原英文.bat」回到英文版，再运行「一键汉化.bat」。")
        if not args.force:
            return 2

    step(3, 5, "等长汉化（复用 extract.py + apply.py）")
    if compressed and ZOPFLI_SRC is None:
        print("！找不到 zopfli 压缩库 —— 而这版启动器把程序集压缩存放，")
        print("  必须重新压缩才能汉化。请确认 _i18n\\_libs\\py3\\zopfli\\ 存在。")
        return 1
    if ZOPFLI_SRC:
        print(f"    压缩库：zopfli（{ZOPFLI_SRC}）")

    # 分级方案：从「完整」逐级退到「最保守」，每级都要能压回原占位。
    # 启动器更新后如果引入了新的同形标识符，靠 --verify 的试运行能发现并自动降级。
    levels = [("完整（#US + XAML，已排除标识符）", [])]
    levels.append(("保守：只翻原文≥8 字符的条目", ["--min-len", "8"]))
    levels.append(("更保守：只翻含空格的整句", ["--require-space"]))
    levels.append(("最小：只翻含空格的整句、不动 XAML", ["--require-space", "--no-baml"]))
    if args.partial:
        for lim in (200, 120, 80, 60, 40, 32, 24):
            levels.append((f"压缩受限：仅长度≤{lim}字的文本",
                           ["--max-len", str(lim), "--no-baml"]))

    def build_level(extra, tag):
        """生成 (补丁后程序集, 压缩流)。压缩塞不下返回 None。"""
        p = patch_assembly(asm, tag, HERE, extra)
        if p is None:
            return None
        if len(p) != len(asm):
            print(f"！程序集长度变了（{human(len(asm))} -> {human(len(p))}），中止。")
            return None
        if not compressed:
            return p, None
        s = deflate_ok(p, csz, args.iterations)
        return (p, s) if s is not None else None

    # ---- 逐级尝试：构建 → 校验 → 落盘 → 真跑一遍 ----
    # 任何一级失败都会自动回滚到英文，然后换更保守的一级重来。
    # 全部失败时最后一步就是「还原英文」，因此绝不会留下一个打不开的启动器。
    if not args.no_backup and not os.path.exists(backup):
        shutil.copy2(exe, backup)
        print(f"    ✔ 已备份英文原版 -> {os.path.basename(backup)}")
    if not os.path.exists(backup):
        print("！没有英文备份可用，无法在验证失败时回滚，已中止。")
        return 1

    def commit(payload):
        tmp = exe + ".tmp-zh"
        with open(tmp, "wb") as f:
            f.write(payload)
        try:
            os.replace(tmp, exe)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    last_msg = ""
    for idx, (label, extra) in enumerate(levels, 1):
        print(f"\n  · 方案 {idx}/{len(levels)}：{label}")
        r = build_level(extra, stem)
        if r is None:
            print("      压缩塞不下 / 词典未命中，跳过这一档")
            continue
        patched, stream = r

        total, outside, first = check_diff_inside(asm, patched, allowed_spans(asm))
        if total == 0:
            print("      没有任何改动，跳过这一档")
            continue
        print(f"      程序集内改动 {human(total)} 字节")
        if outside:
            pct = outside / total * 100
            print(f"      其中 {human(outside)} 字节（{pct:.1f}%）在文本区间之外"
                  f"{f'，首个 @{human(first)}' if first is not None else ''}")
            if pct > 1.0 and not args.force:
                print("      越界比例过高，怀疑误伤，跳过这一档")
                continue

        # 组装新的 exe
        buf = bytearray(data)
        if compressed:
            if len(stream) > csz:
                print("      压缩流超出原占位，跳过这一档")
                continue
            buf[off:off + csz] = stream + b"\x00" * (csz - len(stream))
            pos = entry_record_pos(man, ents, name)
            if pos is None:
                print("！找不到清单里该条目的记录位置，中止。")
                return 1
            # 关键：宿主要求压缩流精确占满 compressedSize，必须同步改写
            struct.pack_into("<q", buf, pos + 16, len(stream))
            print(f"      压缩 {human(len(stream))} 字节（原占位 {human(csz)}），"
                  f"清单长度已同步改写")
        else:
            buf[off:off + size] = patched
        out = bytes(buf)
        if len(out) != len(data):
            print("！文件长度改变了，中止。")
            return 1

        # 回读校验
        man2, ents2 = parse_bundle(out)
        if ents2 is None or len(ents2) != len(ents):
            print("      回读校验失败：清单解析不一致，跳过这一档")
            continue
        app2 = find_app_entry(ents2, stem)
        o2, s2, c2, t2, n2 = app2
        asm2 = inflate_entry(out[o2:o2 + (c2 or s2)], s2) if is_compressed(app2) else out[o2:o2 + s2]
        if asm2 != patched:
            print("      回读校验失败：解压结果与预期不一致，跳过这一档")
            continue
        bad = [e[4] for e, a in zip(ents, ents2) if e[4] != n2 and e != a]
        if bad:
            print(f"      回读校验失败：{bad[:3]} 等条目被动过，跳过这一档")
            continue
        print("      ✔ 结构校验通过（清单可解析、程序集一致、其它条目未动）")

        if args.dry_run:
            print("\n（演练模式，未写盘。去掉 --dry-run 即真正写入。）")
            return 0

        commit(out)

        if not args.verify:
            print(f"\n完成（方案：{label}）。exe 长度未变（{human(len(out))} 字节）。")
            print("打开启动器即可看到简体中文界面。")
            print("想还原英文：运行「还原英文.bat」（当然，必须先把启动器关掉）。")
            return 0

        print("      [验证] 真实启动一遍 ...")
        ok, msg = probe_launch(exe, args.verify_seconds)
        if ok:
            print(f"      ✔ 通过 —— {msg}")
            print(f"\n完成（方案：{label}）。exe 长度未变（{human(len(out))} 字节）。")
            print("打开启动器即可看到简体中文界面。")
            print("想还原英文：运行「还原英文.bat」（当然，必须先把启动器关掉）。")
            return 0

        last_msg = msg
        print(f"      ✗ 未通过 —— {msg}")
        print("      这次碰到了启动器内部的标识符，自动回滚并换更保守的方案 ...")
        shutil.copy2(backup, exe)

    # ---- 所有档位都没通过 ----
    print("\n" + "=" * 62)
    print("所有汉化方案都没能通过启动验证，已把启动器还原为英文原版。")
    print(f"最后一次失败原因：{last_msg}")
    print("启动器本身可以正常使用，只是界面为英文。")
    print("如果希望继续汉化，请把 _i18n\\_work 下的 todo.json 和本提示一起反馈。")
    print("=" * 62)
    try:
        shutil.copy2(backup, exe)
    except OSError:
        pass
    return 3


def do_restore(args):
    exe = os.path.abspath(args.exe)
    backup = exe + BACKUP_SUFFIX
    if not os.path.exists(backup):
        print(f"找不到备份：{backup}")
        return 1
    try:
        with open(exe, "r+b"):
            pass
    except PermissionError:
        print("！exe 正被占用（启动器还开着）。请先完全关闭启动器，再重试。")
        return 1
    shutil.copy2(backup, exe)
    print(f"已用备份还原：{exe}")
    print(f"（备份文件本身保留在 {os.path.basename(backup)}，可反复还原）")
    return 0


def find_launcher(explicit=None):
    """定位要汉化的启动器 exe。

    作者改过好几次名字（Bloodborne Launcher.exe → Lantern Engine.exe），
    所以默认不写死文件名：先按已知名字找，再退一步挑根目录里最大的那个
    「不像游戏本体」的 exe。这样作者下次再改名也不用改脚本。
    """
    if explicit:
        return os.path.abspath(explicit)
    root = os.path.dirname(HERE)          # 游戏包根目录（_i18n 的上一级）
    for name in ("Lantern Engine.exe", "Bloodborne Launcher.exe"):
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p
    best = None
    for f in glob.glob(os.path.join(root, "*.exe")):
        base = os.path.basename(f).lower()
        if base.startswith("bloodborne.exe") or base.endswith(".zh-cn-backup"):
            continue
        size = os.path.getsize(f)
        if size < 5_000_000:              # 启动器是几十 MB 的单文件包，太小的是别的东西
            continue
        if best is None or size > os.path.getsize(best):
            best = f
    return best


def main():
    ap = argparse.ArgumentParser(description="Bloodborne Launcher / Lantern Engine 汉化工具 v2")
    ap.add_argument("exe", nargs="?", default=None,
                    help="启动器 exe 路径；省略则自动在上级目录里找")
    ap.add_argument("--check", action="store_true", help="只检查结构，不写文件")
    ap.add_argument("--apply", action="store_true", help="写入汉化")
    ap.add_argument("--dry-run", action="store_true", help="演练全流程但不写盘")
    ap.add_argument("--restore", action="store_true", help="从备份还原英文")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--force", action="store_true", help="越界/无改动时也继续")
    ap.add_argument("--partial", action="store_true", help="压缩超限时允许降级部分汉化")
    ap.add_argument("--iterations", type=int, default=None, help="zopfli 迭代次数")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--verify", dest="verify", action="store_true", default=True,
                   help="（默认开启）写盘后真启动一遍，不通过就自动回滚并降级")
    g.add_argument("--no-verify", dest="verify", action="store_false",
                   help="跳过试运行验证（不推荐）")
    ap.add_argument("--verify-seconds", type=int, default=9,
                    help="试运行时等待多少秒判断是否崩溃（默认 9）")
    ap.add_argument("--extract-asm", default=None,
                    help="只把应用程序集解包到指定路径（配合 analyze.cs 重建标识符清单）")
    args = ap.parse_args()

    args.exe = find_launcher(args.exe)
    if not args.exe:
        print("！在上层目录里找不到启动器 exe。")
        print("  请把本目录放回游戏包根目录（和启动器同级），或在命令行里直接给出路径。")
        return 1

    if args.restore:
        return do_restore(args)
    if not os.path.exists(args.exe):
        print(f"找不到 {args.exe}")
        return 1
    data = open(args.exe, "rb").read()
    stem = os.path.splitext(os.path.basename(args.exe))[0]
    if args.check:
        print(f"检查 {args.exe}")
        return do_check(data, stem)
    if args.extract_asm:
        man, ents = parse_bundle(data)
        if ents is None:
            print("！清单解析失败")
            return 1
        app = find_app_entry(ents, stem)
        if app is None:
            print("！找不到应用程序集条目")
            return 1
        off, size, csz, _t, name = app
        asm = inflate_entry(data[off:off + (csz or size)], size) if is_compressed(app) else data[off:off + size]
        out = os.path.abspath(args.extract_asm)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(asm)
        print(f"已解包 {name}（{human(len(asm))} 字节）-> {out}")
        return 0
    if not (args.apply or args.dry_run):
        print("请指定动作：--check / --dry-run / --apply / --restore")
        return 1
    return do_apply(args)


if __name__ == "__main__":
    sys.exit(main())
