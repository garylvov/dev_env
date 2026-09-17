#!/usr/bin/env python3
"""Render a patorjk.com TAAG URL to a color-preserving ANSI file.

Usage:  ./taag2ansi.py '<taag url>' [-o out.ans]
        ./taag2ansi.py --font DarkCS3DGree --text Minerva

Supports TheDraw (color) fonts -- ft=thedraw in the URL -- which is what
carries the color. Plain FIGlet fonts have no color to preserve.
"""
import argparse
import os
import re
import struct
import sys
import urllib.parse
import urllib.request

BASE = "https://patorjk.com/software/taag"
CACHE = os.path.expanduser("~/.cache/patorjk")
UA = "taag2ansi/1.0 (+https://github.com/garylvov/dev_env)"

CP437 = (
    '\x00☺☻♥♦♣♠•◘○◙♂♀♪♫☼'
    '►◄↕‼¶§▬↨↑↓→←∟↔▲▼'
    ' !"#$%&\'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`'
    'abcdefghijklmnopqrstuvwxyz{|}~⌂'
    'ÇüéâäàåçêëèïîìÄÅ'
    'ÉæÆôöòûùÿÖÜ¢£¥₧ƒ'
    'áíóúñÑªº¿⌐¬½¼¡«»'
    '░▒▓│┤╡╢╖╕╣║╗╝╜╛┐'
    '└┴┬├─┼╞╟╚╔╩╦╠═╬╧'
    '╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀'
    'αßΓπΣσµτΦΘΩδ∞φε∩'
    '≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ '
)
# CGA color index -> ANSI color offset. The two palettes order their colors
# differently, hence the shuffle.
CGA = [0, 4, 2, 6, 1, 5, 3, 7]
BLANK = (32, 0x07)

# The RGB values patorjk's page paints with. Emitting these as 24-bit color
# reproduces the site exactly, instead of letting the terminal theme decide what
# "bright green" means.
CGA_HEX = ["#000000", "#0000AA", "#00AA00", "#00AAAA",
           "#AA0000", "#AA00AA", "#AA5500", "#AAAAAA",
           "#555555", "#5555FF", "#55FF55", "#55FFFF",
           "#FF5555", "#FF55FF", "#FFFF55", "#FFFFFF"]
CGA_NAMES = ["black", "blue", "green", "cyan", "red", "magenta", "brown", "grey",
             "dark grey", "bright blue", "bright green", "bright cyan",
             "bright red", "bright magenta", "yellow", "white"]


def fetch(url, name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        # An honest, descriptive UA. A bare "Mozilla/5.0" gets a 406 from
        # patorjk's bot filter on some font-set files.
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            f.write(r.read())
    with open(path, "rb") as f:
        return f.read()


def chunk_for(font):
    """Find which tdf-font-set holds a font, following TAAG's hashed bundles."""
    html = fetch(BASE + "/", "taag.html").decode("utf-8", "replace")
    idx = re.search(r'src="(/software/taag/assets/index-[^"]+\.js)"', html).group(1)
    js = fetch("https://patorjk.com" + idx, os.path.basename(idx)).decode("utf-8", "replace")
    m = re.search(r'\./(tdfFontChunkMap-[\w-]+\.js)', js).group(1)
    cmap = fetch(BASE + "/assets/" + m, m).decode("utf-8", "replace")
    hit = re.search(r'["\']?' + re.escape(font) + r'["\']?\s*:\s*"([^"]+)"', cmap)
    if not hit:
        sys.exit(f"font not found in TheDraw registry: {font}")
    return hit.group(1)


def parse_fonts(data):
    fonts = {}
    for m in re.finditer(b"\x55\xaa\x00\xff", data):
        p = m.start() + 4
        name = data[p + 1:p + 1 + 12].decode("latin-1")[:data[p]]
        p += 17                                    # name block + 4 reserved
        ftype, spacing = data[p], data[p + 1]
        p += 4                                     # type, spacing, blocksize
        offsets = struct.unpack("<94H", data[p:p + 188])
        fonts[name] = {"type": ftype, "spacing": spacing, "base": p + 188,
                       "offsets": offsets, "data": data}
    return fonts


def glyph(f, ch):
    """Glyph rows as [(cp437 byte, cga attr), ...]. The width/height bytes in
    the header are advisory; the real extent comes from the row data."""
    i = ord(ch) - 33
    if not 0 <= i <= 93 or f["offsets"][i] == 0xFFFF:
        return None
    d, p = f["data"], f["base"] + f["offsets"][i] + 2
    rows, row = [], []
    while p < len(d):
        if d[p] == 0x00:
            if row:
                rows.append(row)
            break
        if d[p] == 0x0D:
            rows.append(row)
            row = []
            p += 1
            continue
        row.append((d[p], d[p + 1] if f["type"] == 2 else 0x0F))
        p += 2
    return rows


def render(f, text, space_width=1):
    glyphs = []
    for c in text:
        if c == " ":
            glyphs.append([])
            continue
        g = glyph(f, c)
        if g:                       # undefined glyphs contribute nothing at all
            glyphs.append(g)
    if not glyphs:
        return []
    height = max(len(g) for g in glyphs)
    if not height:
        return []

    gap = max(0, f["spacing"] - 1)  # the byte counts columns *including* the glyph
    canvas = [[] for _ in range(height)]
    for n, g in enumerate(glyphs):
        if n and gap:
            for row in canvas:
                row.extend([BLANK] * gap)
        if not g:                   # a space
            for row in canvas:
                row.extend([BLANK] * max(1, space_width * 4))
            continue
        w = max(len(r) for r in g)  # every row padded to the glyph's widest
        for i in range(height):
            r = g[i] if i < len(g) else []
            canvas[i].extend(r[j] if j < len(r) else BLANK for j in range(w))
    return canvas


def rgb(hex_color):
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def sgr(attr, palette=None, transparent=False):
    """Escape sequence for one CGA attribute byte.

    transparent leaves background slot 0 unpainted (SGR 49, "default
    background") so the terminal's own background shows through instead of a
    black rectangle."""
    fg, bg = attr & 15, (attr >> 4) & 7
    if palette:
        fr, fg_, fb = rgb(palette[fg])
        back = "49" if (transparent and bg == 0) else "48;2;%d;%d;%d" % rgb(palette[bg])
        return f"\x1b[0;38;2;{fr};{fg_};{fb};{back}m"
    # High-intensity foregrounds are 90-97, not bold + 30-37: bold makes some
    # terminals reach for a heavier font instead.
    code = 30 + CGA[fg] if fg < 8 else 90 + CGA[fg - 8]
    back = 49 if (transparent and bg == 0) else 40 + CGA[bg]
    return f"\x1b[0;{code};{back}m"


def to_ansi(canvas, palette=None, transparent=False):
    out = []
    for row in canvas:
        last, line = -1, []
        for ch, attr in row:
            if attr != last:
                line.append(sgr(attr, palette, transparent))
                last = attr
            line.append(CP437[ch])
        if last != -1:
            line.append("\x1b[0m")
        out.append("".join(line))
    return "\n".join(out) + "\n"


def show_palette(palette):
    """Print the 16 color slots, as the terminal renders them and as RGB."""
    lines = ["  idx  name             hex        terminal   truecolor"]
    for i, (name, hx) in enumerate(zip(CGA_NAMES, palette)):
        term = sgr(i, None) + "  \u2588\u2588\u2588  " + "\x1b[0m"
        true = sgr(i, palette) + "  \u2588\u2588\u2588  " + "\x1b[0m"
        lines.append(f"  {i:3d}  {name:15s}  {hx}   {term}   {true}")
    lines.append("")
    lines.append("  Slots 0-7 can be backgrounds; all 16 can be foregrounds.")
    return "\n".join(lines)


def from_url(url):
    frag = urllib.parse.urlparse(url).fragment or urllib.parse.urlparse(url).query
    q = urllib.parse.parse_qs(frag)
    font = q.get("f", ["Standard"])[0]
    text = q.get("t", ["Hello"])[0].replace("%0A", "\n")
    if q.get("ft", [""])[0] != "thedraw":
        sys.exit("not a TheDraw font (no ft=thedraw) -- no color to preserve")
    return font, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--font")
    ap.add_argument("--text")
    ap.add_argument("-o", "--out")
    ap.add_argument("--ansi16", action="store_true",
                    help="emit the terminal's 16 color slots instead of 24-bit "
                         "color; matches the website's own output byte for byte, "
                         "but the theme decides the actual shades")
    ap.add_argument("--opaque-bg", action="store_true",
                    help="paint background slot 0 black instead of letting the "
                         "terminal background show through")
    ap.add_argument("--truecolor", action="store_true",
                    help=argparse.SUPPRESS)          # now the default; kept so
    ap.add_argument("--transparent-bg", action="store_true",
                    help=argparse.SUPPRESS)          # old commands still run
    ap.add_argument("--palette",
                    help="comma-separated hex colors overriding the palette, "
                         "from slot 0; overrides --ansi16")
    ap.add_argument("--show-palette", action="store_true",
                    help="print the color slots and exit")
    a = ap.parse_args()

    # 24-bit color over a see-through background is the default: it is what the
    # website actually looks like, and it sits on the terminal's own background
    # rather than a black slab. --ansi16 / --opaque-bg opt back out.
    palette = list(CGA_HEX)
    if a.palette:
        custom = [c.strip() for c in a.palette.split(",") if c.strip()]
        for i, c in enumerate(custom[:16]):
            palette[i] = c if c.startswith("#") else "#" + c
        a.ansi16 = False
    if a.show_palette:
        print(show_palette(palette))
        return
    if not a.font and not a.url:
        ap.error("need a taag url, or --font/--text")
    font, text = (a.font, a.text) if a.font else from_url(a.url)
    chunk = chunk_for(font)
    data = fetch(f"{BASE}/tdf-font-sets/{chunk}", chunk)
    fonts = parse_fonts(data)
    if font not in fonts:
        sys.exit(f"font {font} not in set; available: {', '.join(sorted(fonts))}")
    ansi = to_ansi(render(fonts[font], text),
                   None if a.ansi16 else palette, not a.opaque_bg)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(ansi)
    else:
        sys.stdout.write(ansi)


if __name__ == "__main__":
    main()
