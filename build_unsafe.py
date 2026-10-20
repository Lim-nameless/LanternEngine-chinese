#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 analyze.cs 输出的 strings_ctx.tsv 转成 unsafe-values.json。

analyze.cs 会为程序集里每个字符串字面量判定「使用上下文」：
    cmp   紧跟字符串比较 / 字典查找 / FindName / XML 名字 API —— 是控件名、设置键、枚举名，禁止翻译
    tbl   紧跟 stelem.ref —— 被塞进 string[] 之类的静态表，通常拿去匹配或当键用，禁止翻译
    show  紧跟 set_Text / MessageBox.Show 之类 —— 明确是显示文字
    other 其它
本脚本把 cmp / tbl 那些值汇总成黑名单，apply.py 会在汉化时整值跳过它们。

用法：
    python build_unsafe.py <strings_ctx.tsv> [输出 json]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 少数值虽然被代码拿去比较，但那是「同一个 #US 字面量在多处共用」造成的假阳性：
# 页面标题、引擎开关分组名、图标 switch 全指向同一条记录，一起变就一起仍然一致。
# 这几个是导航栏标签，不译会让界面中英混排，所以显式放行。
ALLOW = {
    "Graphics",   # GraphicsPage 标题 + EngineSwitches 分组名 + EnginePage 图标 switch
    "Online",     # OnlinePage 标题 + EngineSwitches 分组名 + Prompt.Tell 标题
    # 引擎页(EnginePage)的分组标题：同一批字面量既当分组名显示、又当
    # EngineSwitches 的字典键、又喂给 GroupIcon 挑图标。三处一起译就仍然自洽。
    "Performance",
    "Memory and VRAM",
    "Upscalers",
    "Input",
    "Sound",
    "Game fixes",
    "Diagnostics and logs",
    # 「导入设置」在菜单项、文件对话框标题、剪贴板内容判断里共用，同样自洽。
    "Import settings",
}

# 明确不能放行的例子（写在这里是为了后人别再踩）：
#   "Minimize"/"Maximize"/"Close"  和 BAML 里 Tag="Minimize" 成对比较，只改 #US 会让
#                                  标题栏三个按钮失效，必须两边都改，收益极低 → 不动。
#   "Frame stats: "/"Stall: "/"Memory: game "/"THD: ..."  用于 StartsWith 解析引擎日志，
#                                  日志是英文的，改了立刻解析失败 → 不动。
#   "Left Alt"/"Mouse2"/"Space"……  是 bbport.ini 里真实存的值，改了存档里的按键绑定读不回来。
#   "Lantern Engine"              可能参与进程名/目录名判断，只由 BAML 层译窗口标题。

# 内置补丁表的 15 个补丁名。它们在 IL 里只是被 .Add() 进集合，被 analyze.cs 判成 other，
# 但运行时 Patches.Discover 要拿它们和磁盘上 Bloodborne.xml 里的补丁**按名字合并**：
#   Key = "Bloodborne.xml/Skip Intro"，Name = "Skip Intro"
# XML 是英文的，名字一译就合不上，补丁列表会变成中英各一份（22 → 36）。
# 所以这 15 个必须保持英文（游戏内补丁名本来就来自引擎侧数据）。
KEEP_ENGLISH = {
    "Sprint Fix (High FPS)",
    "60FPS (no deltatime)",
    "Intel Black Tonemap Fix",
    "Intel 12th Gen+ SFX workaround",
    "Disable Chromatic Aberration",
    "Disable DoF",
    "Disable Motion Blur (perf increase)",
    "Disable SSAO",
    "Disable AA",
    "Disable Dynamic Light Shadows (perf increase)",
    "Enable Screen Space Reflections (READ NOTE)",
    "Skip Intro",
    "Restore Debug Camera",
    "Restore Debug Menu (READ NOTES)",
    "Increased Graphics Heap Sizes",
}


def main():
    tsv = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "_work", "strings_ctx.tsv")
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "unsafe-values.json")

    if not os.path.exists(tsv):
        print(f"找不到 {tsv}")
        print("请先运行 重建标识符清单.bat（需要 .NET SDK 的 dotnet 命令）。")
        return 1

    vals = set()
    stats = {"cmp": 0, "show": 0, "tbl": 0, "other": 0}
    for line in open(tsv, encoding="utf-8", errors="replace"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 4:
            continue
        ctx, val = p[0], p[3]
        stats[ctx] = stats.get(ctx, 0) + 1
        if ctx in ("cmp", "tbl") and val:
            vals.add(val)
    vals |= KEEP_ENGLISH

    obj = {
        "_comment": "由 analyze.cs 从 Bloodborne Launcher.dll 的 IL 中挖出的「标识符/键/枚举值」清单。"
                    "这些字符串被代码用于比较、查表或 FindName，翻译后启动器会找不到元素或解析失败。"
                    "apply.py 会整值排除它们。",
        "_source_tsv": os.path.basename(tsv),
        "_count": len(vals),
        "unsafe": sorted(vals),
        "allow": sorted(ALLOW & vals),
    }
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

    print(f"上下文统计：{stats}")
    print(f"已写出 {out}：{len(vals)} 条标识符")
    return 0


if __name__ == "__main__":
    sys.exit(main())
