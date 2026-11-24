"""The documentation in docs/ (GitHub Pages, German): every YAML example is a
valid config.yaml, the pages link to each other, and nothing private is in it."""
import html
import pathlib
import re

import pytest

import sequence as seq

DOCS = pathlib.Path(__file__).with_name("docs")
PAGES = sorted(DOCS.glob("*.html"))
YAML = re.compile(r'<pre class="yaml">(.*?)</pre>', re.S)


def examples():
    for page in PAGES:
        for i, block in enumerate(YAML.findall(page.read_text(encoding="utf-8")), 1):
            yield pytest.param(html.unescape(block), id=f"{page.stem}-{i}")


def test_there_are_pages_and_examples():
    names = {p.name for p in PAGES}
    assert {"index.html", "konfiguration.html", "beispiele.html", "cloud.html",
            "bilder-hochladen.html"} <= names
    assert len(list(examples())) >= 15


@pytest.mark.parametrize("text", examples())
def test_every_yaml_example_is_a_valid_config(text):
    if "sequence:" not in text:  # a fragment: show_args alone
        text += "\nsequence:\n  - slideshow\n"
    seq.parse_config(text)


def test_internal_links_exist():
    for page in PAGES:
        for href in re.findall(r'href="(?:\{\{ site\.baseurl \}\}/)?([a-z-]+\.html)', page.read_text()):
            assert (DOCS / href).exists(), f"{page.name}: {href}"
    for href in re.findall(r'href="\{\{ site\.baseurl \}\}/([a-z-]+\.html)',
                           (DOCS / "_includes" / "pages.html").read_text()):
        assert (DOCS / href).exists()
    for src in re.findall(r'src="\{\{ site\.baseurl \}\}/([^"]+)"',
                          "".join(p.read_text() for p in PAGES)):
        assert (DOCS / src).exists(), src


def test_nothing_private_in_the_docs():
    text = "".join(p.read_text(encoding="utf-8") for p in DOCS.rglob("*")
                   if p.is_file() and p.suffix in (".html", ".yml", ".css"))
    assert "selfhost" not in text and "192.168." not in text
    assert not re.search(r"(\w{5}-){4}\w{5}", text.replace("xxxxx-xxxxx-xxxxx-xxxxx-xxxxx", ""))
    for addr in re.findall(r"https?://([\w.-]+)", text):
        assert addr in ("cloud.example.org", "github.com", "fritzthekid.github.io"), addr


def test_layout_has_the_favicon():
    layout = (DOCS / "_layouts" / "default.html").read_text()
    assert '<link rel="icon" href="{{ site.baseurl }}/static/favicon.ico"' in layout
    assert (DOCS / "static" / "favicon.ico").read_bytes()[:4] == b"\x00\x00\x01\x00"
