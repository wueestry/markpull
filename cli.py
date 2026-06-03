import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

import config as cfg
import converter as conv

app = typer.Typer(
    name="markpull",
    help="Pull clean Markdown from a URL, HTML file, or PDF.",
    add_completion=False,
    no_args_is_help=True,
)

err = Console(stderr=True)


@app.command()
def pull(
    source: str = typer.Argument(..., help="URL, HTML file, or PDF to convert."),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="Write output to file (default: stdout)."
    ),
    config_file: Optional[Path] = typer.Option(
        None, "--config", "-c", help="JSON/YAML config file (Obsidian Web Clipper format)."
    ),
    images_dir: Optional[Path] = typer.Option(
        None, "--images-dir", "-i",
        help="Save images here. Defaults to <output-stem>_assets/ next to the output file.",
    ),
    no_images: bool = typer.Option(False, "--no-images", help="Skip image extraction."),
    scale: float = typer.Option(2.0, "--scale", "-s", help="Image scale factor for extracted pictures (default: 2.0)."),
    frontmatter: bool = typer.Option(
        True, "--frontmatter/--no-frontmatter",
        help="Include YAML frontmatter (ignored when --config is set).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show progress details."),
) -> None:
    extract_images = not no_images

    # Load config — explicit flag, then default location, then none
    template_config: cfg.MarkpullConfig | None = None
    _default = cfg.default_config_path()
    resolved_config = config_file or (_default if _default.exists() else None)
    if resolved_config:
        try:
            template_config = cfg.load_config(resolved_config)
            if config_file is None and verbose:
                err.print(f"[dim]Using default config:[/dim] {resolved_config}")
        except Exception as exc:
            err.print(f"[bold red]Config error:[/bold red] {exc}")
            raise typer.Exit(1)

    # Pre-fetch metadata so we can resolve the output path (and assets dir) before
    # running the full docling pipeline — avoids embedding images into a temp result
    # that we'd have to re-convert once the path is known.
    prefetched: conv.Metadata | None = None
    if template_config and output is None:
        prefetched = conv.fetch_metadata(source)
        variables_preview = cfg.build_variables(
            content="",
            title=prefetched.title,
            url=prefetched.url,
            author=prefetched.author,
            published=prefetched.published,
            description=prefetched.description,
            site=prefetched.site,
        )
        output = cfg.resolve_output_path(template_config, variables_preview)
        if output and verbose:
            err.print(f"[dim]Output path from config:[/dim] {output}")

    # Derive assets dir from the output path unless explicitly set or images are skipped
    if extract_images and images_dir is None and output is not None:
        images_dir = output.parent / f"{output.stem}_assets"

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=err,
        transient=True,
    ) as progress:
        progress.add_task(f"Converting [cyan]{source}[/cyan]…", total=None)
        try:
            result = conv.convert(
                source,
                images_dir=images_dir,
                extract_images=extract_images,
                images_scale=scale,
                _prefetched_metadata=prefetched,
            )
        except Exception as exc:
            err.print(f"[bold red]Error:[/bold red] {exc}")
            raise typer.Exit(1)

    # Render output
    if template_config:
        variables = cfg.build_variables(
            content=result.markdown,
            title=result.metadata.title,
            url=result.metadata.url,
            author=result.metadata.author,
            published=result.metadata.published,
            description=result.metadata.description,
            site=result.metadata.site,
        )
        rendered = cfg.render_output(template_config, variables)
        # Resolve output path now if it wasn't pre-resolved (e.g. -o not given, no config path)
        if output is None:
            output = cfg.resolve_output_path(template_config, variables)
    else:
        rendered = result.render(frontmatter=frontmatter)

    # Write output
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        err.print(f"[green]Saved:[/green] {output}")
        if images_dir and images_dir.exists():
            n = sum(1 for _ in images_dir.iterdir())
            err.print(f"[green]Images:[/green] {n} file(s) in {images_dir}/")
        if result.metadata.title and verbose:
            err.print(f"[dim]Title:[/dim] {result.metadata.title}")
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
