#!/usr/bin/env python3
"""把 data/results.db 渲染成单文件离线报告 report.html。

    python report.py              # 生成 report.html
    python report.py --csv        # 另外导出 data/runs.csv
"""
import csv
import os
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MI_DB") or ROOT / "data" / "results.db")   # MI_DB：测试时指到别处，不碰正式数据
INCOMPLETE = 0.1                                     # 与 run.py 一致：出错的题超过这个比例的执行不计分
CATS = {"trap": "陷阱题", "logic": "逻辑", "code": "代码", "knowledge": "学科", "sense": "常识陷阱", "compute": "精确计算"}


def assign_batches(runs):
    """一次 run.py 执行 = 一个批次。旧记录没有 batch 列，按时间推断：
    开始时间落在当前批次结束后 2 分钟内、且同题库同模型在本批还没出现过，就算同一批。"""
    known = {r["batch"] for r in runs if r.get("batch")}
    stamp = lambda r: time.mktime(time.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S"))
    key, end, seen = None, 0, set()
    for r in runs:                                   # 已按 started_at 排序
        if r.get("batch"):
            continue
        t, who = stamp(r), (r["suite"], r["name"])
        if key is None or t > end + 120 or who in seen:
            key, end, seen = r["started_at"], t, set()
            while key in known:                      # 不和真实批次撞名
                key += "'"
        seen.add(who)
        end = max(end, t + (r["duration_s"] or 0))
        r["batch"] = key


def collect():
    bank = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    questions = bank["questions"]
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at, id")]
    latest = {}                                      # 每个模型最近一次计分的 run，只有它带完整回答
    for r in runs:
        r["cat_scores"] = json.loads(r["cat_scores"])
        if r["n_error"] > INCOMPLETE * r["n_total"]:  # 不完整的执行不计分（旧记录里存过分数的也不用）
            r["score"], r["cat_scores"] = None, {}
        else:
            latest[r["name"]] = r["id"]
    assign_batches(runs)
    details = {}
    for r in runs:
        rows = {a["qid"]: a for a in db.execute("SELECT * FROM answers WHERE run_id=?", (r["id"],))}
        # 每题一个字符：1 对 / 0 错 / e 出错 / - 当时题库没有这题
        r["bits"] = "".join(
            "-" if q["id"] not in rows else "e" if rows[q["id"]]["error"] else str(rows[q["id"]]["correct"])
            for q in questions)
        if latest.get(r["name"]) == r["id"]:
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
