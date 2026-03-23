"""CLI entry point for AppSpider."""

from __future__ import annotations

from pathlib import Path

import click

from appspider.crawler import CrawlConfig, Crawler
from appspider.device import Device
from appspider.reporter import generate_html_report


@click.group()
def main() -> None:
    """AppSpider — automated mobile app UI crawler."""


@main.command()
@click.argument("package")
@click.option("--max-screens", default=50, help="Maximum unique screens to discover")
@click.option("--max-actions", default=200, help="Maximum actions to take")
@click.option("--max-depth", default=10, help="Maximum navigation depth before backtracking")
@click.option("--output", default="output", help="Output directory")
@click.option("--serial", default=None, help="ADB device serial (optional)")
@click.option("--delay", default=1.5, type=float, help="Seconds to wait after each action")
def crawl(
    package: str,
    max_screens: int,
    max_actions: int,
    max_depth: int,
    output: str,
    serial: str | None,
    delay: float,
) -> None:
    """Crawl an app's UI and document all screens and flows.

    PACKAGE is the Android package name (e.g. com.example.app).
    """
    config = CrawlConfig(
        package=package,
        max_screens=max_screens,
        max_actions=max_actions,
        max_depth=max_depth,
        output_dir=output,
        settle_delay=delay,
    )
    device = Device(serial=serial)
    crawler = Crawler(config, device)

    click.echo(f"Starting crawl of {package}...")
    state = crawler.crawl()

    click.echo(f"\nDiscovered {len(state.screens)} screens")
    click.echo(f"Output: {state.output_dir}")

    # Auto-generate report
    report = generate_html_report(state.output_dir)
    click.echo(f"Report: {report}")


@main.command()
@click.argument("crawl_dir", type=click.Path(exists=True, path_type=Path))
def report(crawl_dir: Path) -> None:
    """Generate an HTML report from a previous crawl.

    CRAWL_DIR is the path to a crawl output directory.
    """
    report_path = generate_html_report(crawl_dir)
    click.echo(f"Report generated: {report_path}")


if __name__ == "__main__":
    main()
