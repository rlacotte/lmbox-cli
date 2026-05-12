"""Allow `python -m lmbox_cli` as an alternative to the `lmbox` script."""

from lmbox_cli.cli import app

if __name__ == "__main__":
    app()
