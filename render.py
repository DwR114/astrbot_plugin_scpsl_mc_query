"""本地出图（Pillow）：不依赖 AstrBot 的网络文转图服务，也不需要浏览器。"""
import io
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

PLUGIN_DIR = Path(__file__).resolve().parent
_FONT_CACHE: dict = {}

_SYSTEM_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-{w}.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-{w}.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-{w}.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-microhei/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc" if "{w}" == "Bold" else "C:/Windows/Fonts/msyh.ttc",
]


def _font_candidates(bold: bool, data_dir: Path):
    w = "Bold" if bold else "Regular"
    yield from sorted((Path(data_dir) / "fonts").glob("*.[ot]t[fc]")) if (Path(data_dir) / "fonts").is_dir() else []
    yield PLUGIN_DIR / "fonts" / f"NotoSansSC-{w}.subset.otf"
    for p in _SYSTEM_FONTS:
        yield Path(p.replace("{w}", w))


def get_font(size: int, bold: bool = False, data_dir: Path = PLUGIN_DIR / "data"):
    key = (size, bold, str(data_dir))
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    for p in _font_candidates(bold, data_dir):
        try:
            if p.exists():
                font = ImageFont.truetype(str(p), size)
                break
        except OSError:
            continue
    if font is None:
        raise RuntimeError("找不到可用的中文字体，请在 WebUI 或数据目录 fonts/ 放入 .ttf/.otf 字体")
    _FONT_CACHE[key] = font
    return font


_TAG_RE = re.compile(r"<[^>]{0,40}>")


def clean_name(s: str) -> str:
    """SCP:SL 服务器名常带 <color=..> 富文本标签，去掉。"""
    return _TAG_RE.sub("", s or "").strip()


def _fit(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _default_bg(w, h) -> Image.Image:
    """没有上传背景时的内置渐变背景。"""
    top, bot = (24, 32, 58), (10, 12, 22)
    bg = Image.new("RGB", (w, h))
    px = ImageDraw.Draw(bg)
    for y in range(h):
        t = y / max(1, h - 1)
        px.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([w * 0.45, -h * 0.25, w * 1.2, h * 0.5], fill=(40, 90, 160))
    glow = glow.filter(ImageFilter.GaussianBlur(max(w, h) // 6))
    return Image.composite(glow, bg, Image.new("L", (w, h), 70))


def _cover(img: Image.Image, w, h) -> Image.Image:
    r = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * r)), max(h, round(img.height * r))
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return img.crop((l, t, l + w, t + h))


def _rounded_mask(w, h, r) -> Image.Image:
    k = 4
    m = Image.new("L", (w * k, h * k), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * k - 1, h * k - 1], r * k, fill=255)
    return m.resize((w, h), Image.LANCZOS)


CARD_W, CARD_H, GAP, MARGIN = 360, 230, 24, 48
HEADER_H, FOOTER_H = 146, 70
GREEN, RED, WHITE, GREY = (74, 222, 128), (248, 113, 113), (255, 255, 255), (200, 206, 220)


def render_report(results: list, background: Image.Image = None, title: str = None,
                  data_dir: Path = PLUGIN_DIR / "data") -> bytes:
    """results 为 query_all 的返回值。返回 PNG 字节。"""
    if title is None:
        kinds = {r.get("kind") == "mc" for r in results}
        title = "Minecraft 服务器查询" if kinds == {True} else "已配置服务器查询"
    n = max(1, len(results))
    cols = min(n, 3)
    card_h = CARD_H + (26 if any(r.get("version") for r in results) else 0)
    rows = (n + cols - 1) // cols
    W = max(640, MARGIN * 2 + cols * CARD_W + (cols - 1) * GAP)
    H = HEADER_H + rows * card_h + (rows - 1) * GAP + FOOTER_H + MARGIN // 2

    bg = _cover(background, W, H) if background is not None else _default_bg(W, H)
    bg = bg.convert("RGB")
    blurred = bg.filter(ImageFilter.GaussianBlur(16))
    canvas = Image.blend(bg, Image.new("RGB", (W, H), (0, 0, 0)), 0.38)  # RGB：配合 Draw(..., "RGBA") 才能正确半透明混合

    f_title = get_font(44, True, data_dir)
    f_sub = get_font(22, False, data_dir)
    f_name = get_font(26, True, data_dir)
    f_big = get_font(78, True, data_dir)
    f_max = get_font(34, False, data_dir)
    f_small = get_font(20, False, data_dir)

    d = ImageDraw.Draw(canvas, "RGBA")
    d.text((MARGIN, 30), title, font=f_title, fill=WHITE)
    total = sum(r.get("players", 0) for r in results if r.get("ok"))
    online = sum(1 for r in results if r.get("ok"))
    d.text((MARGIN, 100), f"更新于 {time.strftime('%Y-%m-%d %H:%M:%S')}", font=f_sub, fill=GREY)

    grid_w = cols * CARD_W + (cols - 1) * GAP
    x0 = (W - grid_w) // 2
    mask = _rounded_mask(CARD_W, card_h, 22)
    for i, r in enumerate(results):
        cx = x0 + (i % cols) * (CARD_W + GAP)
        cy = HEADER_H + (i // cols) * (card_h + GAP)
        # 毛玻璃：背景模糊 + 半透明深色层
        glass = blurred.crop((cx, cy, cx + CARD_W, cy + card_h)).convert("RGBA")
        glass = Image.alpha_composite(glass, Image.new("RGBA", (CARD_W, card_h), (8, 10, 18, 120)))
        canvas.paste(glass, (cx, cy), mask)
        d = ImageDraw.Draw(canvas, "RGBA")
        d.rounded_rectangle([cx, cy, cx + CARD_W - 1, cy + card_h - 1], 22, outline=(255, 255, 255, 70), width=2)

        ok = r.get("ok")
        color = GREEN if ok else RED
        pad = 24
        name = _fit(d, r["name"], f_name, CARD_W - pad * 2 - 34)
        d.ellipse([cx + pad, cy + 28, cx + pad + 16, cy + 44], fill=color)
        d.text((cx + pad + 28, cy + 20), name, font=f_name, fill=WHITE)

        if ok:
            p, m = str(r["players"]), f"/{r['max_players']}"
            d.text((cx + pad, cy + 62), p, font=f_big, fill=WHITE)
            pw = d.textlength(p, font=f_big)
            d.text((cx + pad + pw + 4, cy + 112), m, font=f_max, fill=GREY)
            ratio = min(1.0, r["players"] / r["max_players"]) if r["max_players"] else 0
            bx, by, bw = cx + pad, cy + 166, CARD_W - pad * 2
            d.rounded_rectangle([bx, by, bx + bw, by + 8], 4, fill=(255, 255, 255, 50))
            if ratio > 0:
                d.rounded_rectangle([bx, by, bx + max(8, int(bw * ratio)), by + 8], 4, fill=color)
            lat = f"  ·  {r['latency_ms']}ms" if r.get("latency_ms") is not None else ""
            addr = _fit(d, f"{r.get('addr') or (r['host'] + ':' + str(r['port']))}{lat}", f_small, CARD_W - pad * 2)
            d.text((cx + pad, cy + 186), addr, font=f_small, fill=GREY)
            if r.get("version"):
                d.text((cx + pad, cy + 212), _fit(d, "版本 " + r["version"], f_small, CARD_W - pad * 2),
                       font=f_small, fill=(147, 197, 253))
        else:
            d.text((cx + pad, cy + 70), "离线", font=get_font(56, True, data_dir), fill=RED)
            d.text((cx + pad, cy + 140), _fit(d, r.get("error", "查询失败"), f_sub, CARD_W - pad * 2),
                   font=f_sub, fill=GREY)
            d.text((cx + pad, cy + 186), _fit(d, r.get("addr") or f"{r['host']}:{r['port']}", f_small, CARD_W - pad * 2),
                   font=f_small, fill=GREY)

    foot = f"在线服务器 {online}/{len(results)}  ·  总在线人数 {total}"
    d = ImageDraw.Draw(canvas, "RGBA")
    fw = d.textlength(foot, font=f_sub)
    d.text(((W - fw) / 2, H - FOOTER_H + 6), foot, font=f_sub, fill=GREY)

    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


def render_jpeg(png_bytes: bytes, quality: int = 85) -> bytes:
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
