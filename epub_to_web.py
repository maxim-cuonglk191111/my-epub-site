#!/usr/bin/env python3
"""
epub_to_web.py — EPUB → Static Reading Website
Ready for Cloudflare Pages (private repo) deployment.

Usage:
    python epub_to_web.py                         # Scans ./books/, outputs to ./output/
    python epub_to_web.py my-book.epub            # Single file
    python epub_to_web.py books/ output/          # Custom paths
    python epub_to_web.py --no-cache              # Force re-process all books
    python epub_to_web.py --site-name "My Library" --base-url "https://xyz.pages.dev"

Cloudflare Pages settings:
    Build command    : pip install -r requirements.txt && python epub_to_web.py
    Output directory : output
"""

import os, sys, json, re, shutil, unicodedata, argparse, warnings
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
_missing = []
try:
    import ebooklib
    from ebooklib import epub
except ImportError:
    _missing.append("ebooklib")
try:
    from bs4 import BeautifulSoup, Comment
except ImportError:
    _missing.append("beautifulsoup4")
try:
    from jinja2 import Environment, DictLoader
except ImportError:
    _missing.append("jinja2")

if _missing:
    print(f"❌ Missing: {', '.join(_missing)}")
    print(f"   Run: pip install {' '.join(_missing)} lxml")
    sys.exit(1)

# Suppress noisy BeautifulSoup warning when parsing EPUB XML as HTML
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass
warnings.filterwarnings("ignore", message="It looks like you're parsing an XML document")


# ══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def slugify(text: str, max_len: int = 60) -> str:
    """Vietnamese-aware slug: strips diacritics → lowercase → hyphens → max 60 chars."""
    text = unicodedata.normalize("NFD", str(text))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-_")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "untitled"


_KEEP_TAGS = {
    "p", "br", "b", "i", "em", "strong", "u", "s",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "blockquote",
    "img", "figure", "figcaption",
    "div", "span", "hr", "sup", "sub",
}

def clean_html(raw: str, img_map: dict | None = None) -> str:
    """Strip EPUB CSS junk; keep only semantic tags; remap image paths."""
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.find_all(["script", "style", "link", "meta", "head"]):
        tag.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    body = soup.find("body") or soup

    for tag in body.find_all(True):
        if tag.name not in _KEEP_TAGS:
            tag.unwrap()
        else:
            safe_attrs = {"src", "alt", "href", "title"}
            for attr in list(tag.attrs):
                if attr not in safe_attrs:
                    del tag.attrs[attr]

    if img_map:
        for img in body.find_all("img"):
            src = img.get("src", "")
            basename = Path(src).name
            for old, new in img_map.items():
                if old in src or Path(old).name == basename:
                    img["src"] = new
                    break

    result = str(body)
    result = re.sub(r"^<body[^>]*>|</body>$", "", result, flags=re.IGNORECASE)
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r"(<br\s*/?>){3,}", "<br><br>", result)
    return result.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  EPUB PARSER
# ══════════════════════════════════════════════════════════════════════════════

def find_cover_item(book):
    """
    Locate the cover image item in an EPUB using multiple fallback strategies:
    1. EPUB3 item with properties="cover-image"
    2. EPUB2 <meta name="cover" content="item-id"> OPF metadata
    3. Any image whose filename contains "cover"
    4. First image in the book (most EPUBs put cover first)
    """
    items = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
    if not items:
        return None

    # Strategy 1 — EPUB3 cover-image property
    for item in items:
        props = getattr(item, "properties", None) or []
        if isinstance(props, str):
            props = props.split()
        if "cover-image" in props:
            return item

    # Strategy 2 — EPUB2 OPF <meta name="cover">
    try:
        cover_meta = book.get_metadata("OPF", "cover")
        if cover_meta:
            cover_id = cover_meta[0][0]
            item = book.get_item_with_id(cover_id)
            if item and item.get_type() == ebooklib.ITEM_IMAGE:
                return item
    except Exception:
        pass

    # Strategy 3 — filename contains "cover"
    for item in items:
        if "cover" in Path(item.file_name).stem.lower():
            return item

    # Strategy 4 — first image (heuristic)
    return items[0]


def parse_epub(epub_path: Path, book_out_dir: Path) -> dict:
    """
    Parse an EPUB file.
    Returns full book dict (includes chapter content for HTML generation).
    Also writes .meta.json (lightweight cache without content).
    """
    print(f"\n📖  Parsing: {epub_path.name}")
    book = epub.read_epub(str(epub_path))

    # ── Metadata ──────────────────────────────────────────────────────────────
    def _meta(ns, key):
        v = book.get_metadata(ns, key)
        return v[0][0] if v else None

    title       = _meta("DC", "title")       or epub_path.stem
    author      = _meta("DC", "creator")     or "Không rõ"
    description = _meta("DC", "description") or ""
    language    = _meta("DC", "language")    or "vi"

    if description:
        description = BeautifulSoup(description, "lxml").get_text()

    # Use the directory name as slug so it's always consistent with the
    # book_out_dir that was already created by main() from the filename slug.
    slug = book_out_dir.name
    print(f"    Title  : {title}")
    print(f"    Author : {author}")
    print(f"    Slug   : {slug}")

    # ── Images ────────────────────────────────────────────────────────────────
    img_dir = book_out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    img_map: dict[str, str] = {}
    has_cover   = False
    cover_ext   = ".jpg"

    # Identify cover item upfront using robust multi-strategy detection
    cover_item    = find_cover_item(book)
    cover_item_id = cover_item.file_name if cover_item else None

    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        name = Path(item.file_name).name
        dest = img_dir / name
        dest.write_bytes(item.get_content())
        img_map[item.file_name] = f"images/{name}"

        if item.file_name == cover_item_id:
            has_cover = True
            ext       = Path(name).suffix or ".jpg"
            cover_ext = ext
            cover_dest = img_dir / f"cover{ext}"
            # Guard against SameFileError when image is already named cover.*
            if dest.resolve() != cover_dest.resolve():
                shutil.copy(dest, cover_dest)
            img_map[item.file_name] = f"images/cover{ext}"  # remap to canonical name

    # ── Language-aware fallback chapter word ─────────────────────────────────
    ch_word = "Chapter" if language.lower().startswith("en") else "Chương"

    # ── File-name → document item lookup (multiple path formats) ─────────────
    fname_to_item: dict[str, object] = {}
    for it in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        fname_to_item[it.file_name]              = it  # full path: OEBPS/ch1.xhtml
        fname_to_item[Path(it.file_name).name]   = it  # basename: ch1.xhtml
        # also without leading slash / dot-dot
        norm = it.file_name.lstrip("/").lstrip("./")
        fname_to_item.setdefault(norm, it)

    # ── Collect TOC entries in order ──────────────────────────────────────────
    def _collect_toc(items: list, result: list) -> None:
        for item in items:
            if isinstance(item, epub.Link):
                result.append((item.href.split("#")[0], item.title or ""))
            elif isinstance(item, tuple) and len(item) == 2:
                sec, children = item
                if hasattr(sec, "href"):
                    result.append((sec.href.split("#")[0], sec.title or ""))
                _collect_toc(children, result)

    toc_raw: list[tuple[str, str]] = []
    _collect_toc(book.toc, toc_raw)

    # Deduplicate by file path (same file may appear at multiple TOC levels)
    seen_files: set[str] = set()
    toc_entries: list[tuple[str, str]] = []
    for href, title in toc_raw:
        key = Path(href).name  # normalise to basename for dedup
        if key not in seen_files:
            seen_files.add(key)
            toc_entries.append((href, title))

    # ── Chapters ──────────────────────────────────────────────────────────────
    chapters: list[dict] = []
    ch_num   = 0

    # Pages whose heading marks them as non-content structural matter
    _STRUCTURAL = {
        "contents", "table of contents", "toc", "cover", "title page",
        "copyright", "colophon", "index", "blank page", "half title",
        "series page", "also by",
    }

    if toc_entries:
        # ── Strategy A: TOC-first (preferred) ─────────────────────────────────
        # Follow the TOC exactly — same order and titles as the author intended.
        for href, toc_title in toc_entries:
            item = (
                fname_to_item.get(href)
                or fname_to_item.get(Path(href).name)
                or fname_to_item.get(href.lstrip("/"))
            )
            if not item:
                continue

            raw  = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(raw, "lxml")
            text = soup.get_text(strip=True)

            if len(text) < 80:          # Skip near-empty pages (title, copyright…)
                continue

            ch_num += 1
            content  = clean_html(raw, img_map)
            ch_slug  = f"chuong-{ch_num}"
            ch_title = toc_title.strip() or f"{ch_word} {ch_num}"

            chapters.append({
                "number":  ch_num,
                "title":   ch_title,
                "slug":    ch_slug,
                "content": content,
            })
            label = ch_title[:68] + ("…" if len(ch_title) > 68 else "")
            print(f"    [{ch_num:4d}] {label}")

    else:
        # ── Strategy B: Spine scan fallback (EPUBs without a usable TOC) ──────
        id_to_item = {it.id: it for it in book.get_items()}
        doc_items = [
            id_to_item[iid]
            for iid, _ in book.spine
            if iid in id_to_item
            and id_to_item[iid].get_type() == ebooklib.ITEM_DOCUMENT
        ] or list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

        for item in doc_items:
            raw  = item.get_content().decode("utf-8", errors="replace")
            soup = BeautifulSoup(raw, "lxml")
            text = soup.get_text(strip=True)

            if soup.find("nav") or len(text) < 200:
                continue

            h       = soup.find(["h1", "h2", "h3"])
            heading = h.get_text(strip=True) if h else ""
            if heading.lower().strip() in _STRUCTURAL:
                continue

            ch_num += 1
            fname    = Path(item.file_name).name
            ch_title = (
                heading
                or f"{ch_word} {ch_num}"
            )

            content  = clean_html(raw, img_map)
            ch_slug  = f"chuong-{ch_num}"

            chapters.append({
                "number":  ch_num,
                "title":   ch_title,
                "slug":    ch_slug,
                "content": content,
            })
            label = ch_title[:68] + ("…" if len(ch_title) > 68 else "")
            print(f"    [{ch_num:4d}] {label}")

    print(f"    ✅  {len(chapters)} chapters")

    full_data = {
        "title":         title,
        "author":        author,
        "description":   description,
        "language":      language,
        "slug":          slug,
        "has_cover":     has_cover,
        "cover_ext":     cover_ext,
        "chapter_count": len(chapters),
        "chapters":      chapters,
    }

    # Write lightweight cache (no content, just metadata + chapter list)
    meta = {k: v for k, v in full_data.items() if k != "chapters"}
    meta["chapters"] = [
        {"number": c["number"], "title": c["title"], "slug": c["slug"]}
        for c in chapters
    ]
    (book_out_dir / ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return full_data


def load_meta(book_out_dir: Path) -> dict | None:
    """Load cached book metadata (no content). Returns None if not found."""
    meta_path = book_out_dir / ".meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  TEMPLATES  (Jinja2)
# ══════════════════════════════════════════════════════════════════════════════

_T: dict[str, str] = {}

_T["base.html"] = r"""<!DOCTYPE html>
<html lang="{{ book.language if book is defined else 'vi' }}" data-theme="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}{% endblock %}</title>
  <meta name="description" content="{% block description %}{% endblock %}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Crimson+Pro:ital,wght@0,400;0,600;1,400&family=Inter:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{{ base_url }}/assets/style.css">
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
</head>
<body>
  <header class="site-header" id="site-header">
    <nav class="nav-wrap">
      <a href="{{ base_url }}/" class="site-logo">
        <span class="logo-glyph">❧</span>
        <span class="logo-name">{{ site_name }}</span>
      </a>
      <a href="/admin/" class="icon-btn admin-link" title="Trang quản trị" aria-label="Admin">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </a>
      <button id="theme-btn" class="icon-btn" title="Đổi giao diện" aria-label="Toggle theme">
        <svg class="icon-moon" xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>
        <svg class="icon-sun" xmlns="http://www.w3.org/2000/svg" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="display:none"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>
      </button>
    </nav>
  </header>

  <main>{% block content %}{% endblock %}</main>

  <footer class="site-footer">
    <p>Tạo bởi <em>epub-to-web</em></p>
  </footer>

  <script src="{{ base_url }}/assets/app.js"></script>
  {% block extra_scripts %}{% endblock %}
</body>
</html>
"""

_T["index.html"] = r"""{% extends "base.html" %}
{% block title %}{{ site_name }}{% endblock %}
{% block description %}Thư viện — {{ books|length }} cuốn truyện{% endblock %}
{% block content %}
<div class="page-home">
  <div class="hero">
    <h1 class="hero-title">Thư viện</h1>
    <p class="hero-sub">{{ books|length }} cuốn truyện</p>
  </div>
  <div class="book-grid wrap">
    {% for b in books %}
    <a href="{{ base_url }}/{{ b.slug }}/" class="book-card">
      <div class="cover-wrap">
        {% if b.has_cover %}
        <img src="{{ base_url }}/{{ b.slug }}/images/cover{{ b.cover_ext }}"
             alt="{{ b.title }}" class="cover-img" loading="lazy">
        {% else %}
        <div class="cover-stub"><span>{{ b.title[0] }}</span></div>
        {% endif %}
      </div>
      <div class="card-body">
        <h2 class="card-title">{{ b.title }}</h2>
        <p class="card-author">{{ b.author }}</p>
        <span class="card-badge">{{ b.chapter_count }} chương</span>
      </div>
    </a>
    {% endfor %}
  </div>
</div>
{% endblock %}
"""

_T["book.html"] = r"""{% extends "base.html" %}
{% block title %}{{ book.title }} — {{ site_name }}{% endblock %}
{% block description %}{{ (book.description or book.title)[:155] }}{% endblock %}
{% block content %}
<div class="page-book">
  <div class="book-header wrap">
    <div class="cover-col">
      {% if book.has_cover %}
      <img src="images/cover{{ book.cover_ext }}" alt="{{ book.title }}" class="cover-lg">
      {% else %}
      <div class="cover-lg-stub"><span>{{ book.title[0] }}</span></div>
      {% endif %}
    </div>
    <div class="info-col">
      <p class="eyebrow">Tác phẩm</p>
      <h1 class="book-title">{{ book.title }}</h1>
      <p class="book-author">{{ book.author }}</p>
      {% if book.description %}
      <p class="book-desc">{{ book.description[:320] }}{% if book.description|length > 320 %}…{% endif %}</p>
      {% endif %}
      <div class="book-cta">
        <a href="{{ chapters[0].slug }}.html" class="btn-read">Đọc ngay →</a>
        <span class="ch-count">{{ book.chapter_count }} chương</span>
      </div>
    </div>
  </div>

  <section class="toc-section wrap">
    <h2 class="toc-head">Mục lục</h2>
    <ol class="toc-list">
      {% for ch in chapters %}
      <li>
        <a href="{{ ch.slug }}.html" class="toc-row">
          <span class="toc-num">{{ ch.number }}</span>
          <span class="toc-title">{{ ch.title }}</span>
        </a>
      </li>
      {% endfor %}
    </ol>
  </section>
</div>
{% endblock %}
"""

_T["chapter.html"] = r"""{% extends "base.html" %}
{% block title %}{{ chapter.title }} — {{ book.title }}{% endblock %}
{% block description %}{{ chapter.title }}, {{ book.title }} — {{ book.author }}{% endblock %}
{% block content %}
<div class="page-chapter">
  <div class="reading-bar" id="reading-bar"></div>

  <!-- Sub-nav: auto-hides on scroll down -->
  <div class="reader-subnav" id="reader-subnav">
    <div class="reader-subnav-in wrap">
      <a href="index.html" class="back-link">
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
        Mục lục
      </a>
      <span class="rdr-book-name">{{ book.title }}</span>
      <div style="display:flex;gap:.2rem;flex-shrink:0">
        <button class="icon-btn" id="btn-toc-top" title="Danh sách chương">
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
        </button>
        <button class="icon-btn" id="btn-sp-top" title="Cài đặt đọc" style="font-size:.8rem;font-weight:600">Aa</button>
      </div>
    </div>
  </div>

  <!-- Chapter body -->
  <article class="reader" id="reader">
    <header class="ch-header wrap">
      <p class="ch-eyebrow">{{ book.title }}</p>
      <h1 class="ch-title">{{ chapter.title }}</h1>
    </header>
    <div class="ch-body wrap" id="ch-body">{{ chapter.content | safe }}</div>
  </article>

  <!-- Inline bottom nav -->
  <nav class="ch-nav wrap" aria-label="Điều hướng chương">
    <div class="ch-nav-grid">
      {% if prev_chapter %}
      <a href="{{ prev_chapter.slug }}.html" class="nav-btn nav-prev">
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
        <span><small>Chương trước</small><strong>{{ prev_chapter.title }}</strong></span>
      </a>
      {% else %}<div></div>{% endif %}
      <a href="index.html" class="nav-toc" title="Mục lục">☰</a>
      {% if next_chapter %}
      <a href="{{ next_chapter.slug }}.html" class="nav-btn nav-next">
        <span><small>Chương tiếp</small><strong>{{ next_chapter.title }}</strong></span>
        <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
      </a>
      {% else %}<div></div>{% endif %}
    </div>
  </nav>
</div>

<!-- ── Float bar ── -->
<div class="float-bar" id="float-bar">
  {% if prev_chapter %}
  <a href="{{ prev_chapter.slug }}.html" class="fbar-btn" id="float-prev" title="{{ prev_chapter.title }}">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
    <span>Trước</span>
  </a>
  {% else %}
  <span class="fbar-btn fbar-disabled">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="15 18 9 12 15 6"/></svg>
    <span>Trước</span>
  </span>
  {% endif %}
  <div class="fbar-sep"></div>
  <button class="fbar-btn" id="btn-toc-bar" title="Danh sách chương">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
    <span>Mục lục</span>
  </button>
  <button class="fbar-btn" id="btn-sp-bar" style="font-size:.85rem;font-weight:700;letter-spacing:-.02em" title="Cài đặt đọc">
    Aa
    <span>Cài đặt</span>
  </button>
  <div class="fbar-sep"></div>
  {% if next_chapter %}
  <a href="{{ next_chapter.slug }}.html" class="fbar-btn" id="float-next" title="{{ next_chapter.title }}">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
    <span>Tiếp</span>
  </a>
  {% else %}
  <span class="fbar-btn fbar-disabled">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>
    <span>Tiếp</span>
  </span>
  {% endif %}
</div>

<!-- ── Settings panel ── -->
<div class="sp-panel" id="sp-panel">
  <div class="sp-handle"></div>
  <div class="sp-head">
    <span class="sp-head-title">Cài đặt đọc</span>
    <button class="sp-close" id="btn-sp-close">
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <div class="sp-body">
    <div class="sp-group">
      <div class="sp-label">Cỡ chữ</div>
      <div class="sp-row sp-fs-row">
        <button class="sp-ctrl" id="sp-fs-dec">A−</button>
        <span class="sp-fs-disp"><span id="sp-fs-val">18</span>px</span>
        <button class="sp-ctrl" id="sp-fs-inc">A+</button>
      </div>
    </div>
    <div class="sp-group">
      <div class="sp-label">Font chữ</div>
      <div class="sp-row">
        <button class="sp-opt" data-pref="font" data-val="serif" style="font-family:'Crimson Pro',Georgia,serif;font-size:1rem">Serif</button>
        <button class="sp-opt" data-pref="font" data-val="sans"  style="font-family:system-ui,sans-serif">Sans-serif</button>
      </div>
    </div>
    <div class="sp-group">
      <div class="sp-label">Giãn dòng</div>
      <div class="sp-row">
        <button class="sp-opt" data-pref="lh" data-val="1.6">Chật</button>
        <button class="sp-opt" data-pref="lh" data-val="1.9">Vừa</button>
        <button class="sp-opt" data-pref="lh" data-val="2.3">Rộng</button>
      </div>
    </div>
    <div class="sp-group">
      <div class="sp-label">Độ rộng trang</div>
      <div class="sp-row">
        <button class="sp-opt" data-pref="width" data-val="52ch">Hẹp</button>
        <button class="sp-opt" data-pref="width" data-val="68ch">Vừa</button>
        <button class="sp-opt" data-pref="width" data-val="90ch">Rộng</button>
      </div>
    </div>
    <div class="sp-group">
      <div class="sp-label">Màu nền</div>
      <div class="sp-row">
        <button class="sp-bg" data-bg="light">☀ Trắng</button>
        <button class="sp-bg" data-bg="sepia">📜 Vàng</button>
        <button class="sp-bg" data-bg="dark" >🌙 Tối</button>
      </div>
    </div>
  </div>
</div>

<!-- ── TOC drawer ── -->
<div class="toc-drawer" id="toc-drawer">
  <div class="td-head">
    <div>
      <div class="td-book-title">{{ book.title }}</div>
      <div class="td-ch-count">{{ book.chapter_count }} chương · đang đọc #{{ chapter.number }}</div>
    </div>
    <button class="td-close" id="btn-td-close">
      <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  </div>
  <ol class="td-list" id="td-list">
    {% for ch in chapters %}
    <li{% if ch.number == chapter.number %} class="td-cur"{% endif %}>
      <a href="{{ ch.slug }}.html">
        <span class="td-num">{{ ch.number }}</span>
        <span class="td-title">{{ ch.title }}</span>
      </a>
    </li>
    {% endfor %}
  </ol>
</div>

<!-- ── Backdrop ── -->
<div class="backdrop" id="backdrop"></div>
{% endblock %}

{% block extra_scripts %}
<script>
(function(){
'use strict';
const LS_P = 'rdr-prefs';
const html  = document.documentElement;
const DEFS  = { fs: 1.15, font: 'serif', lh: 1.9, width: '68ch', bg: 'light' };
let P = Object.assign({}, DEFS, JSON.parse(localStorage.getItem(LS_P)||'{}'));

function saveP(){ localStorage.setItem(LS_P, JSON.stringify(P)); }

function applyP(){
  html.style.setProperty('--fs',   P.fs + 'rem');
  html.style.setProperty('--lh',   P.lh);
  html.style.setProperty('--rmax', P.width);
  html.style.setProperty('--rw',
    P.font === 'sans' ? "'Inter',system-ui,sans-serif"
                      : "'Crimson Pro',Georgia,serif");
  // Reading background (separate from site theme)
  html.setAttribute('data-reading-bg', P.bg);
  // Active states
  document.querySelectorAll('.sp-opt').forEach(b =>
    b.classList.toggle('sp-active', String(P[b.dataset.pref]) === b.dataset.val));
  document.querySelectorAll('.sp-bg').forEach(b =>
    b.classList.toggle('sp-active', b.dataset.bg === P.bg));
  const v = document.getElementById('sp-fs-val');
  if(v) v.textContent = Math.round(P.fs * 16);
}
applyP();

// Font size controls
document.getElementById('sp-fs-dec')?.addEventListener('click', ()=>{
  P.fs = +(Math.max(0.875, P.fs - 0.0625)).toFixed(4); saveP(); applyP();
});
document.getElementById('sp-fs-inc')?.addEventListener('click', ()=>{
  P.fs = +(Math.min(2.0,   P.fs + 0.0625)).toFixed(4); saveP(); applyP();
});
document.querySelectorAll('.sp-opt').forEach(b=>b.addEventListener('click',()=>{
  P[b.dataset.pref] = b.dataset.val; saveP(); applyP();
}));
document.querySelectorAll('.sp-bg').forEach(b=>b.addEventListener('click',()=>{
  P.bg = b.dataset.bg; saveP(); applyP();
}));

// ── Auto-hide on scroll ─────────────────────────────────────────────────────
let lastY = window.scrollY, tick = false;
const hdr    = document.getElementById('site-header');
const subnav = document.getElementById('reader-subnav');
const fbar   = document.getElementById('float-bar');
function onScroll(){
  const y = window.scrollY, d = y - lastY;
  if(Math.abs(d) > 4){
    const hide = d > 0 && y > 120;
    hdr?.classList.toggle('hdr-hidden', hide);
    subnav?.classList.toggle('hdr-hidden', hide);
    fbar?.classList.toggle('fbar-hidden', hide);
    lastY = y;
  }
  tick = false;
}
window.addEventListener('scroll',()=>{ if(!tick){requestAnimationFrame(onScroll);tick=true;} },{passive:true});

// ── Progress bar ────────────────────────────────────────────────────────────
const bar    = document.getElementById('reading-bar');
const reader = document.getElementById('reader');
window.addEventListener('scroll',()=>{
  if(!bar||!reader) return;
  const tot = reader.offsetHeight - window.innerHeight;
  bar.style.width = (tot<=0?100:Math.max(0,Math.min(100,-reader.getBoundingClientRect().top/tot*100)))+'%';
},{passive:true});

// ── Panels ──────────────────────────────────────────────────────────────────
const spPanel  = document.getElementById('sp-panel');
const tocPanel = document.getElementById('toc-drawer');
const backdrop = document.getElementById('backdrop');
function closeAll(){
  spPanel.classList.remove('panel-open');
  tocPanel.classList.remove('panel-open');
  backdrop.classList.remove('panel-open');
  document.body.classList.remove('no-scroll');
}
function openPanel(el){
  closeAll();
  el.classList.add('panel-open');
  backdrop.classList.add('panel-open');
  document.body.classList.add('no-scroll');
}
function togPanel(el){ el.classList.contains('panel-open')?closeAll():openPanel(el); }

['btn-sp-bar','btn-sp-top'].forEach(id=>
  document.getElementById(id)?.addEventListener('click',()=>togPanel(spPanel)));
['btn-toc-bar','btn-toc-top'].forEach(id=>
  document.getElementById(id)?.addEventListener('click',()=>{
    togPanel(tocPanel);
    if(tocPanel.classList.contains('panel-open'))
      setTimeout(()=>document.querySelector('.td-cur')?.scrollIntoView({block:'center',behavior:'smooth'}),150);
  }));
document.getElementById('btn-sp-close')?.addEventListener('click', closeAll);
document.getElementById('btn-td-close')?.addEventListener('click', closeAll);
backdrop.addEventListener('click', closeAll);

// ── Session scroll ──────────────────────────────────────────────────────────
const PK = 'pos:'+location.pathname;
const sp = sessionStorage.getItem(PK);
if(sp) requestAnimationFrame(()=>window.scrollTo(0,parseInt(sp)));
window.addEventListener('beforeunload',()=>sessionStorage.setItem(PK,window.scrollY));

// ── Keyboard ────────────────────────────────────────────────────────────────
document.addEventListener('keydown',e=>{
  if(['INPUT','TEXTAREA'].includes(e.target.tagName)) return;
  if(e.key==='ArrowLeft'||e.key===',')  document.getElementById('float-prev')?.click();
  if(e.key==='ArrowRight'||e.key==='.') document.getElementById('float-next')?.click();
  if(e.key==='Escape') closeAll();
});
})();
</script>
{% endblock %}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  CSS
# ══════════════════════════════════════════════════════════════════════════════

CSS = r"""
/* ── Tokens ── */
:root {
  --ink:     #1c1917;
  --ink-2:   #44403c;
  --ink-3:   #a8a29e;
  --bg:      #faf9f7;
  --bg-2:    #f0ede7;
  --bg-3:    #e7e0d8;
  --gold:    #a87630;
  --accent:  #7c3022;
  --link:    #5b4034;
  --r:       6px;
  --rw:      'Crimson Pro', Georgia, serif;
  --uf:      'Inter', system-ui, sans-serif;
  --rmax:    68ch;
  --fs:      1.2rem;
  --lh:      1.9;
}
[data-theme="dark"] {
  --ink:   #e8e0d5;
  --ink-2: #a8a29e;
  --ink-3: #78716c;
  --bg:    #171412;
  --bg-2:  #201d1a;
  --bg-3:  #2a2623;
  --gold:  #c9a55a;
  --accent:#c97a55;
  --link:  #c9a55a;
}

/* ── Reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:16px;scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{
  font-family:var(--uf);background:var(--bg);color:var(--ink);
  min-height:100vh;display:flex;flex-direction:column;
  transition:background .25s,color .25s;
}
main{flex:1}
a{color:var(--link);text-decoration:none}
a:hover{text-decoration:underline}
img{max-width:100%;height:auto;display:block}

/* ── Layout ── */
.wrap{max-width:900px;margin:0 auto;padding:0 1.5rem}

/* ── Nav ── */
.site-header{
  position:sticky;top:0;z-index:60;
  border-bottom:1px solid var(--bg-3);
  background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(10px);
}
.nav-wrap{
  max-width:900px;margin:0 auto;padding:.7rem 1.5rem;
  display:flex;align-items:center;justify-content:space-between;
}
.site-logo{display:flex;align-items:center;gap:.45rem;color:var(--ink);text-decoration:none}
.logo-glyph{font-size:1.25rem;color:var(--gold)}
.logo-name{font-family:var(--rw);font-size:1.05rem;font-weight:600}
.icon-btn{
  background:none;border:none;cursor:pointer;color:var(--ink-2);
  padding:.35rem .4rem;border-radius:var(--r);
  display:flex;align-items:center;gap:.2rem;
  font-family:var(--uf);font-size:.78rem;font-weight:500;
  transition:color .15s,background .15s;
}
.icon-btn:hover{color:var(--ink);background:var(--bg-2)}

/* ── Home ── */
.hero{
  text-align:center;padding:4rem 1.5rem 2rem;
  border-bottom:1px solid var(--bg-3);
}
.hero-title{
  font-family:var(--rw);
  font-size:clamp(2.2rem,6vw,3.6rem);
  font-weight:600;letter-spacing:-.02em;line-height:1.1;
}
.hero-title::after{
  content:'';display:block;width:3rem;height:2px;
  background:var(--gold);margin:.75rem auto 0;
}
.hero-sub{margin-top:.6rem;color:var(--ink-3);font-size:.9rem}

.book-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(185px,1fr));
  gap:2rem;padding-top:2.5rem;padding-bottom:3.5rem;
}
.book-card{
  display:flex;flex-direction:column;color:inherit;text-decoration:none;
  border-radius:var(--r);transition:transform .2s;
}
.book-card:hover{transform:translateY(-4px);text-decoration:none}
.cover-wrap{
  aspect-ratio:2/3;border-radius:var(--r);overflow:hidden;
  background:var(--bg-3);
  box-shadow:0 4px 18px rgba(0,0,0,.14);
}
.cover-img{width:100%;height:100%;object-fit:cover}
.cover-stub{
  width:100%;height:100%;
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(145deg,var(--bg-3),var(--bg-2));
  font-family:var(--rw);font-size:4.5rem;font-weight:700;color:var(--gold);
}
.card-body{padding:.7rem .15rem 0}
.card-title{
  font-family:var(--rw);font-size:.98rem;font-weight:600;line-height:1.3;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}
.card-author{font-size:.78rem;color:var(--ink-3);margin-top:.2rem}
.card-badge{
  display:inline-block;margin-top:.4rem;
  font-size:.7rem;background:var(--bg-2);color:var(--ink-3);
  padding:.12rem .45rem;border-radius:999px;
}

/* ── Book Detail ── */
.page-book{padding-bottom:4rem}
.book-header{
  display:grid;grid-template-columns:190px 1fr;
  gap:2.5rem;align-items:start;padding-top:3rem;
}
.cover-lg{
  width:100%;border-radius:var(--r);
  box-shadow:0 8px 32px rgba(0,0,0,.2);
}
.cover-lg-stub{
  width:100%;aspect-ratio:2/3;border-radius:var(--r);
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(145deg,var(--bg-3),var(--bg-2));
  font-family:var(--rw);font-size:5rem;font-weight:700;color:var(--gold);
  box-shadow:0 8px 32px rgba(0,0,0,.14);
}
.eyebrow{
  font-size:.72rem;font-weight:500;text-transform:uppercase;
  letter-spacing:.12em;color:var(--gold);
}
.book-title{
  font-family:var(--rw);
  font-size:clamp(1.5rem,3.5vw,2.3rem);font-weight:600;
  line-height:1.2;margin-top:.35rem;
}
.book-author{font-size:.95rem;color:var(--ink-2);margin-top:.4rem}
.book-desc{
  font-size:.92rem;line-height:1.7;color:var(--ink-2);
  margin-top:.9rem;
  padding-left:1rem;border-left:3px solid var(--bg-3);
}
.book-cta{display:flex;align-items:center;gap:1rem;margin-top:1.4rem}
.btn-read{
  display:inline-block;
  background:var(--accent);color:#fff;
  padding:.6rem 1.4rem;border-radius:var(--r);
  font-size:.88rem;font-weight:500;
  transition:opacity .2s;
}
.btn-read:hover{opacity:.82;text-decoration:none}
.ch-count{font-size:.82rem;color:var(--ink-3)}

.toc-section{margin-top:3rem;padding-bottom:3rem}
.toc-head{
  font-family:var(--rw);font-size:1.3rem;font-weight:600;
  padding-bottom:.7rem;border-bottom:1px solid var(--bg-3);
  margin-bottom:.5rem;
}
.toc-list{list-style:none}
.toc-row{
  display:flex;align-items:baseline;gap:.9rem;
  padding:.55rem .4rem;border-radius:var(--r);
  color:var(--ink-2);font-size:.93rem;
  transition:background .15s,color .15s;
}
.toc-row:hover{background:var(--bg-2);color:var(--ink);text-decoration:none}
.toc-num{
  min-width:2.5rem;text-align:right;
  font-size:.75rem;color:var(--ink-3);font-variant-numeric:tabular-nums;
  flex-shrink:0;
}
.toc-title{flex:1}
.toc-list li+li{border-top:1px solid var(--bg-2)}

/* ── Reader ── */
.reading-bar{
  position:fixed;top:0;left:0;height:2px;
  background:var(--gold);width:0%;z-index:200;
  transition:width .12s linear;
}
.reader-bar{
  position:sticky;top:var(--nav-h,49px);z-index:50;
  background:color-mix(in srgb,var(--bg) 90%,transparent);
  backdrop-filter:blur(8px);
  border-bottom:1px solid var(--bg-3);
}
.reader-bar-inner{
  display:flex;align-items:center;justify-content:space-between;
  padding-top:.45rem;padding-bottom:.45rem;gap:.75rem;
}
.back-link{
  display:flex;align-items:center;gap:.25rem;
  font-size:.8rem;color:var(--ink-3);
  transition:color .15s;white-space:nowrap;
}
.back-link:hover{color:var(--ink);text-decoration:none}
.reader-book-name{
  font-size:.78rem;color:var(--ink-3);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  flex:1;text-align:center;
}
.fs-controls{display:flex;gap:.2rem;flex-shrink:0}

.reader{padding:2.5rem 0 3rem}
.ch-header{text-align:center;padding-bottom:2rem;border-bottom:1px solid var(--bg-3);margin-bottom:2.5rem}
.ch-eyebrow{font-size:.72rem;font-weight:500;text-transform:uppercase;letter-spacing:.1em;color:var(--gold);margin-bottom:.5rem}
.ch-title{font-family:var(--rw);font-size:clamp(1.4rem,3vw,2rem);font-weight:600;line-height:1.25}

/* ── Chapter Body ── */
.ch-body{
  font-family:var(--rw);
  font-size:var(--fs);line-height:var(--lh);
  color:var(--ink);max-width:var(--rmax);
}
.ch-body p{margin-bottom:1.1em;text-align:justify}
.ch-body p:first-child::first-letter{
  font-size:3.6em;font-weight:600;float:left;
  line-height:.75;padding-right:.1em;
  color:var(--gold);font-family:var(--rw);
}
.ch-body h1,.ch-body h2,.ch-body h3{
  font-family:var(--rw);font-weight:600;line-height:1.25;
  margin:2em 0 .75em;
}
.ch-body h1{font-size:1.55em}
.ch-body h2{font-size:1.3em}
.ch-body h3{font-size:1.1em;color:var(--ink-2)}
.ch-body blockquote{
  border-left:3px solid var(--gold);padding-left:1.2em;
  color:var(--ink-2);margin:1.5em 0;font-style:italic;
}
.ch-body img{
  max-width:100%;border-radius:var(--r);
  margin:1.75em auto;
  box-shadow:0 4px 18px rgba(0,0,0,.12);
}
.ch-body hr{
  border:none;border-top:1px solid var(--bg-3);
  margin:2.5em auto;width:35%;
}
.ch-body ul,.ch-body ol{padding-left:1.75em;margin-bottom:1em}
.ch-body li{margin-bottom:.4em}

/* ── Chapter Nav ── */
.ch-nav{padding:1.5rem 1.5rem 3.5rem}
.ch-nav-grid{
  display:grid;grid-template-columns:1fr auto 1fr;
  gap:1rem;align-items:center;
  border-top:1px solid var(--bg-3);padding-top:1.5rem;
}
.nav-btn{
  display:flex;align-items:center;gap:.65rem;
  padding:.7rem .9rem;border-radius:var(--r);
  background:var(--bg-2);color:var(--ink-2);
  transition:background .2s,color .2s;
  min-width:0;
}
.nav-btn:hover{background:var(--bg-3);color:var(--ink);text-decoration:none}
.nav-btn span{display:flex;flex-direction:column;min-width:0}
.nav-btn small{font-size:.68rem;color:var(--ink-3)}
.nav-btn strong{font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nav-next{justify-content:flex-end;text-align:right}
.nav-toc{
  font-size:1.2rem;text-align:center;
  padding:.45rem .55rem;border-radius:var(--r);
  transition:background .15s;
}
.nav-toc:hover{background:var(--bg-2);text-decoration:none}

/* ── Footer ── */
.site-footer{
  text-align:center;padding:1.25rem;
  font-size:.78rem;color:var(--ink-3);
  border-top:1px solid var(--bg-3);margin-top:auto;
}
.site-footer em{color:var(--gold);font-style:normal}

/* ── Responsive ── */
@media(max-width:640px){
  .book-header{grid-template-columns:1fr}
  .cover-col{max-width:160px}
  .nav-btn strong{display:none}
  .reader-book-name{display:none}
  .ch-body p:first-child::first-letter{float:none;font-size:1em;padding:0;color:inherit}
}

/* ── Reading background overrides (set via data-reading-bg on html) ── */
[data-reading-bg="sepia"]{--bg:#faf3e5;--bg-2:#f0e8d0;--bg-3:#e5d8b8;--ink:#3d2f18;--ink-2:#6b5539;--ink-3:#9b8060;--gold:#9b6c1a}
[data-reading-bg="dark"] {--bg:#171412;--bg-2:#201d1a;--bg-3:#2a2623;--ink:#e8e0d5;--ink-2:#a8a29e;--ink-3:#78716c;--gold:#c9a55a}
[data-reading-bg="light"]{/* uses :root theme vars */}

/* ── Header auto-hide ── */
.site-header{transition:transform .3s cubic-bezier(.4,0,.2,1);}
.site-header.hdr-hidden{transform:translateY(-100%)}

/* ── Reader sub-nav ── */
.reader-subnav{
  position:sticky;top:0;z-index:55;
  background:color-mix(in srgb,var(--bg) 92%,transparent);
  backdrop-filter:blur(8px);border-bottom:1px solid var(--bg-3);
  transition:transform .3s cubic-bezier(.4,0,.2,1);
}
.reader-subnav.hdr-hidden{transform:translateY(calc(-100% - 50px))}
.reader-subnav-in{
  display:flex;align-items:center;justify-content:space-between;
  padding:.45rem 1.5rem;gap:.75rem;
}
.rdr-book-name{
  font-size:.78rem;color:var(--ink-3);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;text-align:center;
}

/* ── Float bar ── */
.float-bar{
  position:fixed;bottom:1.5rem;left:50%;
  transform:translateX(-50%);
  display:flex;align-items:center;
  background:var(--bg);border:1px solid var(--bg-3);
  border-radius:99px;padding:.35rem .45rem;
  box-shadow:0 4px 28px rgba(0,0,0,.18),0 1px 6px rgba(0,0,0,.08);
  z-index:60;
  transition:transform .3s cubic-bezier(.4,0,.2,1),opacity .3s,bottom .2s;
}
.float-bar.fbar-hidden{
  transform:translateX(-50%) translateY(calc(100% + 2rem));
  opacity:0;pointer-events:none;
}
.fbar-btn{
  display:flex;flex-direction:column;align-items:center;gap:.12rem;
  padding:.45rem .75rem;border-radius:99px;
  font-size:.62rem;font-weight:500;color:var(--ink-2);
  border:none;background:none;cursor:pointer;text-decoration:none;
  transition:background .15s,color .15s;min-width:48px;
}
.fbar-btn:hover{background:var(--bg-2);color:var(--ink);text-decoration:none}
.fbar-disabled{opacity:.28;pointer-events:none}
.fbar-sep{width:1px;height:24px;background:var(--bg-3);margin:0 .1rem;flex-shrink:0}

/* ── Backdrop ── */
.backdrop{
  position:fixed;inset:0;background:rgba(0,0,0,.45);
  z-index:70;opacity:0;pointer-events:none;
  transition:opacity .3s;
}
.backdrop.panel-open{opacity:1;pointer-events:auto}
.no-scroll{overflow:hidden}

/* ── Settings panel ── */
.sp-panel{
  position:fixed;bottom:0;left:0;right:0;
  background:var(--bg);border-top:1px solid var(--bg-3);
  border-radius:16px 16px 0 0;
  z-index:80;
  transform:translateY(100%);
  transition:transform .32s cubic-bezier(.4,0,.2,1);
  max-height:80vh;display:flex;flex-direction:column;
}
.sp-panel.panel-open{transform:translateY(0)}
.sp-handle{
  width:40px;height:4px;border-radius:99px;
  background:var(--bg-3);margin:.6rem auto .2rem;flex-shrink:0;
}
.sp-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:.6rem 1.25rem .5rem;flex-shrink:0;
}
.sp-head-title{font-size:.88rem;font-weight:600}
.sp-close{
  background:none;border:none;cursor:pointer;
  color:var(--ink-3);padding:.2rem;border-radius:var(--r);
  transition:color .15s,background .15s;
}
.sp-close:hover{color:var(--ink);background:var(--bg-2)}
.sp-body{overflow-y:auto;padding:.5rem 1.25rem 2rem}
.sp-group{margin-bottom:1.1rem}
.sp-label{
  font-size:.68rem;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;color:var(--ink-3);margin-bottom:.5rem;
}
.sp-row{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center}
.sp-fs-row{align-items:center;gap:.75rem}
.sp-fs-disp{
  font-size:.82rem;color:var(--ink-2);min-width:3.5rem;text-align:center;
}
.sp-ctrl{
  background:var(--bg-2);border:1px solid var(--bg-3);border-radius:var(--r);
  color:var(--ink-2);padding:.35rem .8rem;cursor:pointer;font-family:var(--uf);
  transition:background .15s,color .15s;font-size:.85rem;
}
.sp-ctrl:hover{background:var(--bg-3);color:var(--ink)}
.sp-opt{
  padding:.4rem .9rem;border-radius:99px;border:1px solid var(--bg-3);
  background:var(--bg-2);color:var(--ink-2);font-size:.82rem;
  cursor:pointer;transition:all .15s;font-family:var(--uf);
}
.sp-opt:hover{border-color:var(--gold);color:var(--ink)}
.sp-opt.sp-active,.sp-bg.sp-active{
  background:var(--gold);color:#fff;border-color:var(--gold);
}
.sp-bg{
  padding:.4rem .9rem;border-radius:99px;border:1px solid var(--bg-3);
  font-size:.82rem;cursor:pointer;transition:all .15s;font-family:var(--uf);
}
.sp-bg:hover{border-color:var(--gold)}

/* ── TOC Drawer ── */
.toc-drawer{
  position:fixed;top:0;right:0;bottom:0;
  width:min(380px,92vw);
  background:var(--bg);border-left:1px solid var(--bg-3);
  z-index:80;display:flex;flex-direction:column;
  transform:translateX(100%);
  transition:transform .32s cubic-bezier(.4,0,.2,1);
}
.toc-drawer.panel-open{transform:translateX(0)}
.td-head{
  display:flex;align-items:flex-start;justify-content:space-between;
  gap:.75rem;padding:1rem 1rem .85rem;
  border-bottom:1px solid var(--bg-3);flex-shrink:0;
}
.td-book-title{font-size:.88rem;font-weight:600;line-height:1.3}
.td-ch-count{font-size:.72rem;color:var(--ink-3);margin-top:.2rem}
.td-close{
  background:none;border:none;cursor:pointer;color:var(--ink-3);
  padding:.2rem;border-radius:var(--r);flex-shrink:0;margin-top:.1rem;
  transition:color .15s,background .15s;
}
.td-close:hover{color:var(--ink);background:var(--bg-2)}
.td-list{list-style:none;overflow-y:auto;flex:1;padding:.35rem 0}
.td-list li{border-bottom:1px solid var(--bg-2)}
.td-list li a{
  display:flex;align-items:baseline;gap:.65rem;
  padding:.6rem 1rem;color:var(--ink-2);
  transition:background .12s,color .12s;text-decoration:none;
}
.td-list li a:hover{background:var(--bg-2);color:var(--ink);text-decoration:none}
.td-list li.td-cur a{
  color:var(--gold);font-weight:600;
  background:color-mix(in srgb,var(--gold) 8%,transparent);
}
.td-num{min-width:2.2rem;text-align:right;font-size:.7rem;color:var(--ink-3);flex-shrink:0}
.td-title{font-size:.84rem;line-height:1.35}

/* ── Admin link ── */
.admin-link{opacity:0;transition:opacity .25s}
.nav-wrap:hover .admin-link{opacity:1}

@media(prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


# ══════════════════════════════════════════════════════════════════════════════
#  JavaScript
# ══════════════════════════════════════════════════════════════════════════════

JS = r"""
(function () {
  'use strict';

  /* ── Theme ── */
  const html = document.documentElement;
  const btn  = document.getElementById('theme-btn');
  const moon = btn && btn.querySelector('.icon-moon');
  const sun  = btn && btn.querySelector('.icon-sun');

  function applyTheme(t) {
    html.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
    if (moon) moon.style.display = t === 'dark' ? 'none' : '';
    if (sun)  sun.style.display  = t === 'dark' ? '' : 'none';
  }

  const stored = localStorage.getItem('theme')
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(stored);

  if (btn) btn.addEventListener('click', () => {
    applyTheme(html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });

  /* ── Font size (reader) ── */
  const body  = document.getElementById('ch-body');
  const fsInc = document.getElementById('fs-inc');
  const fsDec = document.getElementById('fs-dec');
  let fs = parseFloat(localStorage.getItem('fs') || '1.2');

  function applyFs(v) {
    v = Math.min(2.0, Math.max(0.9, Math.round(v * 10) / 10));
    fs = v;
    document.documentElement.style.setProperty('--fs', v + 'rem');
    localStorage.setItem('fs', v);
  }
  applyFs(fs);

  if (fsInc) fsInc.addEventListener('click', () => applyFs(fs + 0.1));
  if (fsDec) fsDec.addEventListener('click', () => applyFs(fs - 0.1));

  /* ── Restore scroll position within session ── */
  if (body) {
    const key = 'pos:' + location.pathname;
    const saved = sessionStorage.getItem(key);
    if (saved) requestAnimationFrame(() => window.scrollTo(0, parseInt(saved)));
    window.addEventListener('beforeunload', () => {
      sessionStorage.setItem(key, window.scrollY);
    });
  }
})();
"""


# ══════════════════════════════════════════════════════════════════════════════
#  SITE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_site(
    books_full:   list,          # books that need chapter HTML regenerated
    books_cached: list,          # books loaded from cache (skip chapter HTML)
    output_dir:   Path,
    site_name:    str  = "Thư viện",
    base_url:     str  = "",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Assets
    assets = output_dir / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "style.css").write_text(CSS, encoding="utf-8")
    (assets / "app.js").write_text(JS, encoding="utf-8")

    env = Environment(loader=DictLoader(_T), autoescape=False)
    ctx = {"site_name": site_name, "base_url": base_url}

    all_books = books_full + books_cached

    # ── Root index ────────────────────────────────────────────────────────────
    html = env.get_template("index.html").render(books=all_books, **ctx)
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"\n✅  index.html  ({len(all_books)} books)")

    # ── Per-book pages ─────────────────────────────────────────────────────────
    for book in all_books:
        book_dir = output_dir / book["slug"]
        book_dir.mkdir(exist_ok=True)
        chapters = book["chapters"]

        # Book TOC page (always regenerated)
        html = env.get_template("book.html").render(book=book, chapters=chapters, **ctx)
        (book_dir / "index.html").write_text(html, encoding="utf-8")

        is_cached = book in books_cached
        if is_cached:
            print(f"♻️   {book['slug']}/  (cached — skipping chapter HTML)")
            continue

        # Chapter HTML pages
        tpl = env.get_template("chapter.html")
        # Strip content from chapters list for the TOC drawer (avoid huge HTML)
        chapters_toc = [{"number": ch["number"], "title": ch["title"], "slug": ch["slug"]}
                        for ch in chapters]
        for i, ch in enumerate(chapters):
            html = tpl.render(
                book=book, chapter=ch,
                chapters=chapters_toc,
                prev_chapter=chapters[i - 1] if i > 0 else None,
                next_chapter=chapters[i + 1] if i < len(chapters) - 1 else None,
                **ctx,
            )
            (book_dir / f"{ch['slug']}.html").write_text(html, encoding="utf-8")

        print(f"✅  {book['slug']}/  ({len(chapters)} chapters)")

    # ── Public manifest ───────────────────────────────────────────────────────
    manifest = [
        {k: v for k, v in b.items() if k != "chapters"}
        | {"chapters": [{"number": c["number"], "title": c["title"], "slug": c["slug"]}
                        for c in b["chapters"]]}
        for b in all_books
    ]
    (output_dir / "books.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_ch = sum(b["chapter_count"] for b in all_books)

    # ── Admin UI ──────────────────────────────────────────────────────────────
    admin_src = Path(__file__).parent / "admin.html"
    if admin_src.exists():
        admin_dir = output_dir / "admin"
        admin_dir.mkdir(exist_ok=True)
        shutil.copy(admin_src, admin_dir / "index.html")
        print(f"✅  admin/index.html  (admin dashboard)")
    else:
        print(f"⚠️   admin.html not found — skipping admin UI generation")

    print(f"\n🎉  Done!  Books: {len(all_books)}  |  Chapters: {total_ch}")
    print(f"    Output: {output_dir.resolve()}")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert EPUB → static reading website (Cloudflare Pages ready)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input",  nargs="?", default="books",
                    help="EPUB file or directory (default: ./books/)")
    ap.add_argument("output", nargs="?", default="output",
                    help="Output directory (default: ./output/)")
    ap.add_argument("--site-name", default="Thư viện",
                    help='Site title (default: "Thư viện")')
    ap.add_argument("--base-url",  default="",
                    help="Base URL, e.g. https://yoursite.pages.dev")
    ap.add_argument("--no-cache",  action="store_true",
                    help="Re-process all books (ignore .meta.json cache)")
    args = ap.parse_args()

    src        = Path(args.input)
    output_dir = Path(args.output)

    # Collect EPUB paths
    if src.is_file() and src.suffix.lower() == ".epub":
        epub_files = [src]
    elif src.is_dir():
        epub_files = sorted(src.glob("*.epub"))
    else:
        print(f"❌  Not found: {src}")
        print("    Create a ./books/ folder and put your .epub files there.")
        sys.exit(1)

    if not epub_files:
        print(f"⚠️   No .epub files in: {src} — generating empty site (normal on first deploy)")
        generate_site(
            books_full=[], books_cached=[],
            output_dir=output_dir,
            site_name=args.site_name,
            base_url=args.base_url,
        )
        sys.exit(0)  # exit 0 = success so CF Pages build passes

    print(f"📚  Found {len(epub_files)} EPUB file(s)")

    books_full:   list = []   # freshly parsed (full content)
    books_cached: list = []   # loaded from .meta.json (no content)

    for ep in epub_files:
        slug      = slugify(ep.stem)
        book_dir  = output_dir / slug
        meta      = None if args.no_cache else load_meta(book_dir)

        # Cache hit: .meta.json exists AND chapter HTML files exist
        if meta and (book_dir / f"{meta['chapters'][0]['slug']}.html").exists():
            print(f"⏭️   Cached: {ep.name}")
            books_cached.append(meta)
        else:
            book_dir.mkdir(parents=True, exist_ok=True)
            try:
                data = parse_epub(ep, book_dir)
                books_full.append(data)
            except Exception as exc:
                print(f"⚠️   Error: {ep.name} — {exc}")
                import traceback; traceback.print_exc()

    if not (books_full or books_cached):
        print("❌  Nothing to generate.")
        sys.exit(1)

    print(f"\n🔨  Generating site…")
    generate_site(
        books_full   = books_full,
        books_cached = books_cached,
        output_dir   = output_dir,
        site_name    = args.site_name,
        base_url     = args.base_url,
    )

    print(f"""
┌─────────────────────────────────────────────────┐
│  Cloudflare Pages — Build settings              │
├─────────────────────────────────────────────────┤
│  Build command:                                 │
│    pip install -r requirements.txt &&           │
│    python epub_to_web.py                        │
│                                                 │
│  Build output directory:  output               │
│  Root directory:          /                    │
└─────────────────────────────────────────────────┘
""")


if __name__ == "__main__":
    main()
