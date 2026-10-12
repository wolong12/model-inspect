#!/usr/bin/env python3
"""把 data/results.db 渲染成单文件离线报告 report.html。

    python report.py              # 生成 report.html
    python report.py --csv        # 另外导出 data/runs.csv
"""
import csv
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "results.db"
CATS = {"trap": "陷阱题", "logic": "逻辑", "code": "代码", "knowledge": "学科", "sense": "常识陷阱", "compute": "精确计算"}


def collect():
    bank = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    questions = bank["questions"]
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at, id")]
    latest = {}                                      # 每个模型最近一次 run，只有它带完整回答
    for r in runs:
        r["cat_scores"] = json.loads(r["cat_scores"])
        latest[r["name"]] = r["id"]
    details = {}
    for r in runs:
        rows = {a["qid"]: a for a in db.execute("SELECT * FROM answers WHERE run_id=?", (r["id"],))}
        # 每题一个字符：1 对 / 0 错 / e 出错 / - 当时题库没有这题
        r["bits"] = "".join(
            "-" if q["id"] not in rows else "e" if rows[q["id"]]["error"] else str(rows[q["id"]]["correct"])
            for q in questions)
        if latest[r["name"]] == r["id"]:
            details[r["id"]] = {qid: {"got": a["got"], "error": a["error"], "reply": a["reply"][-1500:],
                                      "latency_s": a["latency_s"]} for qid, a in rows.items()}
    db.close()
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "suite": bank["suite"], "cats": CATS,
        "questions": [{"id": q["id"], "cat": q["cat"], "src": q["src"], "answer": q["answer"],
                       "q": q["q"][:900], "options": q.get("options", [])} for q in questions],
        "runs": runs, "details": details,
    }


def build(out=ROOT / "report.html"):
    data = json.dumps(collect(), ensure_ascii=False).replace("</", "<\\/")
    html = (ROOT / "template.html").read_text(encoding="utf-8").replace("/*DATA*/null", data)
    Path(out).write_text(html, encoding="utf-8")
    return out


def export_csv(out=ROOT / "data" / "runs.csv"):
    db = sqlite3.connect(DB_PATH)
    cur = db.execute("SELECT * FROM runs ORDER BY started_at, id")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c[0] for c in cur.description])
        w.writerows(cur)
    db.close()
    return out


if __name__ == "__main__":
    if not DB_PATH.exists():
        sys.exit("还没有数据，先执行 python run.py")
    print("报告：", build())
    if "--csv" in sys.argv:
        print("CSV：", export_csv())
