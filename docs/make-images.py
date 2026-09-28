#!/usr/bin/env python3
"""Regenerate the README screenshots and the GitHub social preview.

    pip install playwright && playwright install chromium
    python3 docs/make-images.py            # writes docs/*.png

Everything is rendered from real `jthread-cpu` output on examples/, so the
images cannot drift from what the tool prints.  This is a maintainer tool; it
is not needed to use jthread-cpu.

Fonts: the terminal shots use DejaVu Sans Mono (install fonts-dejavu); the
social preview loads Inter + JetBrains Mono from Google Fonts, or from
--fonts DIR containing inter-latin-{400,500,800}-normal.woff2 and
jetbrains-mono-latin-{400,500,700}-normal.woff2.
"""

import argparse
import html
import os
import pty
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
TOOL = os.path.join(ROOT, "jthread-cpu")

SHOT_ARGS = ["--jstack-file", "examples/dump.txt", "--top-file", "examples/top.txt",
             "--cpus", "8", "-n", "10", "-f", "6", "-s", "1"]
SHOT_TITLE = "~ jthread-cpu " + " ".join(a.replace("examples/", "") for a in SHOT_ARGS)
COLUMNS = 100

THEMES = {
    "mocha": {"bg": "#1e1e2e", "bar": "#181825", "fg": "#cdd6f4", "title": "#7f849c", "shadow": "0 30px 80px rgba(0,0,0,.45)"},
    "latte": {"bg": "#eff1f5", "bar": "#e6e9ef", "fg": "#4c4f69", "title": "#8c8fa1", "shadow": "0 30px 80px rgba(76,79,105,.25)"},
}


def run_in_pty(argv):
    """Run the tool with a real TTY so it colours its output like a terminal."""
    env = dict(os.environ, COLORTERM="truecolor", COLUMNS=str(COLUMNS), LINES="200", LANG="C.UTF-8")
    env.pop("NO_COLOR", None)
    master, slave = pty.openpty()
    p = subprocess.Popen([sys.executable, TOOL] + argv, stdout=slave, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, cwd=ROOT, env=env)
    os.close(slave)
    chunks = []
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    p.wait()
    os.close(master)
    return b"".join(chunks).decode("utf-8").replace("\r\n", "\n")


SGR = re.compile(r"\x1b\[([0-9;]*)m")


def ansi_to_html(text):
    out, fg, bg, bold = [], None, None, False
    pos = 0

    def span(s):
        if not s:
            return
        style = []
        if fg:
            style.append("color:%s" % fg)
        if bg:
            style.append("background:%s" % bg)
        if bold:
            style.append("font-weight:700")
        s = html.escape(s)
        out.append('<span style="%s">%s</span>' % (";".join(style), s) if style else s)

    for m in SGR.finditer(text):
        span(text[pos:m.start()])
        pos = m.end()
        codes = [int(c) for c in m.group(1).split(";") if c] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                fg, bg, bold = None, None, False
            elif c == 1:
                bold = True
            elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                col = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5])
                if c == 38:
                    fg = col
                else:
                    bg = col
                i += 4
            i += 1
    span(text[pos:])
    return "".join(out)


def terminal_page(body, theme):
    t = THEMES[theme]
    return """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:transparent}
.win{display:inline-block;margin:40px;border-radius:14px;overflow:hidden;background:%(bg)s;box-shadow:%(shadow)s}
.bar{height:40px;background:%(bar)s;display:flex;align-items:center;padding:0 16px;position:relative}
.dot{width:13px;height:13px;border-radius:50%%;margin-right:9px}
.t{position:absolute;left:0;right:0;text-align:center;font:13px 'DejaVu Sans Mono',monospace;color:%(title)s}
pre{margin:0;padding:18px 22px 26px;font:14px/1.42 'DejaVu Sans Mono',monospace;color:%(fg)s;white-space:pre}
</style></head><body><div class="win" id="w"><div class="bar"><span class="dot" style="background:#f38ba8"></span>
<span class="dot" style="background:#f9e2af"></span><span class="dot" style="background:#a6e3a1"></span>
<div class="t">%(title_text)s</div></div><pre>%(body)s</pre></div></body></html>""" % dict(
        t, body=body, title_text=html.escape(SHOT_TITLE))


def preview_page(font_css):
    rows = [
        ("94.2", "#f38ba8", 100, "61%", "8421", "8455", "0x2107", "RUNNABLE", "#a6e3a1", "http-nio-8080-exec-7", True,
         "com.acme.order.PricingEngine.recalculate(PricingEngine.java:212)"),
        ("41.8", "#fab387", 44, "27%", "8421", "8460", "0x210c", "RUNNABLE", "#a6e3a1", "scheduled-refresh-2", False, None),
        ("18.3", "#f9e2af", 19, "12%", "8421", "8433", "0x20f1", "JVM_INTERNAL", "#b4befe", "GC Thread#3", False, None),
    ]
    trs = []
    for cpu, col, w, share, pid, tid, nid, st, stc, name, bold, frame in rows:
        trs.append(
            '<tr><td class="num" style="color:%s">%s</td><td><div class="track"><div class="fill" style="width:%d%%;background:%s"></div></div></td>'
            '<td class="dim r">%s</td><td class="dim r">%s</td><td class="r">%s</td><td>%s</td><td style="color:%s">%s</td><td class="%s">%s</td></tr>'
            % (col, cpu, w, col, share, pid, tid, nid, stc, st, "b" if bold else "", name))
        if frame:
            trs.append('<tr><td></td><td colspan="7" class="dim frame">&#8627; %s</td></tr>' % html.escape(frame))
    bars = [8, 11, 14, 6, 12, 17, 60, 44, 9, 7, 8, 10, 13, 16, 11, 34, 12, 8, 9, 7, 10, 12, 9, 8, 11, 21, 9, 7]
    colors = {6: "#f38ba8", 7: "#f38ba8", 15: "#fab387", 25: "#f9e2af"}
    spark = "".join('<i style="height:%dpx;background:%s"></i>' % (h * 1.6, colors.get(i, "#45475a")) for i, h in enumerate(bars))
    return """<!doctype html><html><head><meta charset="utf-8"><style>%(fonts)s
*{box-sizing:border-box}html,body{margin:0}
body{width:1280px;height:640px;overflow:hidden;font-family:Inter,'DejaVu Sans',sans-serif;color:#cdd6f4;
background:radial-gradient(900px 500px at 0%% 0%%,#3b3252 0%%,rgba(59,50,82,0) 70%%),radial-gradient(700px 400px at 100%% 100%%,#2a3048 0%%,rgba(42,48,72,0) 70%%),#15151f}
.mono{font-family:'JetBrains Mono','DejaVu Sans Mono',monospace}
.kicker{position:absolute;left:60px;top:52px;font:600 15px 'JetBrains Mono',monospace;letter-spacing:.28em;color:#cba6f7}
h1{position:absolute;left:56px;top:78px;margin:0;font:800 84px/1 Inter,sans-serif;letter-spacing:-.035em;color:#cdd6f4}
.sub{position:absolute;left:60px;top:170px;font:400 26px Inter,sans-serif;color:#a6adc8}
.pills{position:absolute;left:60px;top:220px;display:flex;gap:14px;align-items:center;font:500 16px 'JetBrains Mono',monospace}
.pill{padding:9px 14px;border:1px solid #45475a;border-radius:7px;background:rgba(17,17,27,.55)}
.pill.act{border-color:#cba6f7;color:#cba6f7}.op{color:#cba6f7;font-size:18px}
.spark{position:absolute;right:60px;top:72px;display:flex;gap:6px;align-items:flex-end;height:100px}
.spark i{display:block;width:9px;border-radius:2px}
.sparkl{position:absolute;right:60px;top:42px;font:400 13px 'JetBrains Mono',monospace;color:#6c7086;letter-spacing:.03em}
.term{position:absolute;left:60px;right:60px;top:295px;height:262px;border:1px solid #45475a;border-radius:12px;background:rgba(24,24,37,.92)}
.tb{height:42px;border-bottom:1px solid #313244;display:flex;align-items:center;padding:0 16px;font:400 14px 'JetBrains Mono',monospace;color:#9399b2}
.tb .d{width:11px;height:11px;border-radius:50%%;margin-right:8px}.tb .p{color:#a6e3a1;margin:0 8px 0 10px}
.meta{padding:14px 42px 8px;font:400 15px 'JetBrains Mono',monospace;color:#7f849c}.meta b{color:#fab387;font-weight:500}
table{margin:4px 0 0 40px;border-collapse:collapse;font:400 15px 'JetBrains Mono',monospace}
th{font-weight:400;color:#7f849c;text-align:left;padding:3px 18px 3px 0}td{padding:2px 18px 2px 0;white-space:nowrap}
.r{text-align:right}.dim{color:#7f849c}.b{font-weight:700;color:#e6e9ff}.num{font-weight:700;text-align:right}
.track{width:140px;height:17px;background:repeating-linear-gradient(90deg,#585b70 0 2px,transparent 2px 9px);background-size:9px 2px;background-repeat:repeat-x;background-position:0 9px}
.fill{height:17px;border-radius:2px}.frame{padding-top:0;padding-bottom:6px}
.checks{position:absolute;left:60px;top:574px;display:flex;gap:30px;font:400 15px 'JetBrains Mono',monospace;color:#9399b2}
.checks span:before{content:"\\2713";color:#a6e3a1;margin-right:12px}
</style></head><body>
<div class="kicker">JVM THREAD &middot; CPU PROFILER</div>
<h1>jthread-cpu</h1>
<div class="sub">Which Java thread is burning your CPU &mdash; and what it&rsquo;s running.</div>
<div class="pills"><span class="pill">jstack <b style="color:#f9e2af">nid=0x2107</b></span><span class="op">&#8904;</span>
<span class="pill">top -H <b style="color:#89b4fa">TID 8455</b></span><span class="op">&rarr;</span><span class="pill act">one line you can act on</span></div>
<div class="sparkl">per-thread CPU &middot; 312 threads</div><div class="spark">%(spark)s</div>
<div class="term"><div class="tb"><span class="d" style="background:#f38ba8"></span><span class="d" style="background:#f9e2af"></span>
<span class="d" style="background:#a6e3a1"></span><span class="p">$</span>jthread-cpu -p 8421 -d 5</div>
<div class="meta">pid 8421 &middot; 312 threads &middot; window 5.0s &middot; total CPU <b>154.3%%</b> = 1.54 cores &middot; <b>19.3%%</b> of 8 cores</div>
<table><tr><th class="r">%%CPU</th><th></th><th class="r">%%JVM</th><th class="r">PID</th><th class="r">TID</th><th>NID(dump)</th><th>STATE</th><th>THREAD NAME</th></tr>
%(rows)s</table></div>
<div class="checks"><span>Linux &amp; macOS</span><span>stdlib only, no deps</span><span>no agent, no restart</span><span>repeated sampling</span><span>text or versioned JSON</span></div>
</body></html>""" % {"fonts": font_css, "spark": spark, "rows": "\n".join(trs)}


def font_css(fonts_dir):
    if not fonts_dir:
        return ("@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;800"
                "&family=JetBrains+Mono:wght@400;500;600;700&display=swap');")
    faces = []
    for fam, stem, weights in (("Inter", "inter", (400, 500, 600, 700, 800)),
                               ("JetBrains Mono", "jetbrains-mono", (400, 500, 600, 700))):
        for w in weights:
            path = os.path.join(os.path.abspath(fonts_dir), "%s-latin-%d-normal.woff2" % (stem, w))
            if os.path.exists(path):
                faces.append("@font-face{font-family:'%s';font-weight:%d;src:url('file://%s')}" % (fam, w, path))
    return "".join(faces)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", help="directory with local woff2 fonts (see docstring)")
    ap.add_argument("--out", default=DOCS)
    args = ap.parse_args()
    from playwright.sync_api import sync_playwright

    pages = {}
    for theme in ("mocha", "latte"):
        ansi = run_in_pty(SHOT_ARGS + ["--theme", theme])
        pages["screenshot.png" if theme == "mocha" else "screenshot-latte.png"] = terminal_page(ansi_to_html(ansi.strip("\n")), theme)

    tmp = os.path.join(args.out, ".render.html")
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, page_html in pages.items():
            pg = b.new_page(device_scale_factor=2, viewport={"width": 1100, "height": 800})
            with open(tmp, "w") as fh:
                fh.write(page_html)
            pg.goto("file://" + tmp)
            pg.wait_for_timeout(200)
            pg.locator("#w").screenshot(path=os.path.join(args.out, name), omit_background=True)
            pg.close()
        pg = b.new_page(device_scale_factor=1, viewport={"width": 1280, "height": 640})
        with open(tmp, "w") as fh:
            fh.write(preview_page(font_css(args.fonts)))
        pg.goto("file://" + tmp)
        pg.wait_for_timeout(800)
        pg.screenshot(path=os.path.join(args.out, "jthread-cpu-social-preview.png"))
        b.close()
    os.remove(tmp)
    print("wrote screenshot.png, screenshot-latte.png, jthread-cpu-social-preview.png to", args.out)


if __name__ == "__main__":
    main()
