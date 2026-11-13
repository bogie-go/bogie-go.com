#!/usr/bin/env python3
"""Build bogie-go.com into _site/.

Pages are plain HTML with a few markers that this script fills in:
  <!-- @header --> / <!-- @footer -->     the shared _includes/*.html
  <!-- @docs-start --> ... <!-- @docs-end -->  docs pages: sidebar + prev/next
  <!-- @script -->                         the one inline script (SCRIPT below), in the footer
  <!-- @supporters -->                     homepage book sponsors, from sponsors.json
  {{VERSION}} / {{VERSION_NUM}} / {{SECTION}}

Docs pages also get an id and a "#" link on every <h2>/<h3>, and an "On this
page" list of the <h2>s, written into the HTML here (no JS needed for those).

The version is the latest tag from the Go module proxy. If that fetch fails,
BOGIE_VERSION or the last known version in FALLBACK_VERSION is used.

Every docs page also gets a Markdown copy, _site/docs/<slug>.md, converted here
from the rendered <article> (so it can never drift from the HTML). The .md files
are not in sitemap.xml; llms.txt lists them at its <!-- @markdown-copies --> marker.

sitemap.xml's <lastmod> for each page is the date of the last git commit that
touched the page's source file, and so is each page's JSON-LD "dateModified".
Without git (or history), the dates already written in sitemap.xml and in the
page are kept.

Standard library only:  python3 build.py   (then serve _site/)
"""
import html, json, os, re, shutil, subprocess, sys, urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_site"
PROXY = "https://proxy.golang.org/github.com/bogie-go/bogie/@latest"
FALLBACK_VERSION = "v0.7.0"
SKIP = {"_site", "_includes", "build.py", "README.md", ".git", ".github", ".claude"}

# Sidebar order is also prev/next order.
NAV = [
    ("The App", [("getting-started", "Getting started"),
                 ("models", "Models and migrations"),
                 ("seeds", "Seeds"),
                 ("controllers", "Resources, controllers and routing"),
                 ("static-files", "Static files and error pages"),
                 ("authentication", "Authentication"),
                 ("authorization", "Authorization"),
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


def read_sponsors(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"error: {path.name}: {e}")
    return data


def parse_sponsors(data, root):
    """Validate the book-sponsor list. Empty is valid and renders nothing."""
    if not isinstance(data, list):
        sys.exit("error: sponsors.json must be a list of sponsor objects")
    sponsors = []
    for i, item in enumerate(data):
        where = f"sponsors.json[{i}]"
        if not isinstance(item, dict):
            sys.exit(f"error: {where} must be an object")
        tier = item.get("tier")
        name = item.get("name")
        url = item.get("url")
        logo = item.get("logo")
        if not isinstance(tier, str) or tier.strip() not in ("presenting", "supporting"):
            sys.exit(f'error: {where}.tier must be "presenting" or "supporting"')
        tier = tier.strip()
        for key, val in (("name", name), ("url", url), ("logo", logo)):
            if not isinstance(val, str) or not val.strip():
                sys.exit(f"error: {where}.{key} must be a non-empty string")
        name, url, logo = name.strip(), url.strip(), logo.strip()
        if not url.startswith(("https://", "http://")):
            sys.exit(f"error: {where}.url must be an http(s) URL")
        logo_path = Path(logo)
        if logo_path.is_absolute() or ".." in logo_path.parts:
            sys.exit(f"error: {where}.logo must be a relative path inside this repo")
        full = (root / logo_path).resolve()
        if not full.is_relative_to(root.resolve()) or not full.is_file():
            sys.exit(f"error: {where}.logo not found: {logo}")
        sponsors.append({"tier": tier, "name": name, "url": url, "logo": logo})
    presenting = sum(s["tier"] == "presenting" for s in sponsors)
    supporting = sum(s["tier"] == "supporting" for s in sponsors)
    if presenting > 1:
        sys.exit("error: sponsors.json: only one presenting sponsor")
    if supporting > 3:
        sys.exit("error: sponsors.json: at most three supporting sponsors")
    return sponsors


def load_sponsors():
    return parse_sponsors(read_sponsors(ROOT / "sponsors.json"), ROOT)


def _sponsor_img(sponsor, height):
    return (f'<img src="{html.escape(sponsor["logo"], quote=True)}" '
            f'alt="{html.escape(sponsor["name"], quote=True)}" height="{height}">')


def supporters_markup(sponsors):
    """Homepage Supporters block. Empty list, and a tier with nobody in it, render nothing."""
    if not sponsors:
        return ""
    parts = []
    presenting = [s for s in sponsors if s["tier"] == "presenting"]
    supporting = [s for s in sponsors if s["tier"] == "supporting"]
    if presenting:
        s = presenting[0]
        parts.append(
            '    <div class="tier presenting">\n'
            '      <p class="eyebrow">Presenting Sponsor</p>\n'
            f'      <a href="{html.escape(s["url"], quote=True)}">\n'
            f'        {_sponsor_img(s, 64)}\n'
            f'        <span class="name" aria-hidden="true">{html.escape(s["name"])}</span>\n'
            '      </a>\n'
            '    </div>')
    if supporting:
        items = "\n".join(
            f'        <li><a href="{html.escape(s["url"], quote=True)}">{_sponsor_img(s, 32)}</a></li>'
            for s in supporting)
        parts.append(
            '    <div class="tier supporting">\n'
            '      <p class="eyebrow">Supporting Sponsors</p>\n'
            '      <ul class="logos">\n'
            f'{items}\n'
            '      </ul>\n'
            '    </div>')
    body = "\n".join(parts)
    return ('<section class="wrap supporters" id="supporters" aria-label="Supporters">\n'
            '  <div class="inner">\n'
            f'{body}\n'
            '  </div>\n'
            '</section>\n')


def render(path, text, inc, version, supporters=""):
    d = git_date(path.relative_to(ROOT))
    if d:  # same source as the sitemap's <lastmod>; the typed date is the fallback
        text = re.sub(r'"dateModified": "\d{4}-\d{2}-\d{2}"', f'"dateModified": "{d}"', text)
    text = text.replace("<!-- @header -->", inc["header"]).replace("<!-- @footer -->", inc["footer"])
    text = text.replace("<!-- @script -->", SCRIPT)
    if path.name == "index.html":
        text = text.replace("<!-- @supporters -->", supporters)
    if "<!-- @docs-start -->" in text:
        slug = path.stem
        section = next((sec for s, _, sec in FLAT if s == slug), "Guides")
        text = text.replace("<!-- @docs-start -->",
                            f'<div class="doclayout">\n{sidebar(slug)}\n<div class="measure doc-main">', 1)
        text = re.sub(r"<article>.*?</article>", lambda m: anchors(m.group(0), slug), text, count=1, flags=re.S)
        text = re.sub(r"(<article>.*?</h1>)", lambda m: m.group(1) + "\n" + MD_LINK % slug, text, count=1, flags=re.S)
        text = re.sub(r'(<link rel="canonical" href="[^"]*">)',
                      lambda m: m.group(1) + '\n<link rel="alternate" type="text/markdown" href="%s/docs/%s.md">' % (SITE, slug),
                      text, count=1)
        text = text.replace("<!-- @docs-end -->", f"{prevnext(slug)}\n</div>\n</div>", 1)
        text = text.replace("{{SECTION}}", section)
    return text.replace("{{VERSION_NUM}}", version.lstrip("v")).replace("{{VERSION}}", version)


# ---- Markdown copies of the docs pages ------------------------------------

SITE = "https://bogie-go.com"
VOID = {"br", "hr", "img", "meta", "link", "input"}
BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "pre", "table", "blockquote", "hr",
         "div", "section", "article", "header", "footer", "figure", "li", "dl", "dt", "dd"}
MD_LINK = '<p class="guide-eyebrow"><a href="/docs/%s.md">Markdown version</a></p>'


class Node:
    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.kids = tag, dict(attrs), []

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self):
        return "".join(k if isinstance(k, str) else k.text() for k in self.kids)


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root", [])
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].kids.append(n)
        if tag not in VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].kids.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def dropped(n):
    """Page chrome, not content."""
    c = n.classes
    return (n.tag in ("script", "style", "button", "aside", "input", "label", "svg", "head")
            or (n.tag == "nav" and "onpage" in c)
            or (n.tag == "a" and "anchor" in c)
            or (n.tag == "footer" and "guide-nav" in c)
            or (n.tag == "p" and "guide-eyebrow" in c))


def wrap(mark, s):
    """Put mark round s, keeping leading/trailing spaces outside it."""
    core = s.strip()
    if not core:
        return s
    return s[:len(s) - len(s.lstrip())] + mark + core + mark + s[len(s.rstrip()):]


def code_span(text):
    text = re.sub(r"\s*\n\s*", " ", text)
    run = max([len(m) for m in re.findall(r"`+", text)], default=0)
    if not run:
        return "`" + text + "`"
    return "`" * (run + 1) + " " + text + " " + "`" * (run + 1)


def escape(t):
    t = t.replace("\\", "\\\\").replace("*", "\\*").replace("<", "\\<").replace("`", "\\`")
    return re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", t)


class Markdown:
    def __init__(self, page_url):
        self.url = page_url
        self.cell = False

    def inline(self, kids):
        out = []
        for k in kids:
            if isinstance(k, str):
                out.append(escape(re.sub(r"\s+", " ", k)))
                continue
            if dropped(k):
                continue
            t = k.tag
            if t == "code":
                out.append(code_span(k.text()))
            elif t in ("strong", "b"):
                out.append(wrap("**", self.inline(k.kids)))
            elif t in ("em", "i"):
                out.append(wrap("*", self.inline(k.kids)))
            elif t == "br":
                out.append(" " if self.cell else "  \n")
            elif t == "a":
                inner = self.inline(k.kids).strip()
                href = (k.attrs.get("href") or "").strip()
                if not href or not inner:
                    out.append(inner)
                else:
                    out.append("[%s](%s)" % (inner, self.link(href)))
            elif t == "span" and "badge-breaking" in k.classes:
                out.append("**%s**" % k.text().strip())
            elif t in BLOCK:  # block inside inline context (a table cell): flatten
                out.append(" " + " ".join(self.blocks([k])).replace("\n", " ") + " ")
            else:  # unknown tags (span, ...): keep the text
                out.append(self.inline(k.kids))
        return "".join(out)

    def link(self, href):
        u = urljoin(self.url, href)
        return u.replace(" ", "%20").replace("(", "%28").replace(")", "%29")

    def para(self, buf):
        s = re.sub(r" +", " ", "".join(buf)).strip()
        return re.sub(r"^(#{1,6} |[-+] |\d+[.)] |> )", lambda m: "\\" + m.group(1), s)

    def blocks(self, kids):
        """Child nodes -> list of Markdown blocks (strings)."""
        out, buf = [], []

        def flush():
            if buf:
                p = self.para([self.inline(buf)])
                if p:
                    out.append(p)
                buf.clear()

        for k in kids:
            if not isinstance(k, str) and dropped(k):
                continue
            if isinstance(k, str) or k.tag not in BLOCK:
                buf.append(k)
                continue
            flush()
            t = k.tag
            if t in ("h2", "h3", "h4", "h5", "h6"):
                text = self.para([self.inline(k.kids)])
                if text:
                    out.append("#" * int(t[1]) + " " + text)
            elif t == "h1":
                continue
            elif t == "p":
                p = self.para([self.inline(k.kids)])
                if p:
                    out.append(p)
            elif t == "pre":
                out.append(self.fence(k.text()) if not self.cell else code_span(k.text().strip()))
            elif t in ("ul", "ol"):
                out.append(self.list(k))
            elif t == "table":
                out.append(self.table(k))
            elif t == "hr":
                out.append("---")
            elif t == "blockquote":
                inner = "\n\n".join(self.blocks(k.kids))
                out.append("\n".join(("> " + l).rstrip() for l in inner.split("\n")))
            else:  # div, section, li, dl, ...: just their content
                out.extend(self.blocks(k.kids))
        flush()
        return [b for b in out if b]

    def fence(self, text):
        text = text[1:] if text.startswith("\n") else text
        text = text[:-1] if text.endswith("\n") else text
        run = max([len(m) for m in re.findall(r"`{3,}", text)], default=0)
        f = "`" * max(3, run + 1)
        return f"{f}\n{text}\n{f}"

    def list(self, node):
        items, n = [], 0
        for li in node.kids:
            if isinstance(li, str) or li.tag != "li":
                continue
            n += 1
            marker = f"{n}. " if node.tag == "ol" else "- "
            parts = self.blocks(li.kids)
            body = ""
            for i, b in enumerate(parts):
                if i:
                    body += "\n" if re.match(r"(- |\d+\. )", b) else "\n\n"
                body += b
            pad = " " * len(marker)
            lines = body.split("\n")
            items.append(marker + "\n".join(lines[:1] + [(pad + l) if l else l for l in lines[1:]]))
        return "\n".join(items)

    def table(self, node):
        rows = []

        def walk(n):
            for k in n.kids:
                if isinstance(k, str):
                    continue
                if k.tag == "tr":
                    rows.append([c for c in k.kids if not isinstance(c, str) and c.tag in ("th", "td")])
                elif k.tag in ("thead", "tbody", "tfoot"):
                    walk(k)
        walk(node)
        if not rows:
            return ""
        self.cell = True
        try:
            grid = [[re.sub(r"\s+", " ", " ".join(self.blocks(c.kids))).strip().replace("|", "\\|") for c in r]
                    for r in rows]
        finally:
            self.cell = False
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        if not all(c.tag == "th" for c in rows[0]):
            grid.insert(0, [""] * width)
        line = lambda r: "| " + " | ".join(r) + " |"
        return "\n".join([line(grid[0]), "| " + " | ".join(["---"] * width) + " |"] + [line(r) for r in grid[1:]])


def to_markdown(article, slug):
    """The rendered <article> -> Markdown text."""
    tree = Tree()
    tree.feed(article)
    tree.close()
    h1 = []

    def find(n):
        for k in n.kids:
            if not isinstance(k, str):
                if k.tag == "h1":
                    h1.append(k)
                else:
                    find(k)
    find(tree.root)
    if not h1:
        sys.exit(f"error: no <h1> in docs/{slug}.html")
    url = f"{SITE}/docs/" if slug == "index" else f"{SITE}/docs/{slug}.html"  # the canonical URL
    md = Markdown(url)
    title = md.para([md.inline(h1[0].kids)])
    body = md.blocks(tree.root.kids)
    return "\n\n".join([f"# {title}", f"Source: {url}"] + body).rstrip() + "\n"


def check_markdown(rel, md):
    """Fail the build on stray tags or relative links in a .md file."""
    prose = re.sub(r"^(`{3,}).*?^\1`*$", "", md, flags=re.S | re.M)  # fenced blocks
    prose = re.sub(r"(`+)[^`].*?\1", "", prose, flags=re.S)           # inline code
    stray = re.findall(r"(?<!\\)<[A-Za-z/!][^\n]{0,30}", prose)
    bad = [l for l in re.findall(r"\]\(([^)]*)\)", prose) if not l.startswith(("https://", "http://", "mailto:"))]
    if stray or bad:
        sys.exit(f"error: {rel}: stray tags {stray[:3]} / relative links {bad[:3]}")


def markdown_copies_list():
    pages = [("index", "Working on a Bogie app")] + [(s, t) for s, t, _ in FLAT]
    return "\n".join(f"- [{t}]({SITE}/docs/{s}.md)" for s, t in pages)


def main():
    version = latest_version()
    if "<!-- @supporters -->" not in (ROOT / "index.html").read_text(encoding="utf-8"):
        sys.exit("error: index.html is missing <!-- @supporters -->")
    supporters = supporters_markup(load_sponsors())
    inc = {n: (ROOT / "_includes" / f"{n}.html").read_text().rstrip("\n") for n in ("header", "footer")}
    if OUT.exists():
        shutil.rmtree(OUT)
    for src in ROOT.rglob("*"):
        rel = src.relative_to(ROOT)
        if rel.parts[0] in SKIP or src.is_dir() or rel == Path("sponsors.json"):
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix == ".html" and rel.parts[0] != "design":
            out = render(src, src.read_text(), inc, version, supporters)
            left = [m for m in ("{{VERSION}}", "{{VERSION_NUM}}", "{{SECTION}}", "<!-- @") if m in out]
            if left:
                sys.exit(f"error: unfilled {left} in {rel}")
            dst.write_text(out)
            if rel.parts[0] == "docs":
                m = re.search(r"<article>.*?</article>", out, re.S)
                if not m or 'rel="alternate" type="text/markdown"' not in out:
                    sys.exit(f"error: {rel}: no <article> or no Markdown alternate link")
                md = to_markdown(m.group(0), src.stem)
                check_markdown(rel.with_suffix(".md"), md)
                dst.with_suffix(".md").write_text(md)
        elif rel == Path("sitemap.xml"):
            dst.write_text(sitemap(src.read_text()))
            if ".md" in dst.read_text():
                sys.exit("error: the .md copies must not be in sitemap.xml")
        elif rel == Path("llms.txt"):
            txt = src.read_text()
            if "<!-- @markdown-copies -->" not in txt:
                sys.exit("error: llms.txt has no <!-- @markdown-copies --> marker")
            txt = txt.replace("<!-- @markdown-copies -->", markdown_copies_list())
            if "@markdown-copies" in txt:
                sys.exit("error: llms.txt marker left unreplaced")
            dst.write_text(txt)
        else:
            shutil.copy2(src, dst)
    print(f"built {OUT.relative_to(ROOT)}/ with Bogie {version}")


if __name__ == "__main__":
    main()
