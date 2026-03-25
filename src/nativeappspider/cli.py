"""CLI entry point for NativeAppSpider."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from nativeappspider.analyzer import check_api_key
from nativeappspider.crawler import CrawlConfig, Crawler
from nativeappspider.device import ADBError, Device
from nativeappspider.reporter import generate_html_report


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """NativeAppSpider — automated mobile app UI crawler."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


@main.command()
@click.argument("package")
@click.option("--max-screens", default=50, help="Maximum unique screens to discover")
@click.option("--max-actions", default=200, help="Maximum actions to take")
@click.option("--max-depth", default=10, help="Maximum navigation depth before backtracking")
@click.option("--output", default="output", help="Output directory")
@click.option("--serial", default=None, help="ADB device serial (optional)")
@click.option("--delay", default=1.5, type=float, help="Seconds to wait after each action")
@click.option("--model", default=None, help="Claude model to use (e.g. claude-sonnet-4-5-20241022)")
@click.option("--fresh", is_flag=True, help="Clear app data before crawling (starts from initial screen)")
@click.option("--avoid", multiple=True, help="Flows to avoid, e.g. --avoid registration --avoid login")
def crawl(
    package: str,
    max_screens: int,
    max_actions: int,
    max_depth: int,
    output: str,
    serial: str | None,
    delay: float,
    model: str | None,
    fresh: bool,
    avoid: tuple[str, ...],
) -> None:
    """Crawl an app's UI and document all screens and flows.

    PACKAGE is the Android package name (e.g. com.example.app).
    """
    # Validate prerequisites before starting — fail fast with a clear message
    # rather than crashing mid-crawl
    try:
        check_api_key()
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    device = Device(serial=serial)
    if not device.is_connected():
        click.echo("Error: No Android device connected. Start an emulator or connect a device.", err=True)
        sys.exit(1)

    try:
        w, h = device.get_screen_size()
        click.echo(f"Device connected ({w}x{h})")
    except ADBError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if fresh:
        click.echo(f"Clearing app data for {package}...")
        try:
            device.clear_app_data(package)
        except ADBError as e:
            click.echo(f"Warning: Failed to clear app data: {e}", err=True)

    if avoid:
        click.echo(f"Avoiding flows: {', '.join(avoid)}")

    config = CrawlConfig(
        package=package,
        max_screens=max_screens,
        max_actions=max_actions,
        max_depth=max_depth,
        output_dir=output,
        settle_delay=delay,
        avoid_flows=list(avoid),
    )

    try:
        crawler = Crawler(config, device, model=model)
        click.echo(f"Starting crawl of {package}...")
        state = crawler.crawl()
    except ADBError as e:
        click.echo(f"Device error: {e}", err=True)
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

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
    # Verify the crawl directory has all the files we need to build a report
    required_files = ["screens.json", "transitions.json", "flow.mmd"]
    missing = [f for f in required_files if not (crawl_dir / f).exists()]
    if missing:
        click.echo(f"Error: Missing files in {crawl_dir}: {', '.join(missing)}", err=True)
        sys.exit(1)

    report_path = generate_html_report(crawl_dir)
    click.echo(f"Report generated: {report_path}")


if __name__ == "__main__":
    main()
