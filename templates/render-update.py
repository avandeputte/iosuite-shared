"""Render update.sh for an app from templates/update.sh: python3 render-update.py <app dir> NAME LABEL REPO SUBDIR "ITEMS" [groups-file] [after-file]"""
import sys
from pathlib import Path

tpl = (Path(__file__).parent / "update.sh").read_text()
out, name, label, repo, subdir, items = sys.argv[1:7]
groups = Path(sys.argv[7]).read_text().rstrip("\n") if len(sys.argv) > 7 and sys.argv[7] else "  :"
after = Path(sys.argv[8]).read_text().rstrip("\n") if len(sys.argv) > 8 and sys.argv[8] else "  :"
s = (tpl.replace("@@NAME@@", name).replace("@@LABEL@@", label).replace("@@UP@@", name.upper()).replace("@@REPO@@", repo)
     .replace("@@SUBDIR@@", f'"{subdir}"' if subdir else '""').replace("@@ITEMS@@", items).replace("@@GROUPS@@", groups).replace("@@AFTER@@", after))
p = Path(out) / "update.sh"
p.write_text(s)
p.chmod(0o755)
print(p)
