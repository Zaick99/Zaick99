#!/usr/bin/env python3
"""Rigenera la grafica del profilo e le card di tutte le repo pubbliche.

Lo lancia la GitHub Action (.github/workflows/papere.yml) ogni giorno e a ogni push;
in locale basta `python scripts/papere.py`. Scrive gli SVG in assets/ e aggiorna
il blocco del README compreso tra i marcatori PAPERE:START e PAPERE:END.
"""
import base64
import io
import json
import math
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from PIL import Image

USER = os.environ.get('PAPERE_USER', 'Zaick99')
HIDDEN = {USER.lower()}          # la repo del profilo non è una papera
MAX_REPOS = int(os.environ.get('PAPERE_MAX', '30'))
ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'assets'
HERE = ROOT / 'scripts'
RAW = f'https://raw.githubusercontent.com/{USER}'
REPO_RAW = f'{RAW}/{USER}/main/assets'

INK, BG, BAND, PANEL, PANEL2, LINE = '#0d0d0f', '#141417', '#0b0b0d', '#1c1c20', '#232328', '#2e2e35'
BONE, TEXT, MUTED, YELLOW, RED = '#f4efe4', '#e6e7e9', '#9a9aa3', '#ffa800', '#d3242c'

FONTS = {
    'display': (HERE / 'fonts/archivo.ttf', 'ZD'),
    'mono': (HERE / 'fonts/plexmono600.ttf', 'ZM'),
    'body': (HERE / 'fonts/inter400.ttf', 'ZB'),
    'bold': (HERE / 'fonts/inter600.ttf', 'ZS'),
}
_tt = {k: TTFont(v[0]) for k, v in FONTS.items()}


# ───────────── rete ─────────────

def fetch(url, api=False):
    headers = {'User-Agent': 'papere-bot'}
    if api:
        headers['Accept'] = 'application/vnd.github+json'
        if os.environ.get('GITHUB_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError):
        return None


def api_json(path):
    body = fetch('https://api.github.com' + path, api=True)
    return json.loads(body) if body else None


# ───────────── testo e font ─────────────

def width(text, font, size, spacing=0):
    f = _tt[font]
    cmap, hm, upm = f.getBestCmap(), f['hmtx'], f['head'].unitsPerEm
    w = sum(hm[cmap.get(ord(c), cmap.get(32))][0] for c in text)
    return w * size / upm + spacing * max(0, len(text) - 1)


def wrap(text, font, size, maxw):
    lines, cur = [], ''
    for word in text.split():
        t = (cur + ' ' + word).strip()
        if width(t, font, size) <= maxw:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit(text, font, size, maxw):
    """Accorcia con … finché il testo non entra in maxw."""
    if width(text, font, size) <= maxw:
        return text
    while text and width(text + '…', font, size) > maxw:
        text = text[:-1]
    return text.rstrip() + '…'


def esc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def png_uri(im, jpeg=False, q=84):
    bio = io.BytesIO()
    if jpeg:
        im.convert('RGB').save(bio, 'JPEG', quality=q, optimize=True)
        return 'data:image/jpeg;base64,' + base64.b64encode(bio.getvalue()).decode()
    im.save(bio, 'PNG', optimize=True)
    return 'data:image/png;base64,' + base64.b64encode(bio.getvalue()).decode()


# ───────────── sprite: teschi del file vettoriale + papera di DuckNote ─────────────

SKULLS = json.loads((HERE / 'skulls.json').read_text())
SK_DIMS = []
_sym = []
for i, s in enumerate(SKULLS, 1):
    w, h = [float(v) for v in s['viewBox'].split()[2:]]
    SK_DIMS.append((w, h))
    holes = ''.join(f'<path d="{d}"/>' for d in s['holes'])
    _sym.append(f'<symbol id="sk{i}" viewBox="{s["viewBox"]}"><path d="{s["outline"]}" fill="#fff" stroke="{INK}" '
                f'stroke-width="5" stroke-linejoin="round"/><g fill="{INK}" stroke="{INK}" stroke-linejoin="round">{holes}</g></symbol>')


def load_duck():
    raw = fetch(f'{RAW}/DuckNote/main/assets/duck.ico')
    im = Image.open(io.BytesIO(raw))
    im.size = max(im.info.get('sizes', {im.size}))
    im.load()
    im = im.convert('RGBA')
    im = im.crop(im.getbbox())
    return im.quantize(64, method=Image.Quantize.FASTOCTREE)


DUCK = load_duck()
DK_DIMS = DUCK.size
_duck_uri = png_uri(DUCK)
_sym.append(f'<symbol id="dk" viewBox="0 0 {DK_DIMS[0]} {DK_DIMS[1]}"><image width="{DK_DIMS[0]}" height="{DK_DIMS[1]}" '
            f'href="{_duck_uri}" xlink:href="{_duck_uri}"/></symbol>')
SPRITES = ''.join(_sym)


class Doc:
    """Un SVG con i soli glifi usati incorporati, così su GitHub si vede identico ovunque."""

    def __init__(self, w, h, label, sprites=True):
        self.w, self.h, self.label, self.sprites = w, h, label, sprites
        self.body, self.used = [], {}

    def text(self, x, y, s, font, size, fill, anchor='start', spacing=0, extra=''):
        self.used.setdefault(font, set()).update(s)
        ls = f' letter-spacing="{spacing}"' if spacing else ''
        self.body.append(f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONTS[font][1]}, sans-serif" font-size="{size}" '
                         f'fill="{fill}" text-anchor="{anchor}"{ls}{extra}>{esc(s)}</text>')

    def add(self, s):
        self.body.append(s)

    def render(self, defs=''):
        faces = []
        for font, chars in sorted(self.used.items()):
            opts = subset.Options()
            opts.flavor = 'woff2'
            f = TTFont(FONTS[font][0])
            sub = subset.Subsetter(opts)
            sub.populate(text=''.join(sorted(chars)) + ' ')
            sub.subset(f)
            bio = io.BytesIO()
            f.flavor = 'woff2'
            f.save(bio)
            faces.append(f"@font-face{{font-family:{FONTS[font][1]};src:url(data:font/woff2;base64,"
                         f"{base64.b64encode(bio.getvalue()).decode()}) format('woff2');}}")
        style = f'<style>{"".join(faces)}</style>' if faces else ''
        sprites = SPRITES if self.sprites else ''
        return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" role="img" aria-label="{esc(self.label)}">\n'
                f'<defs>{style}{sprites}{defs}</defs>\n' + '\n'.join(self.body) + '\n</svg>\n')


def sprite(kind, x, y, size, rot=0, opacity=1):
    """kind: sk1, sk2, dk. size = lato lungo, x/y = centro."""
    w, h = {'sk1': SK_DIMS[0], 'sk2': SK_DIMS[1], 'dk': DK_DIMS}[kind]
    k = size / max(w, h)
    W, H = w * k, h * k
    op = f' opacity="{opacity}"' if opacity != 1 else ''
    return (f'<g transform="translate({x:.1f} {y:.1f}) rotate({rot:.1f})"{op}><use href="#{kind}" xlink:href="#{kind}" '
            f'x="{-W/2:.1f}" y="{-H/2:.1f}" width="{W:.1f}" height="{H:.1f}"/></g>')


def floaters(W, H, n, seed, alpha=(0.07, 0.12), sizes=(24, 74)):
    """Teschi e papere che attraversano lentamente il riquadro, 3 teschi ogni 2 papere."""
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        kind = ['sk1', 'dk', 'sk2', 'dk', 'sk1'][i % 5]
        size = rnd.uniform(*sizes)
        y = rnd.uniform(size * .5, H - size * .5)
        dur = rnd.uniform(28, 58)
        begin = -rnd.uniform(0, dur)
        rot, wob, wdur = rnd.uniform(-25, 25), rnd.uniform(4, 9), rnd.uniform(3, 6)
        inner = sprite(kind, 0, 0, size)
        if kind == 'dk' and rnd.random() < .5:
            inner = f'<g transform="scale(-1 1)">{inner}</g>'
        out.append(f'<g opacity="{rnd.uniform(*alpha):.2f}"><g>'
                   f'<animateTransform attributeName="transform" type="translate" from="{-size:.0f} {y:.0f}" to="{W + size:.0f} {y:.0f}" '
                   f'dur="{dur:.1f}s" begin="{begin:.1f}s" repeatCount="indefinite"/>'
                   f'<g transform="rotate({rot:.0f})"><g><animateTransform attributeName="transform" type="rotate" '
                   f'values="{-wob:.1f};{wob:.1f};{-wob:.1f}" dur="{wdur:.1f}s" repeatCount="indefinite"/>{inner}</g></g></g></g>')
    return '\n'.join(out)


def frame(W, H, r=18):
    return (f'<clipPath id="clip"><rect width="{W}" height="{H}" rx="{r}"/></clipPath>',
            f'<rect width="{W}" height="{H}" rx="{r}" fill="{BG}"/>')


def band(W, H, angle=-18, thick=0.55):
    L = math.hypot(W, H) * 1.6
    return (f'<rect x="{-L/2:.0f}" y="{-H*thick/2:.0f}" width="{L:.0f}" height="{H*thick:.0f}" fill="{BAND}" '
            f'transform="translate({W/2} {H/2}) rotate({angle})"/>')


def tag(d, x, y, s, anchor='start'):
    tw = width(s.upper(), 'mono', 13, 2.2)
    cx = x if anchor == 'start' else x - tw / 2 - 11
    d.add(f'<circle cx="{cx+4:.1f}" cy="{y-4.5:.1f}" r="8" fill="{RED}" opacity=".22"/>'
          f'<circle cx="{cx+4:.1f}" cy="{y-4.5:.1f}" r="4.5" fill="{RED}"/>')
    d.text(cx + 18, y, s.upper(), 'mono', 13, YELLOW, 'start', 2.2)


# ───────────── pezzi fissi ─────────────

def hero():
    W, H = 1000, 340
    d = Doc(W, H, 'ZAICK.NET — Qui mi ci sono impaperato parecchio.')
    clip, bgr = frame(W, H)
    d.add(bgr)
    d.add(f'<g clip-path="url(#clip)">{band(W, H)}{floaters(W, H, 16, 7)}</g>')
    tag(d, 56, 96, 'benvenuto, benvenuto!')
    d.text(54, 192, 'ZAICK', 'display', 92, BONE)
    d.text(54 + width('ZAICK', 'display', 92), 192, '.NET', 'display', 92, YELLOW)
    d.text(56, 238, 'Ah, un visitatore! Entra, entra, fai come fossi a casa tua.', 'body', 17, MUTED)
    d.text(56, 264, 'Solo una cosa: non toccare le papere, sono in ordine. Più o meno.', 'body', 17, MUTED)
    d.text(56, 300, 'Qui mi ci sono impaperato parecchio.', 'bold', 18, BONE)
    avatar = Image.open(io.BytesIO(fetch(f'https://avatars.githubusercontent.com/{USER}?s=460')))
    avatar.thumbnail((440, 440), Image.LANCZOS)
    av = png_uri(avatar, jpeg=True)
    S, cx, cy = 214, 832, 170
    d.add(f'<g transform="translate({cx} {cy}) rotate(-3)">'
          f'<rect x="{-S/2+10}" y="{-S/2+10}" width="{S}" height="{S}" rx="18" fill="{YELLOW}" stroke="{INK}" stroke-width="3"/>'
          f'<clipPath id="av"><rect x="{-S/2}" y="{-S/2}" width="{S}" height="{S}" rx="18"/></clipPath>'
          f'<rect x="{-S/2}" y="{-S/2}" width="{S}" height="{S}" rx="18" fill="{INK}"/>'
          f'<image x="{-S/2-4}" y="{-S/2-4}" width="{S+8}" height="{S+8}" href="{av}" xlink:href="{av}" clip-path="url(#av)"/>'
          f'<rect x="{-S/2}" y="{-S/2}" width="{S}" height="{S}" rx="18" fill="none" stroke="{INK}" stroke-width="3"/></g>')

    def bob(amp, dur, beg):
        return (f'<animateTransform attributeName="transform" type="translate" values="0 0;0 {-amp};0 0" '
                f'dur="{dur}s" begin="{beg}s" repeatCount="indefinite"/>')
    d.add(f'<g>{bob(5, 3.2, 0)}{sprite("sk2", cx + S/2 + 4, cy - S/2 + 6, 70, 14)}</g>')
    d.add(f'<g>{bob(4, 2.6, -1)}{sprite("dk", cx - S/2 - 6, cy + S/2 - 4, 84, -10)}</g>')
    return d.render(clip)


def pond():
    """Separatore: il pelo dell'acqua, con una sola papera che lo attraversa con calma."""
    W, H = 1000, 64
    d = Doc(W, H, '')
    wl = 60  # lunghezza d'onda
    y0 = 44

    def wave(amp, phase):
        p = f'M{-wl*2} {y0}'
        for i in range(-2, W // wl + 3):
            x = i * wl
            p += f' Q{x + wl/4 + phase:.1f} {y0 - amp} {x + wl/2 + phase:.1f} {y0} T{x + wl + phase:.1f} {y0}'
        return p
    fade = ('<linearGradient id="fade" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/>'
            '<stop offset=".15" stop-color="#fff"/><stop offset=".85" stop-color="#fff"/>'
            '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
            f'<mask id="m"><rect width="{W}" height="{H}" fill="url(#fade)"/></mask>')
    d.add(f'<g mask="url(#m)" fill="none" stroke-linecap="round">'
          f'<path d="{wave(3, 0)}" stroke="{YELLOW}" stroke-width="2" opacity=".55">'
          f'<animateTransform attributeName="transform" type="translate" from="0 0" to="{wl} 0" dur="5s" repeatCount="indefinite"/></path>'
          f'<path d="{wave(2, wl/2)}" stroke="{YELLOW}" stroke-width="1.5" opacity=".25" transform="translate(0 7)">'
          f'<animateTransform attributeName="transform" type="translate" from="0 7" to="{-wl} 7" dur="7s" repeatCount="indefinite"/></path>'
          f'</g>')
    duck = sprite('dk', 0, 0, 40)
    d.add(f'<g><animateTransform attributeName="transform" type="translate" from="-40 {y0 - 14}" to="{W + 40} {y0 - 14}" '
          f'dur="34s" repeatCount="indefinite"/>'
          f'<g><animateTransform attributeName="transform" type="rotate" values="-5;5;-5" dur="2.6s" repeatCount="indefinite"/>{duck}</g></g>')
    return d.render(fade)


def seal():
    """Separatore: un teschio solo, al centro, tra due linee che sfumano."""
    W, H = 1000, 56
    d = Doc(W, H, '')
    grad = (f'<linearGradient id="l" x1="0" x2="1"><stop offset="0" stop-color="{YELLOW}" stop-opacity="0"/>'
            f'<stop offset="1" stop-color="{YELLOW}" stop-opacity=".7"/></linearGradient>'
            f'<linearGradient id="r" x1="0" x2="1"><stop offset="0" stop-color="{YELLOW}" stop-opacity=".7"/>'
            f'<stop offset="1" stop-color="{YELLOW}" stop-opacity="0"/></linearGradient>')
    cy = H / 2
    d.add(f'<rect x="120" y="{cy-1}" width="{W/2-160}" height="2" rx="1" fill="url(#l)"/>')
    d.add(f'<rect x="{W/2+40}" y="{cy-1}" width="{W/2-160}" height="2" rx="1" fill="url(#r)"/>')
    for dx in (-30, 30):
        d.add(f'<circle cx="{W/2+dx}" cy="{cy}" r="2.5" fill="{YELLOW}"/>')
    d.add(f'<g><animateTransform attributeName="transform" type="rotate" values="-6 {W/2} {cy};6 {W/2} {cy};-6 {W/2} {cy}" '
          f'dur="4s" repeatCount="indefinite"/>{sprite("sk2", W/2, cy, 40)}</g>')
    return d.render(grad)


def tools():
    W = 1000
    hot = ['C#', '.NET', 'Rust', 'PowerShell']
    rest = ['Visual Studio', 'VS Code', 'Git', 'GitHub Actions', 'Windows', 'Markdown', 'Steam']
    chips = [(c, True) for c in hot] + [(c, False) for c in rest]
    gap, padx, chh = 10, 14, 34
    rows, cur, curw, maxw = [], [], 0, W - 140
    for c, h in chips:
        cw = width(c, 'mono', 14) + padx * 2
        if cur and curw + gap + cw > maxw:
            rows.append((cur, curw))
            cur, curw = [], 0
        cur.append((c, h, cw))
        curw += (gap if len(cur) > 1 else 0) + cw
    rows.append((cur, curw))
    H = 110 + len(rows) * (chh + gap) + 70
    d = Doc(W, H, 'Gli attrezzi del mestiere: ' + ', '.join(c for c, _ in chips), sprites=False)
    d.add(f'<rect x="10" y="10" width="{W-20}" height="{H-20}" rx="16" fill="{INK}"/>')
    d.add(f'<rect x="1" y="1" width="{W-20}" height="{H-20}" rx="16" fill="{PANEL}" stroke="{LINE}" stroke-width="2"/>')
    cxm = (W - 20) / 2
    tag(d, cxm, 50, 'gli attrezzi del mestiere', 'middle')
    y = 74
    for row, rw in rows:
        x = cxm - rw / 2
        for c, h, cw in row:
            d.add(f'<rect x="{x:.1f}" y="{y}" width="{cw:.1f}" height="{chh}" rx="7" fill="{YELLOW if h else PANEL2}" '
                  f'stroke="{INK if h else LINE}" stroke-width="{2 if h else 1}"/>')
            d.text(x + cw / 2, y + 22, c, 'mono', 14, INK if h else TEXT, 'middle')
            x += cw + gap
        y += chh + gap
    d.add(f'<line x1="60" y1="{y+14}" x2="{W-80}" y2="{y+14}" stroke="{LINE}" stroke-dasharray="4 5"/>')
    d.text(cxm, y + 46, 'Ogni artista ha i suoi pennelli. I miei, ogni tanto, fanno quack.', 'body', 16, MUTED, 'middle',
           extra=' font-style="italic"')
    return d.render()


def finale():
    W, H = 1000, 270
    d = Doc(W, H, 'Ti è piaciuto lo spettacolo? Applausi, prego!')
    clip, bgr = frame(W, H)
    d.add(bgr)
    d.add(f'<g clip-path="url(#clip)">{band(W, H, 16, .6)}{floaters(W, H, 12, 21, sizes=(22, 60))}</g>')
    tag(d, W / 2, 62, 'gran finale', 'middle')
    d.text(W / 2, 122, 'Ti è piaciuto lo spettacolo?', 'display', 44, BONE, 'middle')
    d.text(W / 2, 174, 'Applausi, prego!', 'display', 44, YELLOW, 'middle')
    d.text(W / 2, 214, 'Se una delle mie papere ti ha strappato un sorriso, lasciale una stella.', 'body', 16, MUTED, 'middle')
    d.text(W / 2, 238, 'Non è vanità, è riconoscenza. Va bene, è anche un po’ di vanità.', 'body', 16, MUTED, 'middle')
    return d.render(clip)


# ───────────── le papere: una card per repo ─────────────

ICON_NAMES = ['icon', 'appicon', 'app-icon', 'app_icon', 'app', 'favicon', None, 'logo', 'duck', 'avatar', 'ic_launcher', 'brand', 'mark']
ICON_EXTS = {'png': 4, 'svg': 4, 'webp': 4, 'ico': 3, 'jpg': 2, 'jpeg': 2, 'gif': 1}
GOOD_DIRS = {'docs', 'doc', 'assets', 'asset', 'images', 'image', 'img', 'icons', 'icon', 'public', 'static',
             'resources', 'res', 'media', '.github', 'logo', 'branding'}
SKIP = re.compile(r'(^|/)(node_modules|vendor|dist|build|bin|obj|packages|\.git|tests?|__tests__|screenshots?|readme)(/|$)', re.I)
FALLBACK_PATHS = ['assets/icon.png', 'docs/icon.png', 'assets/icon.ico', 'assets/app.ico', 'icon.png', 'icon.ico',
                  'favicon.ico', 'assets/duck.ico', 'assets/logo.png', 'logo.png']


def rank_icons(tree, repo):
    names = [n if n else repo.lower() for n in ICON_NAMES]
    out = []
    for f in tree:
        path = f.get('path', '')
        if f.get('type') != 'blob' or SKIP.search(path):
            continue
        parts = path.split('/')
        file = parts[-1]
        if '.' not in file[1:]:
            continue
        stem, ext = file.rsplit('.', 1)
        ext = ext.lower()
        if ext not in ICON_EXTS or ext == 'svg':   # gli SVG di terzi non si possono incorporare con sicurezza
            continue
        base = re.sub(r'([-_.@]?(\d+x\d+|\d+|small|large|dark|light|round|square|foreground|background|2x|3x))+$', '', stem.lower())
        if base not in names:
            continue
        score = 100 - names.index(base) * 5 + ICON_EXTS[ext] * 3 - (len(parts) - 1) * 4
        if len(parts) > 1 and parts[-2].lower() in GOOD_DIRS:
            score += 6
        if re.search(r'256|512|1024', file):
            score += 2
        if f.get('size', 0) > 1_500_000:
            score -= 40
        out.append((score, path))
    return [p for _, p in sorted(out, reverse=True)[:8]]


def open_image(raw):
    try:
        im = Image.open(io.BytesIO(raw))
        if im.format == 'ICO':
            im.size = max(im.info.get('sizes', {im.size}))
        im.load()
        return im.convert('RGBA')
    except Exception:
        return None


def find_icon(repo, branch):
    """Prima immagine quadrata tra le candidate; un logo largo di solito ha il nome scritto accanto."""
    tree = api_json(f'/repos/{USER}/{urllib.parse.quote(repo)}/git/trees/{urllib.parse.quote(branch)}?recursive=1')
    candidates = rank_icons(tree.get('tree', []), repo) if tree else FALLBACK_PATHS
    wide = None
    for path in candidates:
        raw = fetch(f'{RAW}/{urllib.parse.quote(repo)}/{urllib.parse.quote(branch)}/' + urllib.parse.quote(path))
        im = open_image(raw) if raw else None
        if not im:
            continue
        ratio = im.width / im.height
        if 0.8 <= ratio <= 1.25:
            return im
        if ratio > 1.25 and wide is None:
            wide = im.crop((0, 0, im.height, im.height))   # tiene solo il simbolo a sinistra
    return wide


def card(name, desc, icon, index):
    W, H = 480, 196
    d = Doc(W, H, name, sprites=icon is None)
    cw, ch = W - 10, H - 10
    d.add(f'<rect x="8" y="8" width="{cw}" height="{ch}" rx="16" fill="{INK}"/>')
    d.add(f'<rect x="1" y="1" width="{cw}" height="{ch}" rx="16" fill="{PANEL}" stroke="{LINE}" stroke-width="2"/>')
    I, ix, iy = 92, 26, 28
    d.add(f'<rect x="{ix}" y="{iy}" width="{I}" height="{I}" rx="16" fill="{PANEL2}" stroke="{LINE}" stroke-width="2"/>')
    if icon is not None:
        icon = icon.copy()
        icon.thumbnail((192, 192), Image.LANCZOS)
        k = (I - 18) / max(icon.size)
        iw, ih = icon.width * k, icon.height * k
        uri = png_uri(icon)
        d.add(f'<image x="{ix + (I-iw)/2:.1f}" y="{iy + (I-ih)/2:.1f}" width="{iw:.1f}" height="{ih:.1f}" href="{uri}" xlink:href="{uri}"/>')
    else:
        kind = ['sk1', 'dk', 'sk2'][index % 3]
        d.add(sprite(kind, ix + I / 2, iy + I / 2, 58, 0, .45))
    tx = ix + I + 22
    d.text(tx, iy + 30, fit(name, 'display', 25, cw - tx - 40), 'display', 25, BONE)
    lines = wrap(desc, 'body', 15, cw - tx - 24)
    if len(lines) > 4:
        lines = lines[:3] + [fit(' '.join(lines[3:]), 'body', 15, cw - tx - 24)]
    for j, line in enumerate(lines):
        d.text(tx, iy + 58 + j * 23, line, 'body', 15, MUTED)
    d.text(cw - 18, 34, '↗', 'bold', 18, YELLOW, 'end')
    return d.render()


def load_repos():
    """Repo pubbliche, le più recenti prima. Fork inclusi, archiviate e profilo esclusi."""
    repos, page = [], 1
    while True:
        batch = api_json(f'/users/{USER}/repos?per_page=100&sort=pushed&page={page}')
        if not batch:
            break
        repos += batch
        if len(batch) < 100:
            break
        page += 1
    if not repos and (ROOT / 'scripts/repos.local.json').exists():   # solo per prove in locale senza API
        repos = json.loads((ROOT / 'scripts/repos.local.json').read_text())
    return [r for r in repos if not r.get('archived') and r['name'].lower() not in HIDDEN][:MAX_REPOS]


def slug(name):
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def build_cards():
    overrides = {}
    path = HERE / 'descrizioni.json'
    if path.exists():
        overrides = {k.lower(): v for k, v in json.loads(path.read_text()).items()}
    repos = load_repos()
    if not repos:
        print('Nessuna repo letta da GitHub: le card restano come sono.')
        return None
    links = []
    keep = set()
    for i, r in enumerate(repos):
        desc = overrides.get(r['name'].lower()) or r.get('description') or 'Questa è timida: non ha ancora detto una parola.'
        icon = find_icon(r['name'], r.get('default_branch') or 'HEAD')
        fname = f'card-{slug(r["name"])}.svg'
        (ASSETS / fname).write_text(card(r['name'], desc, icon, i))
        keep.add(fname)
        links.append(f'<a href="{r["html_url"]}"><img src="{REPO_RAW}/{fname}" width="420" alt="{esc(r["name"])}"></a>')
        print(f'  {r["name"]:<28} icona: {"sì" if icon is not None else "teschio/papera"}')
    for old in ASSETS.glob('card-*.svg'):
        if old.name not in keep:
            old.unlink()
    return '\n'.join(links)


def update_readme(block):
    readme = ROOT / 'README.md'
    text = readme.read_text()
    new = re.sub(r'(<!-- PAPERE:START -->).*?(<!-- PAPERE:END -->)', lambda m: m.group(1) + '\n' + block + '\n' + m.group(2), text, flags=re.S)
    if new != text:
        readme.write_text(new)


def stamp_versions():
    """Aggiunge ?v=<hash> a ogni asset nel README: se un file cambia, cambia l'URL
    e browser, app e CDN non possono più mostrare la versione vecchia dalla cache."""
    import hashlib
    readme = ROOT / 'README.md'
    text = readme.read_text()

    def bump(m):
        f = ASSETS / m.group(1)
        if not f.exists():
            return m.group(0)
        return f'{REPO_RAW}/{m.group(1)}?v=' + hashlib.sha1(f.read_bytes()).hexdigest()[:10]
    new = re.sub(re.escape(REPO_RAW) + r'/([\w.-]+\.svg)(\?v=[\w]+)?', bump, text)
    if new != text:
        readme.write_text(new)


def main():
    ASSETS.mkdir(exist_ok=True)
    fixed = {'hero.svg': hero, 'stagno.svg': pond, 'sigillo.svg': seal, 'tools.svg': tools, 'finale.svg': finale}
    only_cards = '--solo-card' in sys.argv
    for name, fn in fixed.items():
        if not only_cards:
            (ASSETS / name).write_text(fn())
            print('scritto', name)
    block = build_cards()
    if block:
        update_readme(block)
    stamp_versions()


if __name__ == '__main__':
    main()
