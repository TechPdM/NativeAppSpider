"""Generate HTML reports from crawl data."""

from __future__ import annotations

import base64
import json
from html import escape as html_escape
from pathlib import Path


def generate_html_report(crawl_dir: Path) -> Path:
    """Generate a self-contained HTML report from crawl output.

    The report is fully self-contained — screenshots are embedded as base64
    data URIs so the HTML file can be shared or opened anywhere without
    needing the original image files.
    """
    screens = json.loads((crawl_dir / "screens.json").read_text())
    transitions = json.loads((crawl_dir / "transitions.json").read_text())
    mermaid = (crawl_dir / "flow.mmd").read_text()  # Mermaid flowchart rendered client-side via CDN

    # Build an HTML card for each discovered screen, showing its screenshot,
    # description, and interactive elements inventory
    cards = []
    for sid, screen in screens.items():
        # Embed screenshot as base64 so the report is portable
        ss_path = Path(screen["screenshot"])
        if ss_path.exists():
            img_data = base64.b64encode(ss_path.read_bytes()).decode()
            img_tag = f'<img src="data:image/png;base64,{img_data}" class="screenshot">'
        else:
            img_tag = '<div class="screenshot placeholder">No screenshot</div>'

        elements_html = ""
        for el in screen.get("elements", []):
            el_type = html_escape(str(el.get("type", "?")))
            el_label = html_escape(str(el.get("label", "unnamed")))
            el_purpose = html_escape(str(el.get("purpose", "")))
            elements_html += (
                f'<li><span class="el-type">{el_type}</span> '
                f'{el_label} — {el_purpose}</li>'
            )

        name = html_escape(screen["screen_name"])
        desc = html_escape(screen["description"])
        activity = html_escape(screen.get("activity", "?"))

        cards.append(f"""
        <div class="screen-card">
            {img_tag}
            <div class="screen-info">
                <h3>{name}</h3>
                <p>{desc}</p>
                <details>
                    <summary>{len(screen.get("elements", []))} elements</summary>
                    <ul>{elements_html}</ul>
                </details>
                <p class="meta">Activity: {activity}<br>
                Visited {screen.get("visit_count", 1)}x</p>
            </div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>NativeAppSpider Report</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; background: #f5f5f5; color: #333; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: 0.5rem; }}
  .stats {{ display: flex; gap: 2rem; margin: 1rem 0; }}
  .stat {{ background: white; padding: 1rem 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .stat-num {{ font-size: 2rem; font-weight: bold; }}
  .screen-card {{ display: flex; gap: 1rem; background: white; margin: 1rem 0; padding: 1rem;
                  border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .screenshot {{ max-width: 200px; max-height: 400px; border-radius: 4px; border: 1px solid #ddd; }}
  .screen-info {{ flex: 1; }}
  .screen-info h3 {{ margin-top: 0; }}
  .el-type {{ background: #e0e0e0; padding: 0.1rem 0.4rem; border-radius: 3px; font-size: 0.8rem; }}
  .meta {{ color: #888; font-size: 0.85rem; }}
  .mermaid {{ background: white; padding: 1rem; border-radius: 8px; margin: 1rem 0; }}
  details {{ margin: 0.5rem 0; }}
  summary {{ cursor: pointer; font-weight: 500; }}
</style>
</head>
<body>
<h1>NativeAppSpider Report</h1>

<div class="stats">
  <div class="stat"><div class="stat-num">{len(screens)}</div>Screens</div>
  <div class="stat"><div class="stat-num">{len(transitions)}</div>Transitions</div>
</div>

<h2>Navigation Flow</h2>
<div class="mermaid">
{mermaid}
</div>

<h2>Screens</h2>
{"".join(cards)}

<script>mermaid.initialize({{startOnLoad: true}});</script>
</body>
</html>"""

    report_path = crawl_dir / "report.html"
    report_path.write_text(html)
    return report_path
