from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from peixao import run_v6


if __name__ == "__main__":
    result = run_v6()
    print(json.dumps(result, indent=2, ensure_ascii=False))
