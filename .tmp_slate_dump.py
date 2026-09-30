import json, sys

raw = json.load(open("/tmp/tpl_raw.json"))
blocks = raw["raw"]["content"]["content"]


def flat(n):
    if isinstance(n, dict):
        return n.get("text", "") + "".join(flat(c) for c in n.get("children", []))
    return ""


def cellinfo(cell):
    attrs = {k: v for k, v in cell.items() if k not in ("children", "id", "type")}
    leaf = []
    for p in cell.get("children", []):
        pa = {k: v for k, v in p.items() if k not in ("children", "id")}
        for lf in p.get("children", []):
            leaf.append({k: v for k, v in lf.items()})
        pa["leaves"] = leaf
        return {"cell": attrs, "p": pa}
    return {"cell": attrs, "p": None}


mode = sys.argv[1]
if mode == "rows":
    bi, lo, hi = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    rows = blocks[bi]["children"]
    print("rows", len(rows))
    for r in range(lo, min(hi, len(rows))):
        row = rows[r]
        ra = {k: v for k, v in row.items() if k not in ("children", "id")}
        print("== row", r, ra, "cells", len(row["children"]))
        for c, cell in enumerate(row["children"]):
            print("  ", c, json.dumps(cellinfo(cell), ensure_ascii=False))
elif mode == "texts":
    bi = int(sys.argv[2])
    rows = blocks[bi]["children"]
    for r, row in enumerate(rows):
        print(r, [flat(c) for c in row["children"]])
elif mode == "block":
    print(json.dumps(blocks[int(sys.argv[2])], ensure_ascii=False, indent=1))