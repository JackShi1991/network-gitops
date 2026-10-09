#!/usr/bin/env python3
"""GitOps render: YAML intent + Jinja2 template -> Cisco config."""

from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader

BASE = Path(__file__).parent
DATA = BASE / "data"
TEMPLATES = BASE / "templates"
RENDERED = BASE / "rendered"

def main():
    RENDERED.mkdir(exist_ok=True)

    for yaml_file in DATA.glob("*.yaml"):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)

        hostname = data["hostname"]
        platform = data.get("platform", "cisco_ios")

        env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
        template = env.get_template(f"{platform}/base.j2")

        rendered = template.render(data)

        out_file = RENDERED / f"{hostname}.cfg"
        with open(out_file, "w") as f:
            f.write(rendered)

        print(f"✅ {hostname}.cfg rendered ({len(rendered.splitlines())} lines)")

if __name__ == "__main__":
    main()
