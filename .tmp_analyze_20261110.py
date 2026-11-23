import json, re, math, statistics
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
SOURCE = Path('/Users/ext.anle6/Downloads/本地导入_202611101736.html')
def load():
    text = SOURCE.read_text()
    holder = {}
    count = 0
    for m in re.finditer(r'<script\b[^>]*\bid="report-chunk-(\d+)"[^>]*>(.*?)</script>', text, re.S):
        op = json.loads(m.group(2))
        path, value = op[:2]
        parent, key = holder, 'data'
        for part in path:
            parent, key = parent[key], part
        if len(op) == 2:
            if isinstance(parent, list):
                while len(parent) <= key:
                    parent.append(None)
            parent[key] = value
        elif value == 'text':
            parent[key] += op[2]
        else:
            assert len(parent[key]) == value
            parent[key].extend(op[2])
        count += 1
    print('CHUNKS', count)
    return holder['data']
def brief(v):
    if isinstance(v, dict):
        return {k: brief(x) for k, x in v.items()}
    if isinstance(v, list):
        return {'length':len(v), 'first':brief(v[0]) if v else None}
    return v

def time(ts):
    return datetime.fromtimestamp(ts/1000, ZoneInfo('Asia/Shanghai')).isoformat(timespec='milliseconds') if ts else None

def stats(values):
    a = sorted(x for x in values if isinstance(x, (int,float)) and math.isfinite(x))
    if not a:
        return None
    def q(p):
        k=(len(a)-1)*p
        lo=int(k)
        return a[lo]+(a[min(lo+1,len(a)-1)]-a[lo])*(k-lo)
    return {'n':len(a),'mean':round(statistics.mean(a),3),'min':a[0], 'p50':round(q(.5),3),'p95':round(q(.95),3),'p99':round(q(.99),3),'max':a[-1]}

if __name__ == '__main__':
    data = load()
    print(json.dumps(brief(data), ensure_ascii=False, indent=2))