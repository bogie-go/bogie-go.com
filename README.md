# bogie-go.com

The website for [Bogie](https://github.com/bogie-go/bogie), built by a small
Python script (`build.py`, standard library only) and published to GitHub Pages
by `.github/workflows/pages.yml`.

    index.html      the front page
    docs/           the guides, one page each; docs/index.html lists them
    style.css       the one stylesheet, shared by every page
    fonts.css       the three self-hosted families under fonts/ (all OFL-licensed)
    bogie.webp      the mascot; favicon.png is the small one
    design/         the design mockup the site is ported from (open Main.dc.html
                    over a local server); the values to match live in its styles
    CNAME           bogie-go.com
    _includes/      the shared header and footer
    build.py        fills the pages in and writes _site/

## Building

Pages are still plain HTML, with a few markers that `build.py` fills in:

- `<!-- @header -->`, `<!-- @footer -->`: the files in `_includes/`
- `<!-- @docs-start -->` / `<!-- @docs-end -->` around a guide's `<article>`:
  the left-hand guide menu and the previous/next links. The menu order and
  groups live in `NAV` at the top of `build.py`; add a new guide there.
- `{{VERSION}}` (e.g. `v0.2.3`), `{{VERSION_NUM}}` (`0.2.3`), `{{SECTION}}`

The version is the latest Bogie tag from
`https://proxy.golang.org/github.com/bogie-go/bogie/@latest`. If that fetch
fails, `BOGIE_VERSION` from the environment or `FALLBACK_VERSION` in
`build.py` is used, so a build never breaks on the network.

    python3 build.py
    python3 -m http.server -d _site

The workflow builds and deploys on every push to `main`, once a day (so a new
Bogie release shows up within a day), and on demand from the Actions tab.
The build fails if any marker is left unfilled.
