# markpull

Pull clean Markdown from any URL, HTML file, or PDF — with extracted images, tables, formulas, and optional YAML frontmatter.

Built on [Docling](https://github.com/DS4SD/docling) for document parsing and inspired by [Defuddle](https://github.com/kepano/defuddle) for metadata extraction and frontmatter output.

---

## Features

- **Universal input** — HTTP/HTTPS URLs, local HTML files, and PDFs
- **Rich extraction** — tables, code blocks, mathematical formulas, and images preserved in Markdown
- **Local image assets** — images saved as PNG files in a sibling `_assets/` folder; no base64 blobs in your notes
- **Metadata frontmatter** — title, URL, author, publication date, description, and site extracted from Open Graph / meta tags
- **Config templates** — drop in an [Obsidian Web Clipper](https://obsidian.md/clipper) JSON config to control frontmatter properties, note naming, and output path
- **stdout-friendly** — pipe output anywhere; images fall back to embedded base64 when no output file is given

---

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

### Run directly with uv (no install)

```bash
uvx --from . markpull <source>
```

### Install as a tool

```bash
uv tool install .
markpull <source>
```

### Development setup

```bash
git clone <repo>
cd markpull
uv sync
uv run markpull <source>
```

---

## Usage

```
markpull [OPTIONS] SOURCE
```

`SOURCE` can be an `https://` URL, a local `.html` file, or a local `.pdf` file.

### Options

| Flag | Short | Description |
|------|-------|-------------|
| `--output PATH` | `-o` | Write to file instead of stdout |
| `--config PATH` | `-c` | JSON/YAML config file (Obsidian Web Clipper format) |
| `--images-dir PATH` | `-i` | Override the images folder (default: `<stem>_assets/` next to output) |
| `--no-images` | | Skip image extraction entirely |
| `--no-frontmatter` | | Omit YAML frontmatter block |
| `--verbose` | `-v` | Show progress and file details |

---

## Examples

**Clip a web article to stdout:**
```bash
markpull https://example.com/article
```

**Save a PDF with images:**
```bash
markpull report.pdf -o notes/report.md
# images → notes/report_assets/image_000000_*.png
```

**Clip a page with an Obsidian config:**
```bash
markpull https://example.com/article -c obsidian-web-clipper-settings.json
# output path and frontmatter properties are driven by the config
```

**HTML file, no images, no frontmatter:**
```bash
markpull page.html --no-images --no-frontmatter -o page.md
```

---

## Output format

Without a config, output includes a YAML frontmatter block followed by the document body:

```markdown
---
title: "HTB: Voleur"
url: "https://0xdf.gitlab.io/2025/11/01/htb-voleur.html"
author: "0xdf"
published: "2025-11-01"
description: "Voleur is an active directory box..."
site: "0xdf hacks stuff"
---

# HTB: Voleur

...
```

Images are saved locally and referenced by relative path:

```markdown
![Image](htb-voleur_assets/image_000003_abc123.png)
```

---

## Config file (Obsidian Web Clipper format)

Pass a JSON or YAML config with `--config` to control every aspect of the output. The format is compatible with [Obsidian Web Clipper](https://obsidian.md/clipper) templates.

```json
{
  "noteContentFormat": "\n# {{title|upper}}\n\n---\n\n{{content}}",
  "noteNameFormat": "{{title|lower|replace:\" \":\"-\"}}",
  "path": "literature-sources",
  "properties": [
    { "name": "title",     "value": "{{title}}",  "type": "text" },
    { "name": "created",   "value": "{{date}}",   "type": "date" },
    { "name": "tags",      "value": "clippings",  "type": "multitext" },
    { "name": "source",    "value": "{{url}}",    "type": "text" },
    { "name": "processed", "value": "False",      "type": "checkbox" }
  ]
}
```

### Template variables

| Variable | Description |
|----------|-------------|
| `{{title}}` | Document title |
| `{{content}}` | Converted Markdown body |
| `{{url}}` | Source URL |
| `{{date}}` | Today's date (`YYYY-MM-DD`) |
| `{{author}}` | Author (from meta tags) |
| `{{published}}` | Publication date |
| `{{description}}` | Page description |
| `{{site}}` | Site name |

### Filters

Filters can be chained with `|`:

| Filter | Example | Result |
|--------|---------|--------|
| `upper` | `{{title\|upper}}` | `MY TITLE` |
| `lower` | `{{title\|lower}}` | `my title` |
| `strip` | `{{title\|strip}}` | leading/trailing whitespace removed |
| `slug` | `{{title\|slug}}` | `my-title` (URL-safe) |
| `replace:"x":"y"` | `{{title\|replace:" ":"-"}}` | spaces → hyphens |

Output file names are always sanitized to remove filesystem-unsafe characters (`:`, `?`, `*`, etc.) regardless of the template.

---

## How it works

1. **Metadata pre-fetch** — for URLs and HTML files, `httpx` fetches the page and `BeautifulSoup` extracts Open Graph, Twitter Card, and standard meta tags to build the frontmatter before the heavy pipeline runs.
2. **Docling pipeline** — the document is converted by Docling's layout-aware pipeline: PDF layout analysis, table structure recognition, formula detection, and image fetching for HTML sources.
3. **Image export** — Docling's `save_as_markdown` writes each picture to `<stem>_assets/` as a PNG; the Markdown references are rewritten to relative paths.
4. **Template rendering** — if a config is provided, variables are substituted into the `noteContentFormat` template and frontmatter is serialised from the `properties` list.

---

## Dependencies

| Package | Role |
|---------|------|
| [docling](https://github.com/DS4SD/docling) | Document parsing and conversion |
| [httpx](https://www.python-httpx.org/) | HTTP fetching |
| [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) | HTML metadata extraction |
| [typer](https://typer.tiangolo.com/) | CLI framework |
| [rich](https://github.com/Textualize/rich) | Terminal output |
