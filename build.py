#!/usr/bin/env python3
"""Build bogie-go.com into _site/.

Pages are plain HTML with a few markers that this script fills in:
  <!-- @header --> / <!-- @footer -->     the shared _includes/*.html
  <!-- @docs-start --> ... <!-- @docs-end -->  docs pages: sidebar + prev/next
  <!-- @script -->                         the one inline script (SCRIPT below), in the footer
  {{VERSION}} / {{VERSION_NUM}} / {{SECTION}}

Docs pages also get an id and a "#" link on every <h2>/<h3>, and an "On this
page" list of the <h2>s, written into the HTML here (no JS needed for those).

The version is the latest tag from the Go module proxy. If that fetch fails,
BOGIE_VERSION or the last known version in FALLBACK_VERSION is used.

sitemap.xml's <lastmod> for each page is the date of the last git commit that
touched the page's source file, and so is each page's JSON-LD "dateModified".
Without git (or history), the dates already written in sitemap.xml and in the
page are kept.

Standard library only:  python3 build.py   (then serve _site/)
"""
import html, json, os, re, shutil, subprocess, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"
PROXY = "https://proxy.golang.org/github.com/bogie-go/bogie/@latest"
FALLBACK_VERSION = "v0.6.8"
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
                   ("commands", "Command reference"),
                   ("releases", "Releases"),
                   ("from-rails", "Rails equivalents")]),
]
FLAT = [(slug, title, sec) for sec, items in NAV for slug, title in items]


# The one script on the site, inlined by the footer's <!-- @script --> marker.
# Nothing is fetched. Home: the terminal's Copy button. Guides: a Copy button
# on every code block.
SCRIPT = r"""<script>
// The one script on the site: Copy buttons. Nothing is fetched.
function copyButton(b, text) {
  b.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text()); b.textContent = 'Copied'; }
    catch (e) { b.textContent = 'Select it'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1600);
  });
}
for (const b of document.querySelectorAll('button.copy')) copyButton(b, () => b.dataset.copy);
for (const pre of document.querySelectorAll('.doc-main pre')) {
  const wrap = document.createElement('div'), b = document.createElement('button');
  wrap.className = 'codewrap';
  b.type = 'button'; b.className = 'copy copy-pre'; b.textContent = 'Copy';
  b.setAttribute('aria-label', 'Copy this code block');
  pre.replaceWith(wrap); wrap.append(pre, b);
  copyButton(b, () => pre.textContent.replace(/\n$/, ''));
}
</script>"""

HEADING = re.compile(r'<h([23])((?: id="[^"]*")?)>(.*?)</h\1>', re.S)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", html.unescape(re.sub(r"<[^>]+>", "", s)).lower()).strip("-") or "section"


def anchors(article, slug):
    """Give every <h2>/<h3> an id and a hover "#" link; add an "On this page"
    list of the <h2>s after the intro paragraph. Releases keep their own ids."""
    if slug in ("releases", "index"):
        return article
    used = set(re.findall(r'\bid="([^"]+)"', article))
    toc = []

    def fix(m):
        level, idattr, inner = m.group(1), m.group(2), m.group(3)
        hid = idattr[5:-1] if idattr else None
        if not hid:
            base = hid = slugify(inner)
            n = 2
            while hid in used:
                hid, n = f"{base}-{n}", n + 1
            used.add(hid)
        if level == "2":
            toc.append((hid, re.sub(r"<[^>]+>", "", inner)))
        return (f'<h{level} id="{hid}">{inner}'
                f'<a class="anchor" href="#{hid}" aria-label="Link to this section"></a></h{level}>')

    article = HEADING.sub(fix, article)
    if len(toc) >= 3:
        items = "".join(f'<li><a href="#{i}">{t}</a></li>' for i, t in toc)
        nav = (f'<nav class="onpage" aria-label="On this page"><p class="onpage-h">On this page</p>'
               f'<ul>{items}</ul></nav>')
        # after the first paragraph that follows the <h1> (the intro)
        article = re.sub(r"(</h1>\s*<p>.*?</p>)", lambda m: m.group(1) + "\n" + nav, article, count=1, flags=re.S)
    return article


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


def git_date(rel):
    """YYYY-MM-DD of the last commit touching rel, or None without git/history."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", str(rel)],
                             cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    d = out.stdout.strip()
    return d if out.returncode == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None


def sitemap(text):
    """Set each <url>'s <lastmod> to its source file's last commit date."""
    def fix(m):
        path = m.group(1).split("bogie-go.com", 1)[-1].lstrip("/")
        src = path + "index.html" if path == "" or path.endswith("/") else path
        d = git_date(src) if (ROOT / src).is_file() else None
        return m.group(0) if not d else re.sub(r"<lastmod>[^<]*</lastmod>", f"<lastmod>{d}</lastmod>", m.group(0))
    return re.sub(r"<url><loc>([^<]+)</loc>.*?</url>", fix, text)


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
    d = git_date(path.relative_to(ROOT))
    if d:  # same source as the sitemap's <lastmod>; the typed date is the fallback
        text = re.sub(r'"dateModified": "\d{4}-\d{2}-\d{2}"', f'"dateModified": "{d}"', text)
    text = text.replace("<!-- @header -->", inc["header"]).replace("<!-- @footer -->", inc["footer"])
    text = text.replace("<!-- @script -->", SCRIPT)
    if "<!-- @docs-start -->" in text:
        slug = path.stem
        section = next((sec for s, _, sec in FLAT if s == slug), "Guides")
        text = text.replace("<!-- @docs-start -->",
                            f'<div class="doclayout">\n{sidebar(slug)}\n<div class="measure doc-main">', 1)
        text = re.sub(r"<article>.*?</article>", lambda m: anchors(m.group(0), slug), text, count=1, flags=re.S)
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
        elif rel == Path("sitemap.xml"):
            dst.write_text(sitemap(src.read_text()))
        else:
            shutil.copy2(src, dst)
    print(f"built {OUT.relative_to(ROOT)}/ with Bogie {version}")


if __name__ == "__main__":
    main()
