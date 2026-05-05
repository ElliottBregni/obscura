"""Standalone CLI for scraping external agent configs into ~/.obscura.

Exists as a dedicated console script (``obscura-scrape``) because
``obscura``'s top-level group consumes a positional PROMPT argument
that shadows subcommand names. The same logic is also wired into
``obscura scrape-configs`` for parity once that routing is fixed.
"""

from __future__ import annotations

import sys

import click

from obscura.core.scrape_configs import (
    apply as scrape_apply,
    known_sources,
    scan as scrape_scan,
)


@click.command("scrape")
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Actually copy/merge. Without this flag, only previews.",
)
@click.option(
    "--source",
    "source_name",
    default=None,
    help="Limit to one source label (e.g. 'claude', 'kiro', 'codex-toml').",
)
@click.option(
    "--list-sources",
    is_flag=True,
    default=False,
    help="Print known source labels and exit.",
)
def main(do_apply: bool, source_name: str | None, list_sources: bool) -> None:
    """Scrape skills + MCPs from external agent configs into ~/.obscura.

    Sources scanned: ~/.claude, ~/.copilot, ~/.codex, ~/.config/opencode,
    ~/.kiro, and Claude Desktop. Duplicates by name are skipped.

    By default this runs in preview mode — pass --apply to import.
    """
    if list_sources:
        for src in known_sources():
            present = "✓" if src.path.exists() else " "
            click.echo(f"  [{present}] {src.label:18s} {src.kind:14s} {src.path}")
        return

    sources = known_sources()
    if source_name:
        sources = [s for s in sources if s.label == source_name]
        if not sources:
            click.echo(
                f"No source labelled '{source_name}'. Known: "
                + ", ".join(s.label for s in known_sources()),
                err=True,
            )
            sys.exit(1)

    report = scrape_scan(sources=sources)

    click.echo("=== Skills ===")
    if report.skills_new:
        for src, entry, target in report.skills_new:
            click.echo(f"  + {target:30s}  ← {src.label} ({entry.name})")
    else:
        click.echo("  (none new)")
    if report.skills_skipped:
        click.echo(f"  skipped {len(report.skills_skipped)} duplicate(s)")

    click.echo("\n=== MCPs ===")
    if report.mcps_new:
        for src, name, cfg in report.mcps_new:
            transport = cfg.get("transport", "?")
            click.echo(f"  + {name:20s}  [{transport}]  ← {src.label}")
    else:
        click.echo("  (none new)")
    if report.mcps_skipped:
        click.echo(f"  skipped {len(report.mcps_skipped)} duplicate(s)")

    if report.sources_missing:
        click.echo(
            "\n(missing: " + ", ".join(s.label for s in report.sources_missing) + ")",
        )
    if report.errors:
        click.echo("\nErrors:")
        for src, msg in report.errors:
            click.echo(f"  ! {src.label}: {msg}", err=True)

    if not report.has_changes:
        click.echo("\nNothing to import.")
        return

    if not do_apply:
        click.echo("\nPreview only. Re-run with --apply to import.")
        return

    skills_added, mcps_added = scrape_apply(report)
    click.echo(f"\nImported: {skills_added} skill(s), {mcps_added} MCP server(s).")


if __name__ == "__main__":
    main()
