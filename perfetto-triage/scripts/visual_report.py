#!/usr/bin/env python3
"""
Turn a triage-out directory into one self-contained report.html.

  ./visual_report.py triage-out                      # neutral theme
  ./visual_report.py triage-out --app-repo ~/code/app  # themed from the app

triage.py already writes report.md for an agent to read. This writes the page a
person reads instead of opening the trace viewer: the same four questions, each
with a chart drawn from the query results and a plain explanation under it.

Two rules the page keeps:

  * Thread states and frames use the colours the Perfetto UI uses, so a strip
    here and the track in ui.perfetto.dev read the same way. Those colours are
    data and never change with the theme.
  * Everything else (page, cards, accent, ranked bars, corner radius, font) is
    taken from the app's own colour tokens when --app-repo finds them and they
    pass a contrast check. If they are missing or unreadable the page falls
    back to a neutral theme and says so in the footer.

Reads findings.json and csv/*.csv. No third-party dependencies, no network, no
JavaScript. Every string that came from the trace is HTML-escaped.
"""

import argparse
import csv
import fnmatch
import html
import json
import os
import re
import sys

NULL = "[NULL]"
esc = html.escape


# --------------------------------------------------------------------------
# reading triage output
# --------------------------------------------------------------------------

def read_csv(out_dir: str, stem: str) -> list[dict]:
    path = os.path.join(out_dir, "csv", stem + ".csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def num(v) -> float | None:
    if v is None or v == "" or v == NULL:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ms(v: float | None) -> str:
    if v is None:
        return "n/a"
    if abs(v) >= 100:
        return f"{v:,.0f} ms"
    if abs(v) >= 10:
        return f"{v:.0f} ms"
    return f"{v:.1f} ms"


def pct(part: float, whole: float) -> str:
    if not whole:
        return "0%"
    p = 100.0 * part / whole
    return f"{p:.0f}%" if p >= 1 else "<1%"


def matches(name: str, pattern: str) -> bool:
    if not pattern or pattern == "*":
        return True
    return fnmatch.fnmatchcase(name or "", pattern) or (name or "").startswith(pattern.rstrip("*"))


def pick_process(rows: list[dict], key: str, pkg: str) -> str | None:
    """The process the report is about: the row the package filter matches.
    Only with no package named does it fall back to the first row (the queries
    sort the busiest first). A named package that matches nothing gets None:
    another app's numbers must never be shown as this app's."""
    named = bool(pkg) and pkg != "*"
    for r in rows:
        if named and matches(r.get(key, ""), pkg):
            return r.get(key)
    return rows[0].get(key) if rows and not named else None


# --------------------------------------------------------------------------
# colour helpers
# --------------------------------------------------------------------------

def parse_hex(value: str) -> tuple[int, int, int] | None:
    """#RGB, #RRGGBB, #AARRGGBB (Android order) or 0xAARRGGBB -> (r, g, b)."""
    if not value:
        return None
    v = value.strip().lower()
    v = v[2:] if v.startswith("0x") else v.lstrip("#")
    if not re.fullmatch(r"[0-9a-f]+", v):
        return None
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    elif len(v) == 4:               # #ARGB
        v = "".join(c * 2 for c in v[1:])
    elif len(v) == 8:               # AARRGGBB, alpha dropped
        v = v[2:]
    if len(v) != 6:
        return None
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


def luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    ra, rb = parse_hex(a), parse_hex(b)
    if not ra or not rb:
        return 0.0
    la, lb = luminance(ra), luminance(rb)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """a moved a fraction t of the way towards b."""
    ra, rb = parse_hex(a), parse_hex(b)
    return to_hex(tuple(round(x + (y - x) * t) for x, y in zip(ra, rb)))


def ink_on(fill: str) -> str:
    """Black or white, whichever reads better on this fill."""
    return "#111111" if contrast(fill, "#111111") >= contrast(fill, "#ffffff") else "#ffffff"


# --------------------------------------------------------------------------
# data colours: the Perfetto UI's convention, fixed regardless of theme
# --------------------------------------------------------------------------

STATE = {
    "running": ("Running", "#317d31"),
    "runnable": ("Runnable", "#99ba36"),
    "io": ("Uninterruptible sleep (IO)", "#ff9900"),
    "blocked": ("Uninterruptible sleep (non-IO)", "#a25b57"),
    "sleeping": ("Sleeping", None),          # drawn empty, as the UI does
    "other": ("Other", "#4051b5"),
}
STATE_MEANING = {
    "running": "on a CPU, executing code",
    "runnable": "ready to run, waiting for a CPU",
    "io": "blocked in the kernel on disk IO",
    "blocked": "blocked in the kernel, not on IO",
    "sleeping": "waiting for something: a lock, a binder reply, a message, a timer",
    "other": "any other scheduler state",
}


def state_key(name: str) -> str:
    n = (name or "").strip()
    low = n.lower()
    if low == "running":
        return "running"
    if low.startswith("runnable") or n in ("R", "R+"):
        return "runnable"
    if "uninterruptible" in low or n.startswith("D"):
        return "io" if ("(io)" in low or n in ("D|IO", "DK")) else "blocked"
    if "sleeping" in low or "idle" in low or n in ("S", "I"):
        return "sleeping"
    return "other"


FRAME = {
    "app": ("Late, app at fault", "#ff5722"),
    "system": ("Late, system at fault", "#ffeb3b"),
    "stuffing": ("Buffer stuffing", "#c0d588"),
    "dropped": ("Dropped", "#03a9f4"),
    "unknown": ("Late, cause unknown", "#f515e0"),
    "ok": ("On time", "#4caf50"),
    "unflagged": ("Late, not flagged as jank", "#9aa0a6"),
}


def frame_key(jank_type: str) -> str:
    t = (jank_type or "").lower()
    if "app deadline missed" in t:
        return "app"
    if "surfaceflinger" in t or "display hal" in t or "prediction error" in t:
        return "system"
    if "buffer stuffing" in t:
        return "stuffing"
    if "dropped" in t:
        return "dropped"
    if t in ("", "none", "no jank"):
        return "ok"
    return "unknown"


# --------------------------------------------------------------------------
# app theme discovery
# --------------------------------------------------------------------------

SKIP_DIRS = {"build", ".gradle", ".git", ".idea", "node_modules", "out", "dist", "Pods",
             ".cxx", "intermediates", "generated", ".kotlin", "DerivedData"}
ROLES = ("primary", "onprimary", "secondary", "tertiary", "background", "onbackground",
         "surface", "onsurface", "error")
KT_NAME = re.compile(r"(color|colour|theme|palette|token|shape|type)", re.I)
KT_VAL = re.compile(r"\bval\s+(\w+)\s*(?::\s*Color)?\s*=\s*Color\(\s*(0x[0-9A-Fa-f]{6,8})\s*\)")
KT_SCHEME = re.compile(r"\b(lightColorScheme|darkColorScheme|lightColors|darkColors)\s*\(")
KT_ARG = re.compile(r"\b(\w+)\s*=\s*(Color\(\s*0x[0-9A-Fa-f]{6,8}\s*\)|[A-Za-z_][\w.]*)")
XML_COLOR = re.compile(r'<color\s+name="([^"]+)"\s*>\s*([^<\s]+)\s*</color>')
KT_RADIUS = re.compile(r"RoundedCornerShape\(\s*(\d+)(?:\.\d+)?\s*\.dp")


def _walk(repo: str, limit: int = 40000):
    seen = 0
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            seen += 1
            if seen > limit:
                return
            yield os.path.join(root, f)


def _balanced(text: str, start: int) -> str:
    """Text inside the parenthesis that opens at text[start-1]."""
    depth, i = 1, start
    while i < len(text) and depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    return text[start:i - 1]


def _role(name: str) -> tuple[str | None, str | None]:
    """A token name -> (role, mode). mode is 'light', 'dark' or None."""
    n = re.sub(r"[^a-z]", "", name.lower())
    mode = "dark" if ("dark" in n or "night" in n) else ("light" if "light" in n else None)
    n = n.replace("mdtheme", "").replace("dark", "").replace("night", "").replace("light", "")
    n = n.replace("color", "").replace("colour", "")
    if n == "accent":
        n = "secondary"
    return (n if n in ROLES else None), mode


def discover_theme(repo: str) -> dict:
    """Look for the app's own colour tokens. Returns
    {"light": {role: hex}, "dark": {role: hex}, "radius": int|None,
     "font": str|None, "sources": [paths]}.  Never raises on odd input."""
    theme = {"light": {}, "dark": {}, "radius": None, "font": None, "sources": []}
    if not repo or not os.path.isdir(repo):
        return theme
    named: dict[str, str] = {}
    scheme_args: list[tuple[str, str, str]] = []     # (mode, role, value expr)
    xml_tokens: list[tuple[str, str, bool]] = []     # (name, value, night)
    fonts: dict[str, int] = {}
    radii: list[int] = []

    for path in _walk(repo):
        rel = os.path.relpath(path, repo)
        base = os.path.basename(path)
        norm = path.replace(os.sep, "/")
        try:
            if base.endswith(".kt") and KT_NAME.search(base) and os.path.getsize(path) < 400_000:
                text = open(path, encoding="utf-8", errors="replace").read()
                hit = False
                for m in KT_VAL.finditer(text):
                    named[m.group(1)] = m.group(2)
                    hit = True
                for m in KT_SCHEME.finditer(text):
                    mode = "dark" if m.group(1).startswith("dark") else "light"
                    for a in KT_ARG.finditer(_balanced(text, m.end())):
                        scheme_args.append((mode, a.group(1), a.group(2)))
                        hit = True
                if "Shapes(" in text or "Shape" in base:
                    med = re.search(r"\bmedium\s*=\s*RoundedCornerShape\(\s*(\d+)", text)
                    found = [int(med.group(1))] if med else [int(x) for x in KT_RADIUS.findall(text)]
                    if found:
                        radii.append(found[0])
                        hit = True
                if hit:
                    theme["sources"].append(rel)
            elif base.startswith("colors") and base.endswith(".xml") and "/res/values" in norm \
                    and os.path.getsize(path) < 400_000:
                text = open(path, encoding="utf-8", errors="replace").read()
                night = "values-night" in norm
                got = XML_COLOR.findall(text)
                xml_tokens.extend((n, v, night) for n, v in got)
                if got:
                    theme["sources"].append(rel)
            elif base.lower().endswith((".ttf", ".otf")) and "/font" in norm:
                family = re.split(r"[_\-. ]", base)[0].lower()
                if family:
                    fonts[family] = fonts.get(family, 0) + 1
        except OSError:
            continue

    xml_named = {n: v for n, v, _ in xml_tokens}

    def resolve(expr: str, depth: int = 0) -> str | None:
        if depth > 6 or not expr:
            return None
        expr = expr.strip()
        m = re.search(r"0x[0-9A-Fa-f]{6,8}", expr)
        if m:
            rgb = parse_hex(m.group(0))
            return to_hex(rgb) if rgb else None
        if expr.startswith("#"):
            rgb = parse_hex(expr)
            return to_hex(rgb) if rgb else None
        if expr.startswith("@color/"):
            return resolve(xml_named.get(expr[7:], ""), depth + 1)
        key = expr.split(".")[-1]
        if key in named:
            return resolve(named[key], depth + 1)
        return None

    # 1. an explicit Compose colour scheme is the strongest signal
    for mode, arg, expr in scheme_args:
        role, _ = _role(arg)
        value = resolve(expr)
        if role and value:
            theme[mode].setdefault(role, value)
    # 2. XML resources, values-night feeding the dark scheme
    for name, value, night in xml_tokens:
        role, mode = _role(name)
        hexv = resolve(value)
        if role and hexv:
            theme[mode or ("dark" if night else "light")].setdefault(role, hexv)
    # 3. plainly named Kotlin vals (Primary, PrimaryDark, md_theme_light_primary)
    for name, expr in named.items():
        role, mode = _role(name)
        hexv = resolve(expr)
        if role and hexv:
            theme[mode or "light"].setdefault(role, hexv)

    # 4. no declared tokens at all: read the colours the UI code uses inline.
    #    This is a guess from usage counts, and the page says so.
    if not theme["light"].get("primary") and not theme["dark"].get("primary"):
        inferred, files = _infer_from_usage(repo)
        if inferred:
            mode = "dark" if luminance(parse_hex(inferred["background"])) < 0.2 else "light"
            for role, value in inferred.items():
                theme[mode].setdefault(role, value)
            theme["inferred"] = True
            theme["sources"].extend(files)

    if radii:
        theme["radius"] = max(0, min(28, radii[0]))
    if fonts:
        family = max(fonts, key=fonts.get)
        if re.fullmatch(r"[a-z][a-z0-9]{1,30}", family):
            theme["font"] = family.capitalize()
    theme["sources"] = sorted(set(theme["sources"]))[:8]
    return theme


KT_INLINE = re.compile(r"(\.background\(|\b\w+\s*=\s*(?:if\s*\([^)]*\)\s*)?)\s*Color\(\s*(0x[0-9A-Fa-f]{6,8})\s*\)")


def _saturation(rgb: tuple[int, int, int]) -> float:
    hi, lo = max(rgb), min(rgb)
    return 0.0 if hi == 0 else (hi - lo) / hi


def _infer_from_usage(repo: str, max_files: int = 3000) -> tuple[dict, list[str]]:
    """Background, text and accent guessed from inline Color(0x..) literals:
    the most used background fill, the most used readable text colour on it,
    and the most used saturated colour that stands out from it."""
    bg: dict[str, int] = {}
    fg: dict[str, int] = {}
    accent: dict[str, int] = {}
    where: dict[str, int] = {}
    seen = 0
    for path in _walk(repo):
        if not path.endswith(".kt"):
            continue
        seen += 1
        if seen > max_files:
            break
        try:
            if os.path.getsize(path) > 400_000:
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if "Color(0x" not in text:
            continue
        hits = 0
        for m in KT_INLINE.finditer(text):
            rgb = parse_hex(m.group(2))
            if not rgb:
                continue
            hexv, lead = to_hex(rgb), m.group(1).lower()
            hits += 1
            if _saturation(rgb) >= 0.35:
                weight = 3 if re.match(r"(contentcolor|tint|primary|accent|selected)", lead) else 1
                accent[hexv] = accent.get(hexv, 0) + weight
            elif ".background(" in lead or lead.startswith(("containercolor", "backgroundcolor", "background")):
                bg[hexv] = bg.get(hexv, 0) + 1
            elif lead.startswith(("color", "contentcolor", "textcolor")):
                fg[hexv] = fg.get(hexv, 0) + 1
        if hits:
            where[os.path.relpath(path, repo)] = hits
    if not bg or not accent:
        return {}, []
    background = max(bg, key=bg.get)
    out = {"background": background}
    readable = {c: n for c, n in fg.items() if contrast(c, background) >= 4.5}
    if readable:
        out["onbackground"] = max(readable, key=readable.get)
    standout = {c: n for c, n in accent.items() if contrast(c, background) >= 3.0}
    if standout:
        out["primary"] = max(standout, key=standout.get)
    if "primary" not in out:
        return {}, []
    files = sorted(where, key=where.get, reverse=True)[:3]
    return out, files


DEFAULTS = {
    "light": {"bg": "#f5f6f8", "surface": "#ffffff", "text": "#14171c",
              "accent": "#3558d6", "plot": "#ffffff", "plot_text": "#1d2127"},
    "dark": {"bg": "#0e1014", "surface": "#171a20", "text": "#e8eaee",
             "accent": "#8ea8ff", "plot": "#101216", "plot_text": "#e3e6ea"},
}


def resolve_theme(theme: dict | None) -> tuple[dict, list[str]]:
    """App tokens -> the CSS variables the page uses, for light and dark.
    A token is used only if text stays readable with it. Returns (vars, notes)."""
    raw = theme or {}
    theme = {"radius": None, "font": None, "inferred": bool(raw.get("inferred")),
             "sources": [str(x) for x in (raw.get("sources") or [])][:8]}
    for mode in ("light", "dark"):
        clean = {}
        for role, value in (raw.get(mode) or {}).items():
            rgb = parse_hex(str(value))
            if rgb and str(role).lower() in ROLES:
                clean[str(role).lower()] = to_hex(rgb)
        theme[mode] = clean
    if isinstance(raw.get("radius"), (int, float)):
        theme["radius"] = max(0, min(28, int(raw["radius"])))
    if isinstance(raw.get("font"), str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]{1,30}", raw["font"]):
        theme["font"] = raw["font"]
    notes, out, used = [], {}, False
    for mode in ("light", "dark"):
        d = dict(DEFAULTS[mode])
        app = theme.get(mode, {})
        bg = app.get("background") or app.get("surface")
        text = app.get("onbackground") or app.get("onsurface")
        if bg and not text:
            text = "#14171c" if contrast(bg, "#14171c") >= contrast(bg, "#e8eaee") else "#e8eaee"
        if bg and text:
            if contrast(bg, text) >= 4.5:
                d["bg"], d["text"] = bg, text
                surface = app.get("surface") or bg
                d["surface"] = surface if contrast(surface, text) >= 4.5 else bg
                used = True
            else:
                notes.append(f"{mode}: app background and text colours contrast "
                             f"{contrast(bg, text):.1f}:1, below 4.5:1, so the neutral pair is used")
        primary = app.get("primary") or (theme.get("light", {}).get("primary") if mode == "dark" else None)
        if primary:
            if contrast(primary, d["bg"]) >= 3.0 and contrast(primary, d["surface"]) >= 3.0:
                d["accent"] = primary
                used = True
            else:
                notes.append(f"{mode}: app primary {primary} contrasts "
                             f"{contrast(primary, d['surface']):.1f}:1 with the page, below 3:1, "
                             f"so the neutral accent is used")
        on = app.get("onprimary")
        d["on_accent"] = on if (on and d["accent"] == app.get("primary")
                                and contrast(on, d["accent"]) >= 4.5) else ink_on(d["accent"])
        d["muted"] = mix(d["text"], d["bg"], 0.38)
        d["border"] = mix(d["text"], d["bg"], 0.84)
        d["track"] = mix(d["text"], d["surface"], 0.9)
        d["plot_grid"] = mix(d["plot_text"], d["plot"], 0.82)
        d["plot_muted"] = mix(d["plot_text"], d["plot"], 0.35)
        out[mode] = d
    out["radius"] = theme.get("radius") if theme.get("radius") is not None else 10
    out["font"] = theme.get("font")
    out["themed"] = used
    return out, notes


# --------------------------------------------------------------------------
# chart builders. Each returns an HTML fragment.
# --------------------------------------------------------------------------

W = 720


def strip_chart(segments: list[tuple[str, float, str | None]], label: str, fmt=ms) -> str:
    """One proportional strip. segments = [(name, value, fill-or-None)].
    A None fill is drawn empty with a dashed edge, the way the Perfetto UI
    leaves sleeping time blank."""
    segments = [s for s in segments if s[1] and s[1] > 0]
    total = sum(s[1] for s in segments)
    if not total:
        return ""
    cells, legend = [], []
    for name, value, fill in segments:
        share = value / total
        text = f"{name} {pct(value, total)}" if share >= 0.22 else (pct(value, total) if share >= 0.07 else "")
        tip = esc(f"{name}: {fmt(value)} ({pct(value, total)})")
        style = (f"flex:{value:.4f} 1 0;background:{fill};color:{ink_on(fill)}" if fill
                 else f"flex:{value:.4f} 1 0")
        cells.append(f'<span class="seg{"" if fill else " seg-empty"}" style="{style}" '
                     f'title="{tip}">{esc(text)}</span>')
        sw = (f'<span class="sw" style="background:{fill}"></span>' if fill
              else '<span class="sw sw-empty"></span>')
        legend.append(f"<li>{sw}<span>{esc(name)}</span><b>{esc(fmt(value))}</b>"
                      f"<i>{pct(value, total)}</i></li>")
    return (f'<div class="plot"><div class="strip" role="img" aria-label="{esc(label)}">'
            f'{"".join(cells)}</div><ul class="legend">{"".join(legend)}</ul></div>')


def bar_chart(rows: list[tuple[str, float, str]], label: str, fmt=ms) -> str:
    """Ranked horizontal bars. rows = [(name, value, note)]. Drawn in the
    accent colour: this is the one chart that takes the app's own colour."""
    rows = [r for r in rows if r[1] is not None and r[1] > 0]
    if not rows:
        return ""
    top = max(r[1] for r in rows)
    out = [f'<div class="plot bars" role="img" aria-label="{esc(label)}">']  # rows may carry a 4th item: a badge
    for name, value, note, *rest in rows:
        badge = f'<span class="own">{esc(rest[0])}</span>' if rest and rest[0] else ""
        out.append(
            f'<div class="bar-row"><span class="bar-name" title="{esc(name)}">{badge}{esc(name)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{100 * value / top:.2f}%">'
            f'</span></span><span class="bar-val">{esc(fmt(value))}'
            f'{"<i>" + esc(note) + "</i>" if note else ""}</span></div>')
    out.append("</div>")
    return "".join(out)


def frames_chart(frames: list[dict]) -> str:
    """Worst frames as bars measured from the deadline."""
    rows = []
    for f in frames:
        over = num(f.get("overrun_ms"))
        if over is None or over <= 0:
            continue
        key = frame_key(f.get("jank_type", ""))
        # This chart only lists frames that overran, so "no jank type" here means
        # the system did not class the overrun as jank, not that it was on time.
        rows.append((f.get("frame_id", "?"), over, num(f.get("ui_ms")),
                     "unflagged" if key == "ok" else key, f.get("jank_type", "")))
    if not rows:
        return ""
    top = max(r[1] for r in rows)
    left, right, row_h = 118, 150, 26
    span = W - left - right
    height = len(rows) * row_h + 34
    p = [f'<line x1="{left}" y1="6" x2="{left}" y2="{height - 22}" stroke="var(--plot-text)" '
         f'stroke-width="1.5"/>',
         f'<text x="{left}" y="{height - 6}" text-anchor="middle" font-size="12" '
         f'fill="var(--plot-muted)">deadline</text>',
         f'<text x="{left + span}" y="{height - 6}" text-anchor="end" font-size="12" '
         f'fill="var(--plot-muted)">{esc(ms(top))} late</text>']
    for i, (fid, over, ui, key, raw) in enumerate(rows):
        y = 8 + i * row_h
        w = max(2.0, span * over / top)
        fill = FRAME[key][1]
        p.append(f'<text x="{left - 8}" y="{y + 14}" text-anchor="end" font-size="12" '
                 f'fill="var(--plot-text)">frame {esc(str(fid))}</text>')
        p.append(f'<rect x="{left}" y="{y}" width="{w:.2f}" height="{row_h - 8}" fill="{fill}" '
                 f'stroke="var(--plot-text)" stroke-opacity="0.35" stroke-width="0.75">'
                 f'<title>{esc(raw or "unknown")}</title></rect>')
        note = f"+{ms(over)}" + (f" · UI thread {ms(ui)}" if ui else "")
        inside = w > 7 * len(note) + 16
        tx = left + w - 8 if inside else left + w + 8
        p.append(f'<text x="{tx:.2f}" y="{y + 14}" text-anchor="{"end" if inside else "start"}" '
                 f'font-size="12" fill="{ink_on(fill) if inside else "var(--plot-text)"}">'
                 f'{esc(note)}</text>')
    keys = []
    for r in rows:
        if r[3] not in keys:
            keys.append(r[3])
    legend = "".join(f'<li><span class="sw" style="background:{FRAME[k][1]}"></span>'
                     f'<span>{esc(FRAME[k][0])}</span></li>' for k in keys)
    return (f'<div class="plot"><svg viewBox="0 0 {W} {height}" role="img" '
            f'aria-label="Worst frames, measured from their deadline">{"".join(p)}</svg>'
            f'<ul class="legend">{legend}</ul></div>')


def tiles(items: list[tuple[str, str, str]]) -> str:
    items = [i for i in items if i[1]]
    if not items:
        return ""
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><b>{esc(value)}</b><span>{esc(label)}</span>'
        f'{"<i>" + esc(note) + "</i>" if note else ""}</div>'
        for label, value, note in items) + "</div>"


def figure(title: str, how: str, chart: str, means: str = "", where: str = "") -> str:
    if not chart:
        return ""
    out = [f'<figure><figcaption><h3>{esc(title)}</h3><p class="how">{esc(how)}</p>'
           f'</figcaption>{chart}']
    if means:
        out.append(f'<p class="means"><span class="tag">inference</span><b>What it means.</b> '
                   f'{esc(means)}</p>')
    if where:
        out.append(f'<p class="where"><b>See it in Perfetto.</b> {esc(where)}</p>')
    out.append("</figure>")
    return "".join(out)


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

SEV_LABEL = {"blocker": "Blocker", "high": "High", "medium": "Medium", "info": "Info"}
VERDICT = {
    "trusted": ("Trusted", "The capture passed every check. The numbers can be quoted "
                           "with the device, build type and run count."),
    "directional_only": ("Directional only", "The findings point at real code paths, "
                                             "but do not quote the numbers."),
    "not_trustworthy": ("Not trustworthy", "Fix the capture before reading anything below."),
}


# What a missing data source means for the reader, in their words.
GAP_MEANS = {
    "frametimeline": "which frames were late",
    "blocked_reasons": "why a thread was blocked in the kernel",
    "binder": "which calls to other processes were slow",
    "sched": "whether a thread was running or waiting",
    "slices": "what the app was doing",
}


def gaps_text(hs: dict) -> str:
    """'which frames were late (this phone runs Android 10, ...); why a thread ...'"""
    out = []
    for gap in hs.get("coverage_gaps") or []:
        text = GAP_MEANS.get(gap, gap.replace("_", " "))
        if gap == "frametimeline" and hs.get("frames") == "unsupported":
            rel = (hs.get("device") or {}).get("android") or "an old version"
            text += f" (this phone runs Android {rel}, and frame deadlines are recorded from Android 12)"
        out.append(text)
    return "; ".join(out)


def section_of(f: dict) -> str:
    src = f.get("source") or []
    if f.get("area") == "trace":
        return "q1"
    if f.get("area") == "code":
        return "q4"
    if any(s.startswith(("12_", "33_")) for s in src):
        return "q2"
    if any(s.startswith("2") for s in src):
        return "q3"
    if any(s.startswith(("1", "3", "4")) for s in src):
        return "q4"
    if src:
        return "q1"
    return {"trace": "q1", "jank": "q3"}.get(f.get("area", ""), "q4")


def finding_cards(findings: list[dict]) -> str:
    out = []
    for f in findings:
        sev = f.get("severity", "info")
        tag = '<span class="tag">directional only</span>' if f.get("directional_only") else ""
        src = ""
        if f.get("source"):
            src = ('<p class="src">Measured in ' +
                   ", ".join(f"<code>{esc(s)}</code>" for s in f["source"]) + "</p>")
        out.append(
            f'<article class="finding sev-{esc(sev)}" id="f{f["_n"]}">'
            f'<header><span class="chip sev-{esc(sev)}">{esc(SEV_LABEL.get(sev, sev))}</span>'
            f'{tag}<h4>{esc(f.get("title", ""))}</h4></header>'
            f'<p>{esc(f.get("detail", ""))}</p>'
            f'<p class="do"><b>What to do.</b> {esc(f.get("action", ""))}</p>{src}</article>')
    return "".join(out)


def q1_trust(hs: dict | None) -> str:
    if not hs:
        return ('<p class="empty">This findings.json has no trust check. It was written by an '
                'older triage.py. Run triage again before relying on any number here.</p>')
    key = hs.get("verdict", "directional_only")
    name, text = VERDICT.get(key, VERDICT["directional_only"])
    gap_list = gaps_text(hs)
    if key == "trusted" and gap_list:
        text = ("The numbers it has can be quoted with the device, build type and run count. "
                "It cannot answer: " + gap_list + ".")
    health = hs.get("trace_health", "unknown")
    rows = [("Trace health", health,
             "no errors and no dropped data" if health == "pass"
             else "trace_processor reported errors or dropped data" if health == "fail"
             else "the health check did not run")]
    unsupported = hs.get("frames") == "unsupported"
    for k, v in (hs.get("coverage") or {}).items():
        if v == "ok":
            note = "this data source is in the trace"
        elif v == "missing" and k == "frametimeline" and unsupported:
            note = "this phone's Android version does not record it"
        elif v == "missing":
            note = "not in the trace, so it cannot answer " + GAP_MEANS.get(k, k)
        else:
            note = "the coverage check did not run"
        rows.append((f"Recorded: {k.replace('_', ' ')}", v, note))
    rows.append(("Build type", hs.get("build_type", "unknown"),
                 hs.get("build_type_detail") or "read from the trace"))
    rows.append(("Startup capture", hs.get("startup_capture", "unknown"),
                 f"capture mode: {hs.get('mode', 'unknown')}"))
    good = {"pass", "ok", "release"}
    bad = {"fail", "debuggable", "missed"}
    # A missing data source fails the verdict only when it is a core one.
    core = {"Recorded: sched", "Recorded: slices"}
    cells = []
    for label, value, note in rows:
        if value == "missing":
            cls = "bad" if label in core else "gap"
        else:
            cls = "good" if value in good else "bad" if value in bad else "na" if value == "not_expected" else "unk"
        word = {"good": "Pass", "bad": "Fail", "na": "n/a", "unk": "Unknown", "gap": "Missing"}[cls]
        cells.append(f'<tr><th scope="row">{esc(label)}</th><td><span class="chip st-{cls}">{word}'
                     f'</span></td><td><code>{esc(str(value))}</code></td><td>{esc(note)}</td></tr>')
    reasons = ""
    if hs.get("reasons"):
        reasons = "<ul class='reasons'>" + "".join(f"<li>{esc(r)}</li>" for r in hs["reasons"]) + "</ul>"
    gaps = ""
    if gap_list and key != "trusted":
        gaps = '<p class="how">This trace cannot answer: ' + esc(gap_list) + ".</p>"
    return (f'<div class="verdict v-{esc(key)}"><span class="chip v-{esc(key)}">{esc(name)}</span>'
            f'<p>{esc(text)}</p></div>{reasons}{gaps}'
            f'<table class="checks"><tbody>{"".join(cells)}</tbody></table>')


def q2_thread_state(out_dir: str, pkg: str) -> str:
    parts = []
    startup = read_csv(out_dir, "12_startup_main_thread_state")
    whole = read_csv(out_dir, "33_main_thread_blocking")

    def chart(rows, name_key, title, scope, whole_trace=False):
        agg: dict[str, float] = {}
        for r in rows:
            v = num(r.get("ms") if "ms" in r else r.get("total_ms"))
            if v and v > 0:
                agg[state_key(r.get(name_key, ""))] = agg.get(state_key(r.get(name_key, "")), 0) + v
        total = sum(agg.values())
        if not total:
            return ""
        order = ["running", "runnable", "io", "blocked", "sleeping", "other"]
        segs = [(STATE[k][0], agg[k], STATE[k][1]) for k in order if k in agg]
        run = agg.get("running", 0)
        wait = total - run - agg.get("runnable", 0)
        if whole_trace:
            blocked = agg.get("io", 0) + agg.get("blocked", 0)
            means = (f"Over a whole capture the main thread is expected to sleep between "
                     f"frames and while it waits for input, so the sleeping share here is not "
                     f"a problem by itself. What to read is how much it ran ({ms(run)}, "
                     f"{pct(run, total)}), how long it was ready with no CPU free "
                     f"({ms(agg.get('runnable', 0))}), and how long it was blocked in the kernel "
                     f"({ms(blocked)}).")
        elif run >= total / 2:
            means = (f"The main thread was executing code for {pct(run, total)} of {scope}. "
                     f"The cost is work the app is doing, so the slices it was running are "
                     f"the place to look.")
        else:
            means = (f"The main thread was on a CPU for only {pct(run, total)} of {scope} and "
                     f"blocked or asleep for {pct(wait, total)}. Profiling the app's own code "
                     f"will not find that time. Look at what the thread was waiting for.")
        if not whole_trace and agg.get("runnable", 0) > total * 0.15:
            means += (f" It also spent {pct(agg['runnable'], total)} ready to run with no CPU "
                      f"free, which points at CPU contention, not at this code.")
        return figure(title,
                      "Each colour is one thread state, sized by its share of the time. "
                      "It is a total, not a timeline: the order is not the order things happened.",
                      strip_chart(segs, title), means,
                      "Expand the app's process. The first thread is the main thread, and the "
                      "thin coloured strip above its slices uses these same colours.")

    if startup:
        parts.append(chart(startup, "main_thread_state", "Main thread during startup", "startup"))
    proc = pick_process(whole, "process_name", pkg)
    mine = [r for r in whole if r.get("process_name") == proc]
    if mine:
        parts.append(chart(mine, "state", f"Main thread over the whole trace ({proc})",
                           "the trace", whole_trace=True))
        blocked = [(r.get("blocked_function"), num(r.get("total_ms")), r.get("state"))
                   for r in mine if r.get("blocked_function") not in (None, "", "-", NULL)]
        blocked = sorted([b for b in blocked if b[1]], key=lambda b: -b[1])[:5]
        if blocked and sum(b[1] for b in blocked) >= 20:      # under 20 ms is not worth a chart
            parts.append(figure("Kernel functions the main thread was blocked in",
                                "Where uninterruptible waits happened, by total time.",
                                bar_chart([(b[0], b[1], b[2]) for b in blocked],
                                          "Kernel blocking functions")))
    body = "".join(p for p in parts if p)
    if not body:
        return '<p class="empty">No thread state data for this process in the trace.</p>'
    items = []
    for k in ("running", "runnable", "io", "blocked", "sleeping"):
        fill = STATE[k][1]
        sw = (f'<span class="sw" style="background:{fill}"></span>' if fill
              else '<span class="sw sw-empty"></span>')
        items.append(f"<li>{sw}<span><b>{esc(STATE[k][0])}</b>: {esc(STATE_MEANING[k])}</span></li>")
    keys = "".join(items)
    return body + f'<details><summary>What the states mean</summary><ul class="legend key">{keys}</ul></details>'


def q3_frames(out_dir: str, pkg: str, hs: dict | None) -> str:
    summary = read_csv(out_dir, "20_jank_summary")
    proc = pick_process(summary, "process_name", pkg)
    row = next((r for r in summary if r.get("process_name") == proc), None)
    if not row:
        if hs and hs.get("frames") == "unsupported":
            rel = (hs.get("device") or {}).get("android") or "this version"
            return (f'<p class="empty">This phone runs Android {esc(rel)}. Frame deadlines are '
                    f'recorded from Android 12, so late frames cannot be measured on it.</p>')
        if hs and (hs.get("coverage") or {}).get("frametimeline") == "missing":
            return ('<p class="empty">This trace has no FrameTimeline data, so missed frames '
                    'cannot be attributed. Record again with the frametimeline data source.</p>')
        return '<p class="empty">No frames were recorded for this process.</p>'
    frames, janky = num(row.get("frames")) or 0, num(row.get("janky")) or 0
    if hs and hs.get("frames") in ("unsupported", "not_recorded"):
        # Frames were counted from Choreographer slices, but nothing recorded a
        # deadline for them. "0 late" would be a claim this trace cannot make.
        if hs.get("frames") == "unsupported":
            rel = (hs.get("device") or {}).get("android") or "this version"
            why = (f"This phone runs Android {rel}. Frame deadlines are recorded from "
                   f"Android 12, so no capture on this phone can say a frame was late. "
                   f"Use an Android 12 or newer phone to measure jank.")
            sub = f"Android {rel} records no deadlines"
        else:
            why = ("The trace has no frame timeline, so there is no deadline to compare "
                   "each frame against. Record again with capture.sh, whose config turns "
                   "the frame timeline on.")
            sub = "no frame timeline in this trace"
        return (tiles([("frames drawn", f"{frames:.0f}", proc or ""),
                       ("late frames", "not measured", sub)])
                + f'<p class="empty">{esc(why)} The frame count above is not a result: '
                  f'it does not mean the frames were on time.</p>')
    app, sf = num(row.get("app_jank")) or 0, num(row.get("sf_jank")) or 0
    other = max(0.0, janky - app - sf)
    worst = num(row.get("worst_overrun_ms"))
    parts = [tiles([
        ("frames recorded", f"{frames:.0f}", proc or ""),
        ("missed their deadline", f"{janky:.0f}", pct(janky, frames) + " of frames"),
        ("worst frame", "+" + ms(worst) if worst and worst > 0 else "on time", "past its deadline"),
    ])]
    segs = [("On time", frames - janky, FRAME["ok"][1]), ("Late, app at fault", app, FRAME["app"][1]),
            ("Late, system at fault", sf, FRAME["system"][1]),
            ("Late, other cause", other, FRAME["unknown"][1])]
    mix_chart = strip_chart(segs, "Frames by outcome", fmt=lambda v: f"{v:.0f} frame" + ("" if round(v) == 1 else "s"))
    rate = 100.0 * janky / frames if frames else 0.0
    low = ("" if rate >= 1 else
           " That is under 1% of frames, which is a low rate: treat what follows as polish, "
           "not as a problem to chase.")
    if janky:
        if app >= sf and app > 0:
            means = (f"{janky:.0f} of {frames:.0f} frames were late. The frame timeline blames the "
                     f"app for {app:.0f} of them and the system compositor for {sf:.0f}, so the "
                     f"app's own frame work is the first place to look." + low)
        elif sf > app:
            means = (f"{janky:.0f} of {frames:.0f} frames were late, and the frame timeline blames "
                     f"the system compositor for more of them ({sf:.0f}) than the app ({app:.0f}). "
                     f"Reading the app's code will not explain those." + low)
        else:
            means = (f"{janky:.0f} of {frames:.0f} frames were late, and the frame timeline does "
                     f"not attribute them to the app or to the compositor.")
    else:
        means = "Every recorded frame met its deadline in this capture."
    parts.append(figure("Frames by outcome",
                        "Every frame in the capture, grouped by whether it met its deadline and, "
                        "if not, who the frame timeline blames. Sizes are frame counts.",
                        mix_chart, means,
                        "Under the app's process, expand Expected Timeline and Actual Timeline. "
                        "The Actual Timeline slices use these colours."))
    worst_rows = [r for r in read_csv(out_dir, "21_worst_frames") if r.get("process_name") == proc][:10]
    first = worst_rows[0].get("frame_id") if worst_rows else None
    parts.append(figure("The worst frames",
                        "One bar per frame, starting at the frame's deadline. Longer means later.",
                        frames_chart(worst_rows), "",
                        (f"Press / in the Perfetto UI and search for a frame number, for example "
                         f"{first}. It appears in the Choreographer#doFrame slice name.")
                        if first else ""))
    split = next((r for r in read_csv(out_dir, "22_jank_cpu_split")
                  if r.get("process_name") == proc), None)
    if split:
        ui, rt = num(split.get("ui_thread_ms")) or 0, num(split.get("render_thread_ms")) or 0
        vs = num(split.get("vsync_delay_ms")) or 0
        if ui and rt and abs(ui - rt) <= 0.1 * max(ui, rt):
            means = ("The UI thread and RenderThread cost about the same in the late frames, "
                     "so neither side is clearly the bottleneck.")
        elif ui > rt:
            means = ("The UI thread costs more than RenderThread in the late frames. Look at "
                     "measure, layout, recomposition and work posted to the main thread before "
                     "looking at the GPU.")
        else:
            means = ("RenderThread costs more than the UI thread in the late frames. Look at "
                     "draw work: shaders, large bitmaps, overdraw and buffer waits.")
        parts.append(figure("Who paid for the late frames",
                            "Time spent inside late frames, summed per stage.",
                            bar_chart([("UI thread (doFrame)", ui, ""),
                                       ("RenderThread (DrawFrame)", rt, ""),
                                       ("Waiting to start (vsync delay)", vs, "")],
                                      "Time in late frames by stage"), means))
    sections = read_csv(out_dir, "24_jank_by_app_section")
    hit = [r for r in sections if (num(r.get("late_frames")) or 0) > 0][:8]
    if hit:
        all_late = num(hit[0].get("all_late_frames")) or 0
        inside = num(hit[0].get("late_frames_in_any_section")) or 0
        top = hit[0]
        rows = [(r.get("section", ""), num(r.get("late_frames")),
                 f"worst +{ms(num(r.get('worst_overrun_ms')))} · ran {r.get('times_run', '')}x",
                 "your code") for r in hit]
        means = (f"{num(top.get('late_frames')):.0f} of the {all_late:.0f} late frames happened "
                 f"while {top.get('section')} was running, so that section is the first place to "
                 f"look. Overlap is not proof it caused them: a section that is simply long "
                 f"overlaps more frames. {inside:.0f} of {all_late:.0f} late frames fall inside "
                 f"at least one of the app's sections.")
        parts.append(figure("Late frames by the app's own trace sections",
                            "Each bar is one trace section the app declares. Its length is the "
                            "number of late frames that overlap it in time.",
                            bar_chart(rows, "Late frames per app trace section",
                                      fmt=lambda v: f"{v:.0f} late"), means,
                            "Press / and search for the section name. The late frames sit on "
                            "the Actual Timeline track at the same time."))
    elif sections:
        parts.append('<p class="empty">The app\'s trace sections ran in this trace, but no late '
                     'frame overlaps any of them. The late frames come from work that is not '
                     'inside a section yet.</p>')
    return "".join(p for p in parts if p)


def q4_time(out_dir: str, pkg: str, hs: dict | None, cm: dict | None = None) -> str:
    parts = []
    owned = {s["name"]: s for s in (cm or {}).get("slices", []) if s.get("owner") == "app"}

    def badge(name):
        return "your code" if name in owned else ""

    def evidence(names):
        hits = [owned[n] for n in names if n in owned]
        if not hits:
            return ""
        return ('<ul class="evidence">' + "".join(
            f'<li><span class="own">your code</span><code>{esc(h["name"])}</code> '
            f'<span>{esc(h["how"])}, <code>{esc(h["evidence"] or "")}</code></span></li>'
            for h in hits) + "</ul>")

    startup = read_csv(out_dir, "10_startup_summary")
    if startup:
        s = startup[0]
        ttid, ttfd = num(s.get("ttid_ms")), num(s.get("ttfd_ms"))
        cls = read_csv(out_dir, "14_startup_class_loading")
        classes = num(cls[0].get("classes_loaded")) if cls else None
        parts.append("<h3 class='sub'>Startup</h3>")
        parts.append(tiles([
            ("time to first frame (TTID)", ms(ttid),
             f"{s.get('startup_type')} start" if s.get("startup_type") not in (None, "", NULL)
             else "start type not recorded"),
            ("time to full content (TTFD)", ms(ttfd) if ttfd else "not reported",
             "" if ttfd else "the app never calls reportFullyDrawn()"),
            ("classes loaded", f"{classes:,.0f}" if classes else "",
             ms(num(cls[0].get("ms"))) + " on the critical path" if cls else ""),
        ]))
        br = [(r.get("reason", ""), num(r.get("ms")), f"{r.get('n', '')} slices")
              for r in read_csv(out_dir, "11_startup_breakdown")][:10]
        parts.append(figure("What startup was spent on",
                            "Perfetto's own breakdown of the launch, largest first. "
                            "Categories overlap, so they do not add up to the total.",
                            bar_chart(br, "Startup time by reason"), "",
                            "Find the Android App Startups track near the top of the trace. "
                            "Selecting the startup slice highlights this window."))
        top = [(r.get("slice_name", ""), num(r.get("total_ms")), f"{r.get('n', '')}x",
                badge(r.get("slice_name", "")))
               for r in read_csv(out_dir, "13_startup_top_slices")][:8]
        parts.append(figure("Longest slices on the main thread during startup",
                            "Ranked by total time. A parent slice includes everything nested "
                            "under it, so read this for where to look, not for blame.",
                            bar_chart(top, "Longest startup slices")
                            + evidence([t[0] for t in top])))
    elif hs and hs.get("startup_capture") == "missed":
        parts.append('<p class="empty">The capture was meant to contain an app launch and does '
                     'not. This is a failed measurement, not a fast startup.</p>')

    hot = read_csv(out_dir, "40_slice_hotspots")[:10]
    if hot:
        parts.append("<h3 class='sub'>Hot slices</h3>")
        rows = [(f"{r.get('name', '')} · {r.get('thread_name', '')}", num(r.get("self_ms")),
                 f"{r.get('n', '')}x", badge(r.get("name", ""))) for r in hot]
        names = [r.get("name", "") for r in hot]
        mine = sum(1 for n in names if n in owned)
        if not cm or not cm.get("checked"):
            means = ("A name here that does not appear anywhere in the app's source is a "
                     "framework or system slice. The app caused it but did not write it. "
                     "Run triage with --app-repo to have each name checked.")
        elif mine:
            means = (f"{mine} of these {len(names)} names "
                     f"{'leads' if mine == 1 else 'lead'} into the app's own code and "
                     f"{'is' if mine == 1 else 'are'} marked. The others were not found in "
                     f"the app's source, so they are framework or library work the app "
                     f"triggered.")
        else:
            means = (f"None of these {len(names)} names was found in the app's source. They are "
                     f"framework or system slices: the app caused them but did not write them. "
                     f"To see your own functions in this list, add trace sections and capture "
                     f"again.")
        parts.append(figure("Where the time was spent, by self time",
                            "Self time is the time inside a slice itself, with the slices nested "
                            "under it taken out. It is the fairest ranking of what cost the most.",
                            bar_chart(rows, "Slices by self time") + evidence(names),
                            means,
                            "Press / and search for a slice name to jump to it."))

    calls = read_csv(out_dir, "30_binder_main_thread")
    client = pick_process(calls, "client_process", pkg)
    binder = [r for r in calls if r.get("client_process") == client][:6]
    why = read_csv(out_dir, "31_binder_why_slow")
    if binder or why:
        parts.append("<h3 class='sub'>Binder calls on the main thread</h3>")
        unnamed = [r for r in binder if r.get("aidl_name") in (None, "", NULL, "unknown")]
        rows = [(f"to {r.get('server_process', '')}", num(r.get("total_ms")), f"{r.get('n', '')}x")
                if r in unnamed else
                (r.get("aidl_name", ""), num(r.get("total_ms")),
                 f"{r.get('n', '')}x to {r.get('server_process', '')}") for r in binder]
        how = "Each bar is one binder interface the main thread called and waited on."
        if unnamed:
            how += (" This trace does not name the interfaces, so those calls are grouped "
                    "by the process that was called.")
        parts.append(figure("Synchronous calls to other processes", how,
                            bar_chart(rows, "Main thread binder calls")))
        agg: dict[str, float] = {}
        for r in why:
            v = num(r.get("total_ms"))
            if v and v > 0:
                agg[state_key(r.get("reason", ""))] = agg.get(state_key(r.get("reason", "")), 0) + v
        total = sum(agg.values())
        if total:
            segs = [(STATE[k][0], agg[k], STATE[k][1])
                    for k in ("running", "runnable", "io", "blocked", "sleeping", "other") if k in agg]
            asleep = agg.get("sleeping", 0)
            means = ("Most of the wait is the calling thread asleep until the reply arrives. "
                     "The call is queueing, not computing, so fewer calls helps more than a "
                     "faster server.") if asleep >= total / 2 else (
                     "The calling thread was mostly active during these calls, so the cost is "
                     "work, not waiting.")
            parts.append(figure("What the caller was doing while it waited",
                                "The calling thread's state during slow binder calls.",
                                strip_chart(segs, "Caller state during binder calls"), means))
    body = "".join(p for p in parts if p)
    return body or '<p class="empty">No startup, slice or binder data for this process.</p>'


def _candidate(c: dict) -> str:
    at = (f"{c['file']}:{c['line']}" if c.get("file") and c.get("line")
          else c.get("file") or "the repo")
    code = f'<code class="src-line">{esc(c["code"])}</code>' if c.get("code") else ""
    note = f' <i>{esc(c["note"])}</i>' if c.get("note") else ""
    return (f'<li><code>{esc(at)}</code>{code}<span>{esc(c.get("title", ""))} '
            f'({esc(c.get("on_path", ""))}). {esc(c.get("fix", ""))}{note}</span></li>')


def audit_intro(audit: dict | None) -> str:
    """Which code was read, and how it was chosen."""
    if not audit:
        return ""
    files = (audit.get("path") or {}).get("files") or []
    how = "".join(f"<li>{esc(h)}</li>" for h in (audit.get("path") or {}).get("how") or [])
    listed = "".join(f'<li><code>{esc(f["file"])}</code> <span>{esc(f["role"])}</span></li>'
                     for f in files[:14])
    more = (f'<p class="src">and {len(files) - 14} more, in code_audit.md</p>'
            if len(files) > 14 else "")
    return (f'<div class="audit"><p><span class="tag">code audit</span> {esc(audit.get("summary", ""))}</p>'
            f'<details><summary>Which code was read, and why</summary><ul class="steps">{how}</ul>'
            f'<ul class="evidence">{listed}</ul>{more}'
            f'<p class="src">{esc(audit.get("limits", ""))}</p></details></div>')


def audit_rest(audit: dict | None) -> str:
    """Audit items that no finding in this trace points at."""
    rest = [it for it in (audit or {}).get("items", []) if not it.get("because")]
    if not rest:
        return ""
    return ("<h3 class='sub'>Also on this path</h3>"
            '<p class="how">No finding in this trace points at these. They are listed because '
            'they sit on the path that was captured.</p>'
            f'<ul class="evidence cands">{"".join(_candidate(it) for it in rest[:10])}</ul>')


def fix_section(plan: dict | None, audit: dict | None = None) -> str:
    """Suggested fixes, in the order to review them."""
    if not plan:
        return ""
    out = [f'<p class="lead">{esc(plan.get("summary", ""))}</p>', audit_intro(audit)]
    items = plan.get("items") or []
    if not items:
        return "".join(out) + audit_rest(audit)
    out.append('<details class="review"><summary>Before acting on any of these</summary><ol>'
               + "".join(f"<li>{esc(step)}</li>" for step in plan.get("review", []))
               + "</ol></details>")
    labels = {"first": "Do first: the capture", "next": "Review next", "later": "Can wait"}
    current = None
    for item in items:
        if item.get("priority") != current:
            current = item.get("priority")
            out.append(f"<h3 class='sub'>{esc(labels.get(current, ''))}</h3>")
        where = (", ".join(f"<code>{esc(w)}</code>" for w in item.get("where") or [])
                 or esc("the candidates below" if item.get("candidates")
                        else item.get("where_hint")
                        or ("the capture, not the code" if item.get("stage") == "capture"
                            else "not known from the trace. Map the slices to code first.")))
        also = "".join(f'<p class="src">Also: {esc(x)}</p>' for x in item.get("also_because") or [])
        skill = (f'<span class="tag">skill: {esc(item["skill"])}</span>' if item.get("skill") else "")
        wait = (f'<p class="src">{esc(item["why_later"])}</p>' if item.get("why_later") else "")
        hold = ('<p class="src">Do not act on this until the capture is fixed.</p>'
                if item.get("blocked_by_capture") else "")
        check = (f'<p><b>Check.</b> {esc(", ".join(item.get("check") or []))}</p>'
                 if item.get("check") else "")
        cands = ""
        if item.get("candidates"):
            extra = (f'<p class="src">and {item["candidates_more"]} more, in code_audit.md</p>'
                     if item.get("candidates_more") else "")
            cands = ('<p><b>In the code on this path.</b> <span class="src">Candidates from a '
                     'pattern match. Read each before acting.</span></p>'
                     f'<ul class="evidence cands">{"".join(_candidate(c) for c in item["candidates"])}'
                     f'</ul>{extra}')
        out.append(
            f'<article class="finding fix p-{esc(str(current))}"><header>'
            f'<span class="qn">{esc(str(item.get("n", "")))}</span>{skill}'
            f'<h4>{esc(item.get("title", ""))}</h4></header>'
            f'<p class="src">Because: {esc(item.get("because", ""))}</p>{also}{wait}{hold}'
            f'<ul class="steps">{"".join(f"<li>{esc(c)}</li>" for c in item.get("change") or [])}</ul>'
            f'<p><b>Where.</b> {where}</p>{cands}{check}</article>')
    return "".join(out) + audit_rest(audit)


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

CSS = """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 var(--font);-webkit-text-size-adjust:100%}
main{max-width:920px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:28px;line-height:1.2;margin:0 0 6px}
h2{font-size:21px;line-height:1.3;margin:0 0 4px}
h3{font-size:16px;margin:0 0 2px}
h3.sub{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:26px 0 10px}
h4{font-size:16px;margin:0;display:inline}
p{margin:0 0 10px}
h1,h2,h3,h4,p,li,td,th,figcaption{overflow-wrap:anywhere}
code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--track);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:14px;margin-bottom:22px;overflow-wrap:anywhere}
section{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:22px;margin:0 0 18px}
.qn{display:inline-block;min-width:26px;height:26px;line-height:26px;text-align:center;border-radius:13px;background:var(--accent);color:var(--on-accent);font-weight:700;font-size:14px;margin-right:8px}
.lead{color:var(--muted);margin:2px 0 16px}
figure{margin:18px 0 0;padding:16px 0 0;border-top:1px solid var(--border)}
figcaption .how,.how{color:var(--muted);font-size:14px;margin:0 0 10px}
.means,.where{font-size:15px;margin:10px 0 0}
.where{color:var(--muted)}
.plot{background:var(--plot);color:var(--plot-text);border:1px solid var(--border);border-radius:calc(var(--radius) * .6);padding:14px}
.plot svg{display:block;width:100%;height:auto;font-family:var(--font)}
.strip{display:flex;height:40px;border-radius:4px;overflow:hidden;font-size:13px;line-height:40px;text-align:center;white-space:nowrap}
.seg{min-width:1px;overflow:hidden}
.seg-empty{border:1px dashed var(--plot-muted);border-radius:0 4px 4px 0;color:var(--plot-text)}
.legend{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px}
.legend li{display:flex;align-items:center;gap:6px}
.legend b{font-weight:600}
.legend i{font-style:normal;color:var(--plot-muted)}
.legend.key{flex-direction:column;color:var(--text)}
.sw{width:12px;height:12px;border-radius:3px;display:inline-block;flex:none;border:1px solid rgba(128,128,128,.45)}
.sw-empty{background:none;border:1px dashed currentColor}
.bars{display:grid;grid-template-columns:minmax(0,38%) minmax(0,1fr) auto;gap:8px 10px;align-items:center;font-size:13px}
.bar-row{display:contents}
.bar-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.bar-track{height:14px;background:var(--plot-grid);border-radius:3px;overflow:hidden}
.bar-fill{display:block;height:100%;min-width:2px;background:var(--accent);border-radius:3px}
.bar-val{white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:600}
.bar-val i{font-style:normal;font-weight:400;color:var(--plot-muted);margin-left:6px}
.fix .qn{background:var(--muted);color:var(--surface)}
.fix.p-first{border-left-color:#8e1b1b}.fix.p-next{border-left-color:var(--accent)}
.steps{margin:6px 0 10px;padding-left:20px;font-size:15px}
.steps li{margin:2px 0;overflow-wrap:anywhere}
.review ol{margin:8px 0 0;padding-left:22px;color:var(--muted)}
.own{display:inline-block;font:700 11px/1.5 var(--font);letter-spacing:.02em;background:var(--accent);color:var(--on-accent);border-radius:999px;padding:0 7px;margin-right:6px;vertical-align:1px;white-space:nowrap}
.evidence{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:6px;font-size:13px;color:var(--muted)}
.evidence li{overflow-wrap:anywhere}
.cands{margin:6px 0 12px;gap:10px}
.cands li{border-left:3px solid var(--border);padding-left:10px}
.cands li>code:first-child{font-weight:600;color:var(--text)}
.cands .src-line{display:block;margin:3px 0;white-space:pre-wrap}
.cands span{display:block;color:var(--muted)}
.audit{border:1px solid var(--border);border-radius:calc(var(--radius) * .6);padding:12px 14px;margin:0 0 14px}
.audit p{margin:0 0 6px}
.means .tag{margin-right:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:0 0 4px}
.tile{border:1px solid var(--border);border-radius:calc(var(--radius) * .6);padding:12px 14px}
.tile b{display:block;font-size:24px;line-height:1.2;font-variant-numeric:tabular-nums}
.tile span{display:block;font-size:13px;color:var(--muted)}
.tile i{display:block;font-style:normal;font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.chip{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.03em;padding:2px 9px;border-radius:999px;color:#fff;margin-right:8px;vertical-align:middle}
.chip.sev-blocker{background:#8e1b1b}.chip.sev-high{background:#b93221}.chip.sev-medium{background:#a85a00}.chip.sev-info{background:#2b6cb0}
.chip.v-trusted,.chip.st-good{background:#1e7d32}.chip.v-directional_only{background:#a85a00}.chip.v-not_trustworthy,.chip.st-bad{background:#b93221}
.chip.st-unk{background:#6b7280}.chip.st-na{background:#6b7280}.chip.st-gap{background:#a85a00}
.chip,.tag{white-space:nowrap}
.tag{font-size:12px;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:1px 8px;margin-right:8px;vertical-align:middle}
.verdict{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:0 0 10px}
.verdict p{margin:0}
.verdict .chip{font-size:14px;padding:4px 12px}
.reasons{margin:0 0 12px;padding-left:20px;color:var(--muted);font-size:14px}
table.checks{width:100%;border-collapse:collapse;font-size:14px}
table.checks th,table.checks td{text-align:left;padding:7px 8px 7px 0;border-top:1px solid var(--border);vertical-align:top;font-weight:400}
table.checks td:last-child{color:var(--muted)}
.finding{border:1px solid var(--border);border-left:4px solid var(--muted);border-radius:calc(var(--radius) * .6);padding:14px 16px;margin:14px 0 0}
.finding.sev-blocker{border-left-color:#8e1b1b}.finding.sev-high{border-left-color:#b93221}.finding.sev-medium{border-left-color:#a85a00}.finding.sev-info{border-left-color:#2b6cb0}
.finding header{margin-bottom:8px}
.finding p{font-size:15px}
.finding .do{margin-bottom:6px}
.finding .src{font-size:13px;color:var(--muted);margin:0}
.index{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:minmax(0,1fr);gap:8px}
.index li,h4,.finding p,figure p,.tile b{overflow-wrap:anywhere}
.index a{color:inherit;text-decoration:none;border-bottom:1px solid var(--border)}
.index a:hover{border-bottom-color:var(--accent)}
.empty{color:var(--muted);font-size:15px;margin:0}
details{margin-top:14px;font-size:14px}
summary{cursor:pointer;color:var(--muted)}
footer{color:var(--muted);font-size:13px;margin-top:22px}
footer p{margin:0 0 6px}
@media (max-width:560px){main{padding:20px 12px 48px}section{padding:16px}.bars{grid-template-columns:minmax(0,1fr) auto}.bar-name{grid-column:1 / -1;margin-bottom:-4px}.bar-val i{display:none}table.checks td:last-child{display:none}.plot{padding:10px;overflow-x:auto}.plot svg{min-width:520px}.finding h4{display:block;margin-top:8px}}
@media print{section,.plot{break-inside:avoid}}
"""


def css_vars(v: dict) -> str:
    font = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    if v.get("font"):
        font = f"'{v['font']}'," + font

    def block(d):
        return (f"--bg:{d['bg']};--surface:{d['surface']};--text:{d['text']};--muted:{d['muted']};"
                f"--border:{d['border']};--track:{d['track']};--accent:{d['accent']};"
                f"--on-accent:{d['on_accent']};--plot:{d['plot']};--plot-text:{d['plot_text']};"
                f"--plot-grid:{d['plot_grid']};--plot-muted:{d['plot_muted']};")
    return (f":root{{color-scheme:light dark;--radius:{int(v['radius'])}px;--font:{font};"
            f"{block(v['light'])}}}"
            f"@media (prefers-color-scheme:dark){{:root{{{block(v['dark'])}}}}}")


def render(out_dir: str, theme: dict | None = None, code_map: dict | None = None,
           extra_findings: list | None = None) -> str:
    """The whole page as a string. Raises only if findings.json is unreadable."""
    with open(os.path.join(out_dir, "findings.json")) as fh:
        data = json.load(fh)
    pkg = data.get("pkg") or "*"
    hs = data.get("health_status")
    findings = data.get("findings") or []
    if extra_findings:
        order = {"blocker": 0, "high": 1, "medium": 2, "info": 3}
        findings = sorted(findings + list(extra_findings),
                          key=lambda x: order.get(x.get("severity"), 9))
    for i, f in enumerate(findings, 1):
        f["_n"] = i
    by_section: dict[str, list[dict]] = {}
    for f in findings:
        by_section.setdefault(section_of(f), []).append(f)
    vars_, notes = resolve_theme(theme)
    cm = data.get("code_map") or code_map

    index = ""
    if findings:
        index = ('<section><h2>Findings, worst first</h2><ol class="index">' + "".join(
            f'<li><span class="chip sev-{esc(f.get("severity", "info"))}">'
            f'{esc(SEV_LABEL.get(f.get("severity"), "Info"))}</span>'
            f'<a href="#f{f["_n"]}">{esc(f.get("title", ""))}</a></li>' for f in findings)
            + "</ol></section>")
    else:
        index = ('<section><h2>Findings</h2><p class="empty">Nothing crossed a threshold. Either '
                 'the app is healthy in this trace, or the trace does not cover the interaction '
                 'you care about.</p></section>')

    def section(n, key, title, lead, body):
        return (f'<section id="{key}"><h2><span class="qn">{n}</span>{esc(title)}</h2>'
                f'<p class="lead">{esc(lead)}</p>{body}'
                f'{finding_cards(by_section.get(key, []))}</section>')

    body = [
        section(1, "q1", "Can this trace be trusted?",
                "Dropped data, a debuggable build or a missing data source makes every number "
                "below it meaningless, so this comes first.", q1_trust(hs)),
        index,
        section(2, "q2", "Was the main thread running or waiting?",
                "Running means the app's code is slow. Waiting means it was blocked, and "
                "profiling the app's own code will find nothing.", q2_thread_state(out_dir, pkg)),
        section(3, "q3", "Which frames were late, and who is to blame?",
                "Every frame has a deadline. The frame timeline says whether the app or the "
                "system compositor missed it.", q3_frames(out_dir, pkg, hs)),
        section(4, "q4", "Where did the time go?",
                "Only now is it worth ranking slices.", q4_time(out_dir, pkg, hs, cm)),
    ]
    fixes = fix_section(data.get("fix_plan"), data.get("code_audit"))
    if fixes:
        body.append('<section id="fixes"><h2><span class="qn">5</span>What to fix, in order</h2>'
                    '<p class="how">Suggestions drawn from this one trace, for a person or an '
                    'agent to review. They are not instructions.</p>' + fixes + "</section>")

    trace = os.path.basename(str(data.get("trace", "")))
    meta = " · ".join(x for x in (
        f"trace <code>{esc(trace)}</code>" if trace else "",
        f"package <code>{esc(pkg)}</code>",
        f"capture mode <code>{esc(str(data.get('mode') or (hs or {}).get('mode') or 'unknown'))}</code>",
    ) if x)
    if theme and theme.get("sources") and vars_["themed"]:
        lead = ("The app declares no theme tokens, so page colours are a guess from the colours "
                "its UI code uses most, in " if theme.get("inferred")
                else "Page colours come from the app's own tokens in ")
        themed = lead + ", ".join(f"<code>{esc(s)}</code>" for s in theme["sources"][:4]) + "."
    elif theme is not None:
        themed = "No usable colour tokens were found in the app repository, so the neutral theme is used."
    else:
        themed = "Neutral theme. Pass --app-repo to use the app's own colours."
    foot = [f"<p>{themed}</p>"] + [f"<p>{esc(n)}.</p>" for n in notes]
    foot.append("<p>Charts and numbers are measured from the trace. Lines marked "
                "<span class=\"tag\">inference</span> are this tool's reading of them, and a "
                "reading can be wrong.</p>")
    foot.append("<p>Thread state and frame colours follow the Perfetto UI, so this page and the "
                "trace viewer read the same way. Charts are drawn from the query results in "
                "<code>csv/</code>. The full tables are in <code>report.md</code>.</p>")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:\">"
        f"<title>Perfetto triage: {esc(pkg)}</title><style>{css_vars(vars_)}{CSS}</style></head>"
        f"<body><main><h1>Perfetto triage</h1><p class=\"meta\">{meta}</p>"
        f"{''.join(body)}<footer>{''.join(foot)}</footer></main></body></html>")


def write(out_dir: str, app_repo: str | None = None, theme_file: str | None = None,
          dest: str | None = None) -> str:
    theme = None
    if theme_file:
        with open(theme_file) as fh:
            theme = json.load(fh)
    elif app_repo:
        theme = discover_theme(app_repo)
    code_map, extra = None, None
    if app_repo:
        try:
            with open(os.path.join(out_dir, "findings.json")) as fh:
                stored = json.load(fh)
            if not stored.get("code_map"):
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    "code_map", os.path.join(os.path.dirname(os.path.abspath(__file__)), "code_map.py"))
                cmod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(cmod)
                code_map = cmod.build(cmod.load_results(out_dir), app_repo, stored.get("pkg") or "")
                extra = cmod.findings(code_map)
        except Exception:  # the ownership marks are optional
            code_map, extra = None, None
    page = render(out_dir, theme, code_map, extra)
    dest = dest or os.path.join(out_dir, "report.html")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(page)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", nargs="?", default="triage-out",
                    help="a directory written by triage.py (default ./triage-out)")
    ap.add_argument("--app-repo", default=None,
                    help="the app's source tree. Its colour tokens (Compose colour scheme, "
                         "res/values/colors.xml, shapes, font files) theme the page.")
    ap.add_argument("--theme", default=None,
                    help="a theme JSON to use as is, in the shape --print-theme prints")
    ap.add_argument("--print-theme", action="store_true",
                    help="print the tokens found in --app-repo and exit")
    ap.add_argument("--out", default=None, help="output file (default <out_dir>/report.html)")
    args = ap.parse_args()
    if args.print_theme:
        print(json.dumps(discover_theme(args.app_repo or "."), indent=2))
        return
    if not os.path.exists(os.path.join(args.out_dir, "findings.json")):
        raise SystemExit(f"no findings.json in {args.out_dir}. Run triage.py first.")
    dest = write(args.out_dir, args.app_repo, args.theme, args.out)
    print(f"Visual report: {dest}")


if __name__ == "__main__":
    main()
