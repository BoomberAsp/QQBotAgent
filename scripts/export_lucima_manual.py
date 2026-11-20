"""Export the shared Lucima manual for the static public site."""

from pathlib import Path
from runpy import run_path
from shutil import copyfile


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    source = root / "QQBot" / "lucima_bridge"
    target = root / "public_site" / "lucima"
    render_manual = run_path(str(source / "manual.py"))["render_manual"]
    target.mkdir(exist_ok=True)
    (target / "index.html").write_text(render_manual(stylesheet="./manual.css"), encoding="utf-8", newline="\n")
    copyfile(source / "manual.css", target / "manual.css")


if __name__ == "__main__":
    main()
