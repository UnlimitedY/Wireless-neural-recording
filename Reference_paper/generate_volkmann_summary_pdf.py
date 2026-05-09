from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import numpy as np
from matplotlib import pyplot as plt
from matplotlib import patches
from matplotlib import font_manager
from matplotlib.font_manager import FontProperties


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PDF = BASE_DIR / "volkmann_2025_circadian_lfp_summary_cn.pdf"
OUTPUT_PNG = BASE_DIR / "volkmann_2025_circadian_lfp_summary_cn.png"

TITLE = "昼夜时钟完整性决定脑区内外 LFP 节律的规则性"
SUBTITLE = (
    "Volkmann et al., Molecular Psychiatry (2025) | "
    "Integrity of the circadian clock determines regularity of high-frequency and diurnal LFP rhythms within and between brain areas"
)

SUMMARY_TEXT = (
    "Volkmann 等同时记录小鼠视交叉上核（SCN）和伏隔核（NAc）的局部场电位，比较野生型与 "
    "Cry1/2 缺失鼠在光暗循环（LD）和持续黑暗（DD）下的神经活动。结果显示，完整的内源性昼夜钟"
    "与外界明暗线索共同维持脑电活动的多尺度秩序：在 WT+LD 条件下，LFP 及其频段具有最清晰的 "
    "24 小时节律、最少的碎片化、最低的熵和最高的跨个体一致性；去掉任一时间线索后，节律幅度下降、"
    "相位更分散。相反，在 Cry1/2 缺失尤其 DD 条件下，SCN 与 NAc 的 24 小时节律几乎消失，时间序列"
    "更碎片化、长期自相关更差、规则性下降。关键的是，时钟受损并不意味着活动完全不同步，而是出现"
    "“同一时刻更同步、长期更不稳定”的异常状态：局部及跨脑区活动在瞬时上更容易一起波动，却失去"
    "健康脑网络应有的相位编排与长期可预测性。"
)

FOOTNOTE = (
    "图为根据原文结果绘制的中文示意图，不按原始数据比例重绘。"
    "原文 DOI: 10.1038/s41380-024-02795-z"
)

FONT_CANDIDATES = [
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]

COLORS = {
    "bg": "#f5f1e8",
    "ink": "#14202b",
    "muted": "#5d6c79",
    "panel": "#fcfbf8",
    "panel_edge": "#d2c8b7",
    "day": "#f2d783",
    "night": "#23364d",
    "wt": "#0f766e",
    "ko": "#c4493a",
    "accent": "#c78a28",
    "grid": "#d6d9dc",
    "bar_bg": "#e5e1d8",
}


def configure_font():
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            font_manager.fontManager.addfont(str(path))
            name = FontProperties(fname=str(path)).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return FontProperties(fname=str(path))
    plt.rcParams["axes.unicode_minus"] = False
    return None


def wrap_text(text, width):
    lines = []
    current = ""
    for char in text:
        current += char
        if len(current) >= width and char in "，。、；：,. ":
            lines.append(current.strip())
            current = ""
    if current.strip():
        lines.append(current.strip())
    return "\n".join(lines)


def signal_shape(kind):
    x = np.linspace(0, 24, 500)
    if kind == "wt_ld":
        y = (
            0.65 * np.sin(2 * np.pi * (x - 14.8) / 24)
            + 0.12 * np.sin(2 * np.pi * 3.5 * x / 24 + 0.4)
            + 0.05 * np.cos(2 * np.pi * x / 6)
        )
    elif kind == "wt_dd":
        y = (
            0.42 * np.sin(2 * np.pi * (x - 14.0) / 24)
            + 0.10 * np.sin(2 * np.pi * 4.1 * x / 24 + 1.0)
            + 0.09 * np.sin(2 * np.pi * x / 14)
        )
    elif kind == "ko_ld":
        y = (
            0.24 * np.sin(2 * np.pi * (x - 14.5) / 24)
            + 0.20 * np.sin(2 * np.pi * 5.4 * x / 24 + 0.7)
            + 0.12 * np.sign(np.sin(2 * np.pi * 2.7 * x / 24))
            - 0.04 * np.cos(2 * np.pi * x / 4.5)
        )
    else:
        y = (
            0.06 * np.sin(2 * np.pi * (x - 14.0) / 24)
            + 0.25 * np.sin(2 * np.pi * 7.5 * x / 24 + 0.6)
            + 0.17 * np.sign(np.sin(2 * np.pi * 3.2 * x / 24))
            - 0.08 * np.cos(2 * np.pi * x / 3.8)
        )
    return x, y


def add_card(ax, x0, y0, w, h, title, subtitle, kind, color, metrics, bullets):
    card = patches.FancyBboxPatch(
        (x0, y0),
        w,
        h,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.3,
        edgecolor=COLORS["panel_edge"],
        facecolor=COLORS["panel"],
    )
    ax.add_patch(card)

    ax.text(x0 + 0.03 * w, y0 + 0.90 * h, title, fontsize=14, fontweight="bold", color=COLORS["ink"])
    ax.text(x0 + 0.03 * w, y0 + 0.83 * h, subtitle, fontsize=9.8, color=COLORS["muted"])

    strip_x = x0 + 0.03 * w
    strip_y = y0 + 0.69 * h
    strip_w = 0.52 * w
    strip_h = 0.09 * h
    ax.add_patch(patches.Rectangle((strip_x, strip_y), strip_w / 2, strip_h, color=COLORS["day"], lw=0))
    ax.add_patch(patches.Rectangle((strip_x + strip_w / 2, strip_y), strip_w / 2, strip_h, color=COLORS["night"], lw=0))
    ax.text(strip_x + strip_w * 0.25, strip_y + strip_h / 2, "光照 / 主观白天", ha="center", va="center", fontsize=8.5, color="#4d3700")
    ax.text(strip_x + strip_w * 0.75, strip_y + strip_h / 2, "黑暗 / 主观黑夜", ha="center", va="center", fontsize=8.5, color="#f7f1df")

    trace_x = x0 + 0.03 * w
    trace_y = y0 + 0.38 * h
    trace_w = 0.52 * w
    trace_h = 0.25 * h
    ax.add_patch(
        patches.FancyBboxPatch(
            (trace_x, trace_y),
            trace_w,
            trace_h,
            boxstyle="round,pad=0.004,rounding_size=0.012",
            linewidth=0.8,
            edgecolor="#d9d3c6",
            facecolor="#fbfaf6",
        )
    )
    for frac in (0.0, 0.5, 1.0):
        xx = trace_x + trace_w * frac
        ax.plot([xx, xx], [trace_y, trace_y + trace_h], color=COLORS["grid"], lw=0.8, zorder=1)
    ax.plot([trace_x, trace_x + trace_w], [trace_y + trace_h / 2, trace_y + trace_h / 2], color="#cfd3d6", lw=0.8, zorder=1)
    ax.text(trace_x, trace_y - 0.028 * h, "ZT0", fontsize=7.5, color=COLORS["muted"])
    ax.text(trace_x + trace_w * 0.48, trace_y - 0.028 * h, "ZT12", fontsize=7.5, color=COLORS["muted"])
    ax.text(trace_x + trace_w * 0.94, trace_y - 0.028 * h, "ZT24", fontsize=7.5, color=COLORS["muted"])

    xs, ys = signal_shape(kind)
    xs = trace_x + trace_w * xs / 24
    ys = trace_y + trace_h * (0.50 + 0.37 * ys)
    ax.fill_between(xs, trace_y + trace_h / 2, ys, color=color, alpha=0.14, zorder=2)
    ax.plot(xs, ys, color=color, lw=2.3, zorder=3)

    metric_x = x0 + 0.61 * w
    metric_y = y0 + 0.67 * h
    bar_w = 0.31 * w
    bar_h = 0.035 * h
    labels = ["24h 节律", "长期规则性", "瞬时同步性"]
    for i, (label, value) in enumerate(zip(labels, metrics)):
        yy = metric_y - i * 0.13 * h
        ax.text(metric_x, yy + 0.043 * h, label, fontsize=8.8, color=COLORS["ink"])
        ax.add_patch(
            patches.FancyBboxPatch(
                (metric_x, yy),
                bar_w,
                bar_h,
                boxstyle="round,pad=0.004,rounding_size=0.01",
                linewidth=0,
                facecolor=COLORS["bar_bg"],
            )
        )
        ax.add_patch(
            patches.FancyBboxPatch(
                (metric_x, yy),
                bar_w * value,
                bar_h,
                boxstyle="round,pad=0.004,rounding_size=0.01",
                linewidth=0,
                facecolor=color,
            )
        )

    bullet_y = y0 + 0.26 * h
    for idx, line in enumerate(bullets):
        ax.text(x0 + 0.03 * w, bullet_y - idx * 0.085 * h, f"• {line}", fontsize=9.2, color=COLORS["ink"])


def draw_footer_callout(ax):
    x0, y0, w, h = 0.08, 0.225, 0.84, 0.07
    box = patches.FancyBboxPatch(
        (x0, y0),
        w,
        h,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.2,
        edgecolor="#d2b57d",
        facecolor="#fbf6ea",
    )
    ax.add_patch(box)
    ax.text(x0 + 0.02, y0 + 0.043, "核心一句话", fontsize=12.2, fontweight="bold", color=COLORS["accent"])
    ax.text(
        x0 + 0.18,
        y0 + 0.043,
        "完整时钟带来的不是“所有脑区同时起伏”，而是跨时间尺度的有序协同；",
        fontsize=10.8,
        color=COLORS["ink"],
    )
    ax.text(
        x0 + 0.18,
        y0 + 0.016,
        "Cry1/2 缺失则表现为短时过同步、长期不规则。",
        fontsize=10.8,
        color=COLORS["ink"],
    )


def build_page():
    font_prop = configure_font()
    fig = plt.figure(figsize=(8.27, 11.69), facecolor=COLORS["bg"])
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    ax.text(0.07, 0.955, TITLE, fontsize=23, fontweight="bold", color=COLORS["ink"], va="top")
    ax.text(0.07, 0.926, wrap_text(SUBTITLE, 68), fontsize=9.8, color=COLORS["muted"], va="top")

    ax.text(0.07, 0.865, "图解摘要", fontsize=16, fontweight="bold", color=COLORS["accent"])
    ax.text(
        0.17,
        0.865,
        "比较 WT 与 Cry1/2 缺失小鼠在 LD / DD 下的 SCN-NAc LFP 组织方式",
        fontsize=11,
        color=COLORS["muted"],
        va="center",
    )

    add_card(
        ax,
        0.07,
        0.58,
        0.40,
        0.23,
        "WT + LD",
        "内源时钟完整 + 外界明暗线索完整",
        "wt_ld",
        COLORS["wt"],
        [0.98, 0.94, 0.48],
        [
            "24h 节律最强，昼夜分界最清晰",
            "碎片化最低，时间序列最平滑",
            "SCN 对 NAc 的预测性最稳定",
        ],
    )
    add_card(
        ax,
        0.53,
        0.58,
        0.40,
        0.23,
        "WT + DD",
        "只保留内源时钟，没有外界光暗提示",
        "wt_dd",
        COLORS["wt"],
        [0.67, 0.72, 0.52],
        [
            "仍保留节律，但幅度下降",
            "相位更分散，个体差异增大",
            "长期稳定性弱于 WT+LD",
        ],
    )
    add_card(
        ax,
        0.07,
        0.33,
        0.40,
        0.23,
        "Cry1/2-/- + LD",
        "内源时钟受损，只剩外界光暗牵引",
        "ko_ld",
        COLORS["ko"],
        [0.44, 0.34, 0.77],
        [
            "光照可部分带出昼夜模式",
            "块状结构变碎，规律性下降",
            "同一时刻跨区同步偏高",
        ],
    )
    add_card(
        ax,
        0.53,
        0.33,
        0.40,
        0.23,
        "Cry1/2-/- + DD",
        "内源与外界时序线索都缺失",
        "ko_dd",
        COLORS["ko"],
        [0.10, 0.16, 0.92],
        [
            "几乎没有稳定的 24h 节律",
            "碎片化与熵最高，长期最不稳",
            "短时过同步最明显",
        ],
    )

    draw_footer_callout(ax)

    ax.text(0.07, 0.195, "一段话总结", fontsize=16, fontweight="bold", color=COLORS["accent"])
    summary_box = patches.FancyBboxPatch(
        (0.07, 0.055),
        0.86,
        0.12,
        boxstyle="round,pad=0.012,rounding_size=0.02",
        linewidth=1.0,
        edgecolor=COLORS["panel_edge"],
        facecolor=COLORS["panel"],
    )
    ax.add_patch(summary_box)
    ax.text(
        0.09,
        0.157,
        wrap_text(SUMMARY_TEXT, 46),
        fontsize=10.9,
        color=COLORS["ink"],
        va="top",
        linespacing=1.55,
    )

    ax.text(0.07, 0.03, FOOTNOTE, fontsize=8.8, color=COLORS["muted"])

    if font_prop is not None:
        for text in ax.texts:
            text.set_fontproperties(font_prop)

    fig.savefig(OUTPUT_PNG, dpi=220, facecolor=fig.get_facecolor(), bbox_inches="tight")
    fig.savefig(OUTPUT_PDF, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    build_page()
