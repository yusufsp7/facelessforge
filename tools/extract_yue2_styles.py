"""Regenerate server/engines/yue2_styles.py from the extracted YuE2 hub catalog."""
import json

cat = json.load(open(r"Y:\_yue2_catalog_raw.json", encoding="utf-8"))

HEADER = '''"""YuE2 style catalog - extracted from YuE2 Studio 3.7.0 embedded hub.json.

32 adapters (style / slider / composition / artist) with their official LoRA
download URLs and English descriptions. Used by the yue2 engine style picker.
Regenerate with tools/extract_yue2_styles if YuE2 Studio updates.
"""


def _s(sid, kind, label, desc, author="", page="", files=None):
    return {"id": sid, "kind": kind, "label": label, "description": desc,
            "author": author, "page": page, "files": files or []}


STYLES = [
'''

lines = [HEADER]
for a in cat["adapters"]:
    files = [{"url": f["url"], "file": f["file"], "bytes": f["bytes"]}
             for f in a.get("files", [])]
    sid = a["id"]
    kind = a["kind"]
    label = a["name"].get("en", sid).replace('"', '\\"')
    desc = a["description"].get("en", "").replace('"', '\\"')
    author = a.get("author", "").replace('"', '\\"')
    page = a.get("page", "")
    lines.append('    _s(%s, %s, %s, %s, %s, %s, %s),\n' % (
        json.dumps(sid, ensure_ascii=False),
        json.dumps(kind, ensure_ascii=False),
        json.dumps(label, ensure_ascii=False),
        json.dumps(desc, ensure_ascii=False),
        json.dumps(author, ensure_ascii=False),
        json.dumps(page, ensure_ascii=False),
        json.dumps(files, ensure_ascii=False)))
lines.append("]\n\n\nSTYLES_BY_ID = {s[\"id\"]: s for s in STYLES}\n")

out = "".join(lines)
open(r"Y:\Software project\Content Automation\contentforge\server\engines\yue2_styles.py",
     "w", encoding="utf-8").write(out)
print("written", len(out), "chars")
