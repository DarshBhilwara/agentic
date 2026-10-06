import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in ("code", "inference", "benchmarks"):
    sys.path.insert(0, str(ROOT / directory))
