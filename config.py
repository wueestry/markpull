"""
Config loader and template renderer compatible with Obsidian Web Clipper's JSON format.

Template variables: {{title}}, {{url}}, {{author}}, {{published}},
                    {{description}}, {{site}}, {{content}}, {{date}}
Filters:  |upper  |lower  |strip  |slug  |replace:"from":"to"  (chainable)
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any


@dataclass
class Property:
    name: str
    value: str
    type: str = "text"  # text | date | multitext | checkbox | number


@dataclass
class MarkpullConfig:
    note_content_format: str = "{{content}}"
    note_name_format: str = "{{title}}"
    path: str = ""
    properties: list[Property] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "MarkpullConfig":
        props = [
            Property(
                name=p["name"],
                value=p.get("value", ""),
                type=p.get("type", "text"),
            )
            for p in data.get("properties", [])
        ]
        return cls(
            note_content_format=data.get("noteContentFormat", "{{content}}"),
            note_name_format=data.get("noteNameFormat", "{{title}}"),
            path=data.get("path", ""),
            properties=props,
        )


def load_config(path: str | Path) -> MarkpullConfig:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(text)
        except ImportError as exc:
            raise SystemExit("Install pyyaml to use YAML config files.") from exc
    else:
        data = json.loads(text)
    return MarkpullConfig.from_dict(data)


# ── template engine ──────────────────────────────────────────────────────────

# Matches {{varName}} or {{varName|filter1|filter2|...}}
_TOKEN_RE = re.compile(r"\{\{(\w+)((?:\|[^}]*)*)\}\}")
# Matches replace:"from":"to" (handles escaped quotes via [^"]*?)
_REPLACE_RE = re.compile(r'replace:"([^"]*?)":"([^"]*?)"')


def _apply_filter(value: str, f: str) -> str:
    f = f.strip()
    if f == "upper":
        return value.upper()
    if f == "lower":
        return value.lower()
    if f == "strip":
        return value.strip()
    if f == "slug":
        # Remove filesystem-unsafe characters, then collapse hyphens
        safe = re.sub(r'[^\w\s-]', '', value)
        return re.sub(r'[-\s]+', '-', safe).strip('-')
    m = _REPLACE_RE.match(f)
    if m:
        return value.replace(m.group(1), m.group(2))
    return value  # unknown filter → pass through


def render_template(template: str, variables: dict[str, str]) -> str:
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        filter_chain = m.group(2)  # e.g. "|lower|replace:\" \":\"-\""
        value = variables.get(var, "")
        for part in filter_chain.split("|"):
            if part:
                value = _apply_filter(value, part)
        return value

    return _TOKEN_RE.sub(_replace, template)


# ── frontmatter builder ──────────────────────────────────────────────────────

def _render_property(prop: Property, variables: dict[str, str]) -> Any:
    rendered = render_template(prop.value, variables)
    if prop.type == "checkbox":
        return rendered.lower() in ("true", "1", "yes")
    if prop.type == "multitext":
        if not rendered:
            return []
        if "," in rendered:
            return [v.strip() for v in rendered.split(",") if v.strip()]
        return [rendered]
    # text / date / number
    return rendered


def _yaml_scalar(value: str) -> str:
    """Quote a string if it contains YAML special characters."""
    if not value:
        return '""'
    if any(c in value for c in ':#{}[]|>&*!,@`"\'\\'):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _serialize_frontmatter(props: dict) -> str:
    lines = ["---"]
    for key, value in props.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {str(value)}")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_yaml_scalar(str(item))}")
        elif isinstance(value, str):
            lines.append(f"{key}: {_yaml_scalar(value)}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ── public API ───────────────────────────────────────────────────────────────

def build_variables(
    content: str,
    title: str | None = None,
    url: str | None = None,
    author: str | None = None,
    published: str | None = None,
    description: str | None = None,
    site: str | None = None,
) -> dict[str, str]:
    return {
        "content": content,
        "title": title or "",
        "url": url or "",
        "author": author or "",
        "published": published or "",
        "description": description or "",
        "site": site or "",
        "date": date.today().isoformat(),
    }


def render_output(config: MarkpullConfig, variables: dict[str, str]) -> str:
    fm_data: dict[str, Any] = {}
    for prop in config.properties:
        value = _render_property(prop, variables)
        # Keep explicit False/empty-list but skip empty strings
        if value == "":
            continue
        fm_data[prop.name] = value

    body = render_template(config.note_content_format, variables)

    if not fm_data:
        return body

    return _serialize_frontmatter(fm_data) + "\n" + body


def resolve_output_path(config: MarkpullConfig, variables: dict[str, str]) -> Path | None:
    """Return the suggested output path from config, or None if not determinable."""
    name = render_template(config.note_name_format, variables).strip()
    if not name:
        return None
    # Always sanitize the filename regardless of whether |slug was used in the template
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', name)  # Windows + POSIX unsafe chars
    name = re.sub(r'-+', '-', name).strip('-')
    if not name.endswith(".md"):
        name += ".md"
    if config.path:
        return Path(config.path) / name
    return Path(name)
