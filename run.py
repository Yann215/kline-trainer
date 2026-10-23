"""Start after pip install -e './backend[dev]' and building frontend."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
from app.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.argv.insert(1, "serve")
    main()
