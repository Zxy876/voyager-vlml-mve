"""给面板截图加红框标注：红矩形 + 红色数字徽章 + 中文标签。

编号顺序 = README 第 19 章「测试复现指南」的叙述顺序：
  06 数据源(①-④) → 01 概览(⑤-⑦) → 02 运行中(⑧-⑪)
  → 03 轨迹(⑫) → 04 技能(⑬-⑭) → 05 最终(⑮-⑯-⑰)

坐标来源：2026-10-13 服务器实测，agent-browser eval getBoundingClientRect
（文档坐标；06 为从 01/05 整页裁剪的图，坐标已按裁剪偏移 (90,2700) 换算）。

用法：把**未标注**的整页截图放到本目录的 raw/ 子目录（文件名见下面对照），
跑 `python annotate_shots.py`，标注结果直接写回本目录（即入库的 6 张图）。
⚠️ 面板布局随内容变（题面长短会挪 hero 元素），换库/换题后坐标要重新取。

字体：Hiragino Sans GB —— `/System/Library/Fonts/PingFang.ttc` 在 macOS 上
**并不存在**，按它找不到字体会回退 Helvetica，中文全渲染成方块字（实测踩过）。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
RAW = HERE / "raw"          # 未标注原图放这里（不入库）
OUT = HERE                  # 标注结果写回本目录

FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FONT = ImageFont.truetype(FONT_PATH, 15)
FONT_BIG = ImageFont.truetype(FONT_PATH, 17)

RED = (220, 38, 38)
WHITE = (255, 255, 255)


def annotate(src: str, dst: str, boxes: list) -> None:
    """boxes: (编号, x, y, w, h, 标签)"""
    img = Image.open(f"{RAW}/{src}").convert("RGB")
    draw = ImageDraw.Draw(img)
    W, H = img.size
    for num, x, y, w, h, label in boxes:
        w = min(w, W - x - 2)
        h = min(h, H - y - 2)
        # 红框（粗 3px）
        for i in range(3):
            draw.rectangle([x - i, y - i, x + w + i, y + h + i], outline=RED)
        # 数字徽章：红圆 + 白字，贴在框左上角
        r = 15
        cx, cy = x, y
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RED)
        tw = draw.textlength(str(num), font=FONT_BIG)
        draw.text((cx - tw / 2, cy - 11), str(num), font=FONT_BIG, fill=WHITE)
        # 标签：红底白字小条，贴在徽章右侧
        lw = draw.textlength(label, font=FONT) + 14
        lh = 24
        ly = cy - lh / 2
        lx = cx + r + 6
        if lx + lw > W - 4:                    # 右边放不下就放框内左上
            lx = max(4, x + 6)
        draw.rectangle([lx, ly, lx + lw, ly + lh], fill=RED)
        draw.text((lx + 7, ly + 3), label, font=FONT, fill=WHITE)
    img.save(f"{OUT}/{dst}")
    print(f"  {dst}: {len(boxes)} 个红框")


print("标注中…")
# ①-④ 数据源与数据库（先确认连的库，再学会切换）
annotate("raw-06-datasource.png", "06-datasource.png", [
    (1, 31, 110, 1198, 17, "① 当前库：vlml_rib.duckdb（默认库）"),
    (2, 31, 169, 894, 32, "② 载入新的数据库（填绝对路径）"),
    (3, 935, 169, 61, 39, "③ 验货"),
    (4, 1006, 169, 129, 39, "④ 切换并重启进程"),
])
# ⑤-⑦ 概览：数据源就绪 → 启动 → 导出
annotate("raw-01-overview.png", "01-overview.png", [
    (5, 1183, 209, 132, 60, "⑤ 数据源就绪"),
    (6, 179, 155, 133, 39, "⑥ 启动 Voyager"),
    (7, 409, 154, 104, 41, "⑦ 导出 md"),
])
# ⑧-⑪ 运行中：tab/停止/心跳/日志
annotate("raw-02-runlog-running.png", "02-runlog-running.png", [
    (8, 286, 488, 165, 68, "⑧ 运行日志 · 运行中"),
    (9, 321, 155, 79, 39, "⑨ 停止"),
    (10, 100, 585, 1240, 129, "⑩ 循环心跳"),
    (11, 121, 901, 1198, 460, "⑪ 循环日志：配方复用 · 验题换题 · 逐轮战报"),
])
# ⑫ 轨迹
annotate("raw-03-trace.png", "03-trace.png", [
    (12, 121, 780, 1198, 2156, "⑫ 逐轮轨迹（轮次明细）"),
])
# ⑬-⑭ 技能
annotate("raw-04-skills.png", "04-skills.png", [
    (13, 100, 585, 1240, 3317, "⑬ 技能库（41 条 SQL 配方）"),
    (14, 100, 3918, 1240, 116, "⑭ 行动因果时间线"),
])
# ⑮-⑰ 最终战果（标签只写元素名，数值以 pill 里可见的为准）
annotate("raw-05-overview-final.png", "05-overview-final.png", [
    (15, 763, 140, 132, 60, "⑮ 当前题判据"),
    (16, 1043, 141, 132, 59, "⑯ 累计轮次"),
    (17, 1183, 141, 132, 59, "⑰ 技能库"),
])
print("完成")
