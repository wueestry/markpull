import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from docling.backend.html_backend import HTMLBackendOptions
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
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


def fetch_metadata(source: str) -> Metadata:
    """Lightweight metadata-only fetch — no docling, just HTTP + HTML parsing."""
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
    return Metadata()


def convert(
    source: str,
    images_dir: Path | None = None,
    extract_images: bool = True,
    _prefetched_metadata: Metadata | None = None,
) -> PullResult:
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
    pdf_options.images_scale = 2.0
    pdf_options.generate_picture_images = extract_images
    pdf_options.generate_page_images = False

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
    """Use docling's save_as_markdown to write images directly to disk.

    docling always embeds the absolute artifacts_dir path in markdown references,
    so we replace it afterwards with just the directory name (relative to the
    markdown file, which sits in images_dir.parent).
    """
    images_dir.mkdir(parents=True, exist_ok=True)
    abs_dir = images_dir.resolve()
    tmp = images_dir.parent / f".markpull-{uuid.uuid4().hex[:8]}.md"
    try:
        doc.save_as_markdown(tmp, artifacts_dir=abs_dir, image_mode=ImageRefMode.REFERENCED)
        md = tmp.read_text(encoding="utf-8")
        # "!/abs/path/to/assets/img.png" → "!/assets_dirname/img.png"
        return md.replace(str(abs_dir) + "/", images_dir.name + "/")
    finally:
        tmp.unlink(missing_ok=True)


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
