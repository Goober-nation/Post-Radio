"""Regenerate config.example.yaml from postradio/config.py so the two can't drift (a test checks it)."""
import dataclasses
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from postradio.config import Config  # noqa: E402

USER = ("defaults", "storage", "sources", "feedback", "server")


def main():
    c = dataclasses.asdict(Config())
    c["advanced"]["radio_mode_thresholds"] = list(c["advanced"]["radio_mode_thresholds"])
    out = ["# postradio configuration. Copy to config.yaml (or point $POSTRADIO_CONFIG / --config at it).",
           "# Only keys you change are needed. See postradio/config.py for what each one does.", "",
           "# ---------------- user settings ----------------",
           yaml.safe_dump({k: c[k] for k in USER}, sort_keys=False, allow_unicode=True).rstrip(), "",
           "# ---------------- advanced: algorithm constants ----------------",
           "# Changing these alters ranking behaviour; defaults are tuned by hand on ~10 seeds.",
           yaml.safe_dump({"advanced": c["advanced"]}, sort_keys=False, allow_unicode=True, width=200).rstrip(), ""]
    (ROOT / "config.example.yaml").write_text("\n".join(out))


if __name__ == "__main__":
    main()
