#!/usr/bin/env python3
"""Build bogie-go.com into _site/.

Pages are plain HTML with a few markers that this script fills in:
  <!-- @header --> / <!-- @footer -->     the shared _includes/*.html
  <!-- @docs-start --> ... <!-- @docs-end -->  docs pages: sidebar + prev/next
  {{VERSION}} / {{VERSION_NUM}} / {{SECTION}}

The version is the latest tag from the Go module proxy. If that fetch fails,
BOGIE_VERSION or the last known version in FALLBACK_VERSION is used.
Standard library only:  python3 build.py   (then serve _site/)
"""
import html, json, os, shutil, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"
PROXY = "https://proxy.golang.org/github.com/bogie-go/bogie/@latest"
FALLBACK_VERSION = "v0.2.8"
SKIP = {"_site", "_includes", "build.py", "README.md", ".git", ".github", ".claude"}

# Sidebar order is also prev/next order.
NAV = [
    ("The App", [("getting-started", "Getting started"),
                 ("models", "Models and migrations"),
                 ("seeds", "Seeds"),
                 ("controllers", "Resources, controllers and routing"),
                 ("static-files", "Static files and error pages"),
                 ("authentication", "Authentication"),
                 ("services", "Services"),
                 ("jobs", "Background jobs")]),
    ("Infra", [("configuration", "Configuration and credentials"),
               ("own-postgres", "Your own Postgres"),
               ("testing", "Testing and CI"),
               ("deploying", "Deploying"),
               ("upgrading", "Upgrading an app")]),
    ("Reference", [("conventions", "Conventions and agents"),
                   ("from-rails", "From Rails"),
                   ("commands", "Command reference")]),
]
FLAT = [(slug, title, sec) for sec, items in NAV for slug, title in items]


def latest_version():
    if os.environ.get("BOGIE_VERSION"):
        return os.environ["BOGIE_VERSION"]
    try:
        with urllib.request.urlopen(PROXY, timeout=10) as r:
            v = json.load(r)["Version"]
        if v.startswith("v"):
            return v
    except Exception as e:  # network, JSON, anything: never fail the build
        print(f"warning: version fetch failed ({e}); using {FALLBACK_VERSION}", file=sys.stderr)
    return FALLBACK_VERSION


def sidebar(current):
    out = ['<input type="checkbox" id="docnav-toggle" class="docnav-toggle">',
           '<label for="docnav-toggle" class="docnav-button">Guide menu</label>',
           '<aside class="docnav"><nav aria-label="Guide">',
           '<a class="docnav-home%s" href="/docs/">All guides</a>' % (" current" if current == "index" else "")]
    for sec, items in NAV:
        out.append(f'<p class="docnav-h">{html.escape(sec)}</p><ul>')
        for slug, title in items:
            cur = ' class="current" aria-current="page"' if slug == current else ""
            out.append(f'<li><a{cur} href="/docs/{slug}.html">{html.escape(title)}</a></li>')
        out.append("</ul>")
    out.append("</nav></aside>")
    return "\n".join(out)


def prevnext(current):
    slugs = [s for s, _, _ in FLAT]
    i = slugs.index(current) if current in slugs else -1
    prev = FLAT[i - 1] if i > 0 else None
    nxt = FLAT[i + 1] if i + 1 < len(FLAT) else None
    def cell(cls, label, item):
        if not item:
            return f'<div class="{cls}"></div>'
        return f'<div class="{cls}"><span class="label">{label}</span><a href="/docs/{item[0]}.html">{html.escape(item[1])}</a></div>'
    return f'<footer class="guide-nav">\n  {cell("prev", "Previous", prev)}\n  {cell("next", "Next", nxt)}\n</footer>'


def render(path, text, inc, version):
    text = text.replace("<!-- @header -->", inc["header"]).replace("<!-- @footer -->", inc["footer"])
    if "<!-- @docs-start -->" in text:
        slug = path.stem
        section = next((sec for s, _, sec in FLAT if s == slug), "Guides")
        text = text.replace("<!-- @docs-start -->",
                            f'<div class="doclayout">\n{sidebar(slug)}\n<div class="measure doc-main">', 1)
        text = text.replace("<!-- @docs-end -->", f"{prevnext(slug)}\n</div>\n</div>", 1)
        text = text.replace("{{SECTION}}", section)
    return text.replace("{{VERSION_NUM}}", version.lstrip("v")).replace("{{VERSION}}", version)


def main():
    version = latest_version()
    inc = {n: (ROOT / "_includes" / f"{n}.html").read_text().rstrip("\n") for n in ("header", "footer")}
    if OUT.exists():
        shutil.rmtree(OUT)
    for src in ROOT.rglob("*"):
        rel = src.relative_to(ROOT)
        if rel.parts[0] in SKIP or src.is_dir():
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".html" and rel.parts[0] != "design":
            out = render(src, src.read_text(), inc, version)
            left = [m for m in ("{{VERSION}}", "{{VERSION_NUM}}", "{{SECTION}}", "<!-- @") if m in out]
            if left:
                sys.exit(f"error: unfilled {left} in {rel}")
            dst.write_text(out)
        else:
            shutil.copy2(src, dst)
    print(f"built {OUT.relative_to(ROOT)}/ with Bogie {version}")


if __name__ == "__main__":
    main()
