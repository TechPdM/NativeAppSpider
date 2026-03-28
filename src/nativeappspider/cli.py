"""CLI entry point for NativeAppSpider."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click
import yaml

from nativeappspider.analyzer import check_api_key
from nativeappspider.crawler import CrawlConfig, Crawler, load_checkpoint
from nativeappspider.device import ADBError, Device
from nativeappspider.reporter import generate_html_report


def _load_config_file(path: str) -> dict:
    """Load a YAML config file and return its contents as a dict."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        click.echo(f"Error: Config file not found: {path}", err=True)
        sys.exit(1)
    except yaml.YAMLError as e:
        click.echo(f"Error: Invalid YAML in {path}: {e}", err=True)
        sys.exit(1)

    if not isinstance(data, dict):
        click.echo(f"Error: Config file must be a YAML mapping, got {type(data).__name__}", err=True)
        sys.exit(1)

    return data


def _merge_config(cli_params: dict, ctx: click.Context, config_data: dict) -> dict:
    """Merge config file values with CLI params. CLI args take priority.

    A CLI param overrides the config file only if the user explicitly
    provided it on the command line (i.e. it's not just the default).
    """
    merged = dict(config_data)

    for key, value in cli_params.items():
        # Check if this param was explicitly passed on the CLI
        source = ctx.get_parameter_source(key)
        if source == click.core.ParameterSource.COMMANDLINE:
            merged[key] = value
        elif key not in merged:
            # Not in config file either — use the CLI default
            merged[key] = value

    return merged


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """NativeAppSpider — automated mobile app UI crawler."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


@main.command()
@click.argument("package", required=False, default=None)
@click.option("--config", "config_file", default=None, type=click.Path(),
              help="YAML config file (CLI args override file values)")
@click.option("--max-screens", default=50, help="Maximum unique screens to discover")
@click.option("--max-actions", default=200, help="Maximum actions to take")
@click.option("--max-depth", default=10, help="Maximum navigation depth before backtracking")
@click.option("--output", default="output", help="Output directory")
@click.option("--serial", default=None, help="ADB device serial (optional)")
@click.option("--delay", default=1.5, type=float, help="Seconds to wait after each action")
@click.option("--model", default=None, help="Claude model to use (e.g. claude-sonnet-4-5-20241022)")
@click.option("--fresh", is_flag=True, help="Clear app data before crawling (starts from initial screen)")
@click.option("--avoid", multiple=True, help="Flows to avoid, e.g. --avoid registration --avoid login")
@click.option("--dismiss", multiple=True, help="Screens to dismiss quickly, e.g. --dismiss consent --dismiss privacy")
@click.option("--focus", default=None, help="Navigate to this screen first, then explore from there")
@click.option("--scroll-discovery/--no-scroll-discovery", default=True,
              help="Scroll containers to find off-screen elements")
@click.option("--record", is_flag=True, help="Record crawl steps for replay test fixtures")
@click.option("--continue", "continue_from", default=None, type=click.Path(exists=True),
              help="Resume a previous crawl from its output directory")
@click.pass_context
def crawl(
    ctx: click.Context,
    package: str | None,
    config_file: str | None,
    max_screens: int,
    max_actions: int,
    max_depth: int,
    output: str,
    serial: str | None,
    delay: float,
    model: str | None,
    fresh: bool,
    avoid: tuple[str, ...],
    dismiss: tuple[str, ...],
    focus: str | None,
    scroll_discovery: bool,
    record: bool,
    continue_from: str | None,
) -> None:
    """Crawl an app's UI and document all screens and flows.

    PACKAGE is the Android package name (e.g. com.example.app).
    Can also be specified in the config file.
    """
    # --- Resume mode ---
    resume_state = None
    if continue_from:
        crawl_dir = Path(continue_from)
        if not (crawl_dir / "screens.json").exists():
            click.echo(f"Error: {crawl_dir} is not a valid crawl output directory", err=True)
            sys.exit(1)

        resume_state, saved_config = load_checkpoint(crawl_dir)

        # Use saved config as base, allow CLI overrides for budget params
        package = package or saved_config.package
        source = ctx.get_parameter_source
        if source("max_screens") != click.core.ParameterSource.COMMANDLINE:
            max_screens = saved_config.max_screens
        if source("max_actions") != click.core.ParameterSource.COMMANDLINE:
            max_actions = saved_config.max_actions
        if source("max_depth") != click.core.ParameterSource.COMMANDLINE:
            max_depth = saved_config.max_depth
        delay = saved_config.settle_delay
        avoid = tuple(saved_config.avoid_flows)
        dismiss = tuple(saved_config.dismiss_flows)
        focus = saved_config.focus_screen
        scroll_discovery = saved_config.scroll_discovery
        output = str(crawl_dir.parent)

        click.echo(f"Resuming from {crawl_dir} ({len(resume_state.screens)} screens, "
                    f"{resume_state.action_count} actions)")

    # --- Config file mode ---
    elif config_file:
        config_data = _load_config_file(config_file)
        cli_params = {
            "package": package,
            "max_screens": max_screens,
            "max_actions": max_actions,
            "max_depth": max_depth,
            "output": output,
            "serial": serial,
            "delay": delay,
            "model": model,
            "fresh": fresh,
            "avoid": avoid,
            "dismiss": dismiss,
            "focus": focus,
            "scroll_discovery": scroll_discovery,
        }
        merged = _merge_config(cli_params, ctx, config_data)

        package = merged.get("package")
        max_screens = merged.get("max_screens", 50)
        max_actions = merged.get("max_actions", 200)
        max_depth = merged.get("max_depth", 10)
        output = merged.get("output", "output")
        serial = merged.get("serial")
        delay = merged.get("delay", 1.5)
        model = merged.get("model")
        fresh = merged.get("fresh", False)
        focus = merged.get("focus")
        scroll_discovery = merged.get("scroll_discovery", True)
        # avoid/dismiss can be a list in YAML or a tuple from CLI
        avoid_raw = merged.get("avoid", ())
        avoid = tuple(avoid_raw) if isinstance(avoid_raw, list) else avoid_raw
        dismiss_raw = merged.get("dismiss", ())
        dismiss = tuple(dismiss_raw) if isinstance(dismiss_raw, list) else dismiss_raw

    if not package:
        click.echo("Error: PACKAGE is required (provide as argument or in config file)", err=True)
        sys.exit(1)

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

    if fresh and not continue_from:
        click.echo(f"Clearing app data for {package}...")
        try:
            device.clear_app_data(package)
        except ADBError as e:
            click.echo(f"Warning: Failed to clear app data: {e}", err=True)

    if avoid:
        click.echo(f"Avoiding flows: {', '.join(avoid)}")
    if dismiss:
        click.echo(f"Dismissing screens: {', '.join(dismiss)}")
    if focus:
        click.echo(f"Focusing on: {focus}")

    config = CrawlConfig(
        package=package,
        max_screens=max_screens,
        max_actions=max_actions,
        max_depth=max_depth,
        output_dir=output,
        settle_delay=delay,
        avoid_flows=list(avoid),
        dismiss_flows=list(dismiss),
        focus_screen=focus,
        scroll_discovery=scroll_discovery,
    )

    try:
        crawler = Crawler(config, device, model=model, record=record,
                          resume_state=resume_state)
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
