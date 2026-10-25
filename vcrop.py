"""截图内容区检测与裁剪。

Windrecorder 每小时截一张全屏。显示器关着的时候，Windows 会给出一个
奇怪的虚拟桌面（实测 2400x675 的超宽扁条），窗口被挤在一角；显示器开着
的时候也常常是"半边窗口、半边桌面"。直接把这些图放进总账，用户看到的
是大片无效画面。

这里把"内容"和"壁纸"分开，只裁出内容那一块；原图不动，随时能回查。

判据是**灰度双峰度**，不是亮度也不是梯度 —— 这两样都分不开壁纸：
壁纸是照片，有山有天空有树林，亮度颜色都很丰富，被拉伸压缩后梯度也
不低。真正的区别在**黑白分明**：窗口里全是文字、图标、按钮，一个 40 像素
的小块里往往同时踩着黑字和白底，灰度直方图呈双峰；壁纸的灰度是连续的，
单峰。实测三张截图：内容区双峰度 16~30，壁纸区 2~3（正常分辨率下 15~17
对 21~29），一条阈值就能切开。

裁法：按 40 像素分块算双峰度，阈值取"均值减半个标准差"（壁纸块占多数，
均值被拉低，减半个标准差正好落在两个峰的中间），再做行/列投影取内容块
的外接矩形。投影只认成片的 —— 某行的内容块数不到最大值 1/5 就不算内容
行，专门剔掉壁纸里那几块高对比区域。
"""
from __future__ import annotations

from pathlib import Path

try:
    import numpy as np
except Exception:                                   # pragma: no cover
    np = None

try:
    from PIL import Image
except Exception:                                   # pragma: no cover
    Image = None

_BS = 40          # 分块边长，约等于一个图标/一行文字的高度


def _bimodal_grid(gray: np.ndarray, bs: int = _BS) -> np.ndarray:
    """每块算一个"双峰程度"：两个峰越分开、越高。"""
    H, W = gray.shape
    rows, cols = H // bs, W // bs
    if rows < 2 or cols < 2:
        return np.zeros((max(rows, 1), max(cols, 1)), dtype=np.float32)
    out = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            blk = gray[r * bs:(r + 1) * bs, c * bs:(c + 1) * bs]
            h, _ = np.histogram(blk, bins=32, range=(0, 255))
            h = h.astype(np.float32)
            h /= (h.sum() + 1e-6)
            main = int(np.argmax(h))
            if main < 16:                       # 主峰在暗侧，去亮侧找次峰
                sec = 16 + int(np.argmax(h[16:]))
            else:                               # 反之去暗侧找
                sec = int(np.argmax(h[:16]))
            out[r, c] = abs(sec - main) * (h[main] + h[sec])
    return out


def content_rect(img, bs: int = _BS, pad: float = 0.05):
    """内容外接矩形 (left, top, right, bottom)；判不出就返回 None。"""
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    H, W = gray.shape
    bm = _bimodal_grid(gray, bs)
    if bm.size == 0 or not np.isfinite(bm).all():
        return None

    thr = float(bm.mean() - 0.5 * bm.std())
    hot = bm > thr
    row_cnt = hot.sum(axis=1)
    col_cnt = hot.sum(axis=0)
    r_ok = np.where(row_cnt >= max(2, row_cnt.max() * 0.2))[0]
    c_ok = np.where(col_cnt >= max(2, col_cnt.max() * 0.2))[0]
    if r_ok.size == 0 or c_ok.size == 0:
        return None

    top, bot = int(r_ok[0]) * bs, min(H, (int(r_ok[-1]) + 1) * bs)
    left, right = int(c_ok[0]) * bs, min(W, (int(c_ok[-1]) + 1) * bs)

    # 四周留一点边，别把窗口边框切掉
    px, py = int((right - left) * pad), int((bot - top) * pad)
    left, top = max(0, left - px), max(0, top - py)
    right, bot = min(W, right + px), min(H, bot + py)

    # 裁完还剩大半张图，说明这张本来就没有明显空白，别裁
    if (right - left) * (bot - top) >= 0.85 * W * H:
        return None
    return left, top, right, bot


def crop_file(src: Path, dst: Path, max_w: int = 1400,
              quality: int = 82) -> tuple[bool, str]:
    """裁一张。返回 (是否真的裁过, 说明)。

    任何一步出错都老实退回原图 —— 总账里的图宁可大一点也不许没有。
    没装 numpy/PIL 也一样：当没裁过处理，链接照原图走。
    """
    if Image is None or np is None:
        return False, "没装 PIL/numpy"
    try:
        im = Image.open(src)
        im.load()
    except Exception as e:
        return False, f"打不开：{e}"

    rect = content_rect(im)
    if rect is None:
        return False, "没有明显空白，保留原图"

    try:
        l, t, r, b = rect
        out = im.crop((l, t, r, b))
        if out.width > max_w:            # 缩一下，免得看大图要滚屏
            out = out.resize((max_w, round(out.height * max_w / out.width)),
                             Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.convert("RGB").save(dst, "JPEG", quality=quality, optimize=True)
    except Exception as e:
        return False, f"裁失败：{e}"

    keep = (r - l) * (b - t) / (im.width * im.height) * 100
    return True, f"保留 {keep:.0f}%（{im.width}x{im.height} → {out.width}x{out.height}）"


if __name__ == "__main__":
    import sys

    for p in sys.argv[1:]:
        s = Path(p)
        d = s.with_name(s.stem + ".crop.jpg")
        did, why = crop_file(s, d)
        print(f"{s.name}: {'已裁' if did else '原图'}  {why}")
