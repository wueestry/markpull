import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from docling.backend.html_backend import HTMLBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import OcrAutoOptions, PdfPipelineOptions
from docling.document_converter import DocumentConverter, HTMLFormatOption, PdfFormatOption
from docling_core.types.doc import ImageRefMode


@dataclass
class Metadata:
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published: str | None = None
    description: str | None = None
    site: str | None = None

    def as_dict(self) -> dict:
        keys = ("title", "url", "author", "published", "description", "site")
        return {k: getattr(self, k) for k in keys if getattr(self, k) is not None}


@dataclass
class PullResult:
    markdown: str
    metadata: Metadata

    def render(self, frontmatter: bool = True) -> str:
        if not frontmatter or not self.metadata.as_dict():
            return self.markdown
        fm_lines = []
        for key, value in self.metadata.as_dict().items():
            escaped = value.replace('"', '\\"')
            fm_lines.append(f'{key}: "{escaped}"')
        fm = "\n".join(fm_lines)
        return f"---\n{fm}\n---\n\n{self.markdown}"


def _is_url(source: str) -> bool:
    try:
        p = urlparse(source)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def _meta_tag(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    tag = None
    if name:
        tag = soup.find("meta", attrs={"name": name})
    if tag is None and prop:
        tag = soup.find("meta", attrs={"property": prop})
    if tag and tag.get("content"):
        return tag["content"].strip() or None
    return None


def _extract_html_metadata(html: str, url: str | None = None) -> Metadata:
    soup = BeautifulSoup(html, "html.parser")

    title = (
        _meta_tag(soup, prop="og:title")
        or _meta_tag(soup, name="twitter:title")
        or (soup.title.get_text(strip=True) if soup.title else None)
        or (soup.find("h1").get_text(strip=True) if soup.find("h1") else None)
    )

    author = (
        _meta_tag(soup, name="author")
        or _meta_tag(soup, prop="article:author")
        or _meta_tag(soup, name="twitter:creator")
    )

    published = (
        _meta_tag(soup, prop="article:published_time")
        or _meta_tag(soup, name="date")
        or _meta_tag(soup, name="pubdate")
        or _meta_tag(soup, prop="og:updated_time")
    )
    if published and "T" in published:
        published = published.split("T")[0]

    description = (
        _meta_tag(soup, prop="og:description")
        or _meta_tag(soup, name="description")
        or _meta_tag(soup, name="twitter:description")
    )

    site = _meta_tag(soup, prop="og:site_name")

    return Metadata(
        title=title,
        url=url,
        author=author,
        published=published,
        description=description,
        site=site,
    )


def _youtube_video_id(url: str) -> str | None:
    """Return the YouTube video ID from any common URL form, or None."""
    p = urlparse(url)
    host = p.netloc.removeprefix("www.")
    if host in ("youtube.com", "m.youtube.com"):
        if p.path == "/watch":
            return parse_qs(p.query).get("v", [None])[0]
        if p.path.startswith(("/embed/", "/shorts/", "/v/")):
            return p.path.split("/")[2] or None
    if host == "youtu.be":
        return p.path.lstrip("/") or None
    return None


def _fetch_youtube(url: str) -> PullResult:
    """Fetch metadata + transcript for a YouTube video."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import CouldNotRetrieveTranscript

    video_id = _youtube_video_id(url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from: {url}")

    # Metadata from og tags
    metadata = Metadata(url=url)
    try:
        resp = httpx.get(url, follow_redirects=True, timeout=30,
                         headers={"User-Agent": "markpull/0.1"})
        resp.raise_for_status()
        metadata = _extract_html_metadata(resp.text, url=url)
    except Exception:
        pass

    # Transcript — try English first, then any available language
    api = YouTubeTranscriptApi()
    snippets = []
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
        snippets = list(fetched)
    except CouldNotRetrieveTranscript:
        try:
            available = api.list(video_id)
            first = next(iter(available), None)
            if first:
                fetched = api.fetch(video_id, languages=[first.language_code])
                snippets = list(fetched)
        except Exception:
            pass
    except Exception:
        pass

    md = _format_transcript(snippets)
    return PullResult(markdown=md, metadata=metadata)


def _format_transcript(snippets: list) -> str:
    if not snippets:
        return "*No transcript available for this video.*"

    lines = ["## Transcript", ""]
    for s in snippets:
        start = int(s.start)
        h, rem = divmod(start, 3600)
        m, sec = divmod(rem, 60)
        ts = f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
        lines.append(f"**[{ts}]** {s.text.strip().replace(chr(10), ' ')}")

    return "\n".join(lines)


def _epub_dc(book, field: str) -> str | None:
    items = book.get_metadata("DC", field)
    for value, _ in items:
        if value and value.strip():
            return value.strip()
    return None


def _extract_epub_metadata(path: str) -> Metadata:
    import ebooklib.epub

    book = ebooklib.epub.read_epub(path, options={"ignore_ncx": True})

    title = _epub_dc(book, "title")

    creators = [v.strip() for v, _ in book.get_metadata("DC", "creator") if v and v.strip()]
    author = ", ".join(creators) if creators else None

    published = _epub_dc(book, "date")
    if published and "T" in published:
        published = published.split("T")[0]

    description = _epub_dc(book, "description")
    site = _epub_dc(book, "publisher")

    return Metadata(title=title, author=author, published=published, description=description, site=site)


def _prepare_epub_for_docling(epub_path: str) -> tuple[Path, Path]:
    import ebooklib
    import ebooklib.epub

    book = ebooklib.epub.read_epub(epub_path, options={"ignore_ncx": True})
    tmp = Path(tempfile.mkdtemp(prefix=".markpull-epub-"))
    images_subdir = tmp / "images"
    images_subdir.mkdir()

    # Extract images and build src → flat-name map
    image_map: dict[str, str] = {}
    used_names: set[str] = set()
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        original = item.get_name()
        flat = Path(original).name
        if flat in used_names:
            flat = f"{len(used_names):03d}-{flat}"
        used_names.add(flat)
        image_map[original] = flat
        image_map[Path(original).name] = flat
        (images_subdir / flat).write_bytes(item.get_content())

    # Build combined HTML from spine items
    title = _epub_dc(book, "title") or ""
    seen_ids: set[str] = set()
    body_parts: list[str] = []

    for idref, _ in book.spine:
        if idref in seen_ids:
            continue
        seen_ids.add(idref)
        item = book.get_item_with_id(idref)
        if item is None:
            continue
        raw = item.get_content()
        if not raw:
            continue
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body") or soup

        for img in body.find_all("img"):
            src = img.get("src", "")
            flat = image_map.get(src) or image_map.get(Path(src).name)
            if flat:
                img["src"] = f"images/{flat}"

        body_parts.append(str(body) if body.name != "body" else body.decode_contents())

    combined = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title></head>'
        f"<body>{'<hr>'.join(body_parts)}</body></html>"
    )
    combined_html = tmp / "content.html"
    combined_html.write_text(combined, encoding="utf-8")
    return combined_html, tmp


def _convert_epub(
    source: str,
    images_dir: Path | None,
    extract_images: bool,
    images_scale: float,
    _prefetched_metadata: "Metadata | None",
) -> "PullResult":
    try:
        metadata = _prefetched_metadata or _extract_epub_metadata(source)
    except Exception:
        metadata = Metadata()

    combined_html, tmp_dir = _prepare_epub_for_docling(source)
    try:
        html_opts = HTMLBackendOptions(
            fetch_images=extract_images,
            enable_local_fetch=extract_images,
            enable_remote_fetch=False,
            source_uri=str(combined_html),
        )
        doc_converter = DocumentConverter(
            format_options={
                InputFormat.HTML: HTMLFormatOption(backend_options=html_opts),
            }
        )
        result = doc_converter.convert(str(combined_html))
        doc = result.document

        if not metadata.title and getattr(doc, "name", None):
            metadata.title = doc.name

        if not extract_images:
            md = doc.export_to_markdown(image_mode=ImageRefMode.PLACEHOLDER)
        elif images_dir is not None:
            md = _save_with_images(doc, images_dir)
        else:
            md = doc.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)

        if metadata.title:
            md = _strip_leading_h1(md, metadata.title)

        return PullResult(markdown=md, metadata=metadata)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def fetch_metadata(source: str) -> Metadata:
    """Lightweight metadata-only fetch — no docling, just HTTP + HTML parsing."""
    if _youtube_video_id(source):
        try:
            resp = httpx.get(source, follow_redirects=True, timeout=30,
                             headers={"User-Agent": "markpull/0.1"})
            resp.raise_for_status()
            return _extract_html_metadata(resp.text, url=source)
        except Exception:
            return Metadata(url=source)
    if _is_url(source):
        try:
            resp = httpx.get(
                source,
                follow_redirects=True,
                timeout=30,
                headers={"User-Agent": "markpull/0.1"},
            )
            resp.raise_for_status()
            if "text/html" in resp.headers.get("content-type", ""):
                return _extract_html_metadata(resp.text, url=source)
        except Exception:
            pass
        return Metadata(url=source)
    if source.lower().endswith((".html", ".htm")):
        try:
            return _extract_html_metadata(Path(source).read_text(errors="replace"))
        except Exception:
            pass
    if source.lower().endswith(".epub"):
        try:
            return _extract_epub_metadata(source)
        except Exception:
            pass
    return Metadata()


def convert(
    source: str,
    images_dir: Path | None = None,
    extract_images: bool = True,
    images_scale: float = 2.0,
    full_page_ocr: bool = False,
    _prefetched_metadata: Metadata | None = None,
) -> PullResult:
    if _youtube_video_id(source):
        return _fetch_youtube(source)

    if not _is_url(source) and source.lower().endswith(".epub"):
        return _convert_epub(
            source,
            images_dir=images_dir,
            extract_images=extract_images,
            images_scale=images_scale,
            _prefetched_metadata=_prefetched_metadata,
        )

    is_url = _is_url(source)

    if _prefetched_metadata is not None:
        metadata = _prefetched_metadata
    else:
        metadata = Metadata(url=source if is_url else None)
        if is_url:
            try:
                resp = httpx.get(
                    source,
                    follow_redirects=True,
                    timeout=30,
                    headers={"User-Agent": "markpull/0.1"},
                )
                resp.raise_for_status()
                if "text/html" in resp.headers.get("content-type", ""):
                    metadata = _extract_html_metadata(resp.text, url=source)
            except Exception:
                pass
        elif source.lower().endswith((".html", ".htm")):
            try:
                metadata = _extract_html_metadata(Path(source).read_text(errors="replace"))
            except Exception:
                pass

    pdf_options = PdfPipelineOptions()
    pdf_options.images_scale = images_scale
    pdf_options.generate_picture_images = extract_images
    pdf_options.generate_page_images = False
    pdf_options.do_formula_enrichment = True
    if full_page_ocr:
        pdf_options.ocr_options = OcrAutoOptions(force_full_page_ocr=True)

    html_backend_options = HTMLBackendOptions(
        fetch_images=extract_images,
        enable_remote_fetch=extract_images,
        enable_local_fetch=extract_images,
        source_uri=source if is_url else None,
    )

    doc_converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
            InputFormat.HTML: HTMLFormatOption(backend_options=html_backend_options),
        }
    )

    result = doc_converter.convert(source)
    doc = result.document

    if not metadata.title and getattr(doc, "name", None):
        metadata.title = doc.name

    if not extract_images:
        md = doc.export_to_markdown(image_mode=ImageRefMode.PLACEHOLDER)
    elif images_dir is not None:
        md = _save_with_images(doc, images_dir)
    else:
        md = doc.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)

    if metadata.title:
        md = _strip_leading_h1(md, metadata.title)

    return PullResult(markdown=md, metadata=metadata)


def _save_with_images(doc, images_dir: Path) -> str:
    """Use docling's save_as_markdown to write images to disk, then rewrite
    standard Markdown image refs as Obsidian wikilinks: ![[filename.png]]."""
    images_dir.mkdir(parents=True, exist_ok=True)
    abs_dir = images_dir.resolve()
    tmp = images_dir.parent / f".markpull-{uuid.uuid4().hex[:8]}.md"
    try:
        doc.save_as_markdown(tmp, artifacts_dir=abs_dir, image_mode=ImageRefMode.REFERENCED)
        md = tmp.read_text(encoding="utf-8")
        return _to_wikilinks(md, abs_dir)
    finally:
        tmp.unlink(missing_ok=True)


def _to_wikilinks(md: str, abs_images_dir: Path) -> str:
    """Rewrite ![alt](abs_images_dir/file.png) → ![[file.png]] in md."""
    prefix = str(abs_images_dir) + "/"

    def repl(m: re.Match) -> str:
        path = m.group(1)
        if path.startswith(prefix):
            return f"![[{path[len(prefix):]}]]"
        return m.group(0)

    return re.sub(r'!\[[^\]]*\]\(([^)]+)\)', repl, md)


def _strip_leading_h1(md: str, title: str) -> str:
    lines = md.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# "):
            h1_text = stripped[2:].strip()
            if h1_text.lower() == title.lower():
                del lines[i]
                if i < len(lines) and not lines[i].strip():
                    del lines[i]
        break
    return "".join(lines)
