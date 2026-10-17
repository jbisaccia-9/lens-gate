"""Regenerate RESULTS.md from real command output. Run from the repo root."""
import datetime
import shutil
import subprocess
import sys
from pathlib import Path

STEPS = [
    ("Unit tests", "python -m pytest -q", 0),
    ("Seed the synthetic world", "python -m lensgate seed data", 0),
    ("The seeded morning: five questions, four people, the real broker and gate", "python -m lensgate demo data --out out/run", 0),
    ("Re-grade the morning from disk", "python -m lensgate check-dir out/run --data data", 0),
    ("Directory vs Metabase: the planted drift must be reported", "python -m lensgate sync data", 1),
    ("Build the counterexamples", "python -m lensgate counterexamples out/cx --data data", 0),
    ("Gate refuses every counterexample, each for its own rule", "python -m lensgate check-dir out/cx --data data", 1),
]

shutil.rmtree("out", ignore_errors=True)
shutil.rmtree("data", ignore_errors=True)
out = [f"# Results\n\nGenerated {datetime.date.today()} by `scripts/make_results.py` - every block below is captured command output, not prose.\n"]
ok_all = True
for title, cmd, want in STEPS:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    good = (r.returncode == 0) if want == 0 else (r.returncode != 0)
    ok_all &= good
    exp = "exit 0" if want == 0 else "expected non-zero exit"
    body = (r.stdout + r.stderr).strip()
    if "pytest" in cmd:
        body = "\n".join(l for l in body.splitlines() if not l.startswith("=") or "passed" in l)
    body = "\n".join(l for l in body.splitlines() if "WARNING: Running pip" not in l)
    out.append(f"## {title}\n\n`{cmd}` - {exp}, {'OK' if good else 'UNEXPECTED'}\n\n```\n{body}\n```\n")
Path("RESULTS.md").write_text("\n".join(out), encoding="utf-8")
print("wrote RESULTS.md", "OK" if ok_all else "WITH UNEXPECTED RESULTS")
sys.exit(0 if ok_all else 1)
