"""Run the general assistant: python -m general.main."""
from importlib import import_module
from pathlib import Path

# Source tree keeps shared code in webui/; the image copies it to /app/.
shared_module = "webui.main" if (Path(__file__).resolve().parents[1] / "webui" / "main.py").is_file() else "main"

if __name__ == "__main__":
    import_module(shared_module).main()
