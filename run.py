#!/usr/bin/env python3
"""model-inspect：10 分钟内给模型做一次固定题库体检，结果累计进 SQLite。

    python run.py                 # 测 config.json 里的全部模型
    python run.py gpt-x deepseek  # 只测指定模型（按 name）
    python run.py --base-url URL --api-key KEY --model ID   # 不写配置，临时测一个
    python run.py --smoke         # 每个模型只问 3 题验证连通，不入库
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "results.db"
DEFAULTS = {"protocol": "", "concurrency": 8, "timeout": 180, "budget": 600, "max_tokens": 8192,
            "stream": False, "params": {}}
CATS = {"trap": "陷阱题", "logic": "逻辑", "code": "代码", "knowledge": "学科", "sense": "常识陷阱", "compute": "精确计算"}
SYSTEM = "你在参加一次标准化测验。可以先简要推理，但最后必须单独一行按要求的格式给出答案。"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, name TEXT, model TEXT, base_url TEXT, suite TEXT,
  score REAL, cat_scores TEXT, n_total INTEGER, n_correct INTEGER, n_error INTEGER,
  duration_s REAL, avg_latency_s REAL, tokens_in INTEGER, tokens_out INTEGER,
  returned_model TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS answers (
  run_id INTEGER, qid TEXT, cat TEXT, correct INTEGER, got TEXT, expected TEXT,
  latency_s REAL, tokens_in INTEGER, tokens_out INTEGER, error TEXT, reply TEXT);
CREATE INDEX IF NOT EXISTS answers_run ON answers(run_id);
"""


# ---------- 配置 ----------
def load_models(args):
    """返回待测模型列表，每项是合并了 defaults 的 dict。"""
    if args.model:
        one = {"name": args.name or args.model, "model": args.model,
               "base_url": args.base_url or os.environ.get("MI_BASE_URL", ""),
               "api_key": args.api_key or os.environ.get("MI_API_KEY", ""),
               "protocol": args.protocol or ""}
        models, defaults = [one], {}
    else:
        path = Path(args.config)
        if not path.exists():
            sys.exit(f"找不到 {path}。先复制 config.example.json 为 config.json 并填写，或用 --base-url/--api-key/--model 临时测试。")
        cfg = json.loads(path.read_text(encoding="utf-8"))
        models, defaults = cfg.get("models", []), cfg.get("defaults", {})
    out = []
    for m in models:
        m = {**DEFAULTS, **defaults, **m}
        m.setdefault("name", m.get("model"))
        key = str(m.get("api_key", ""))
        if key.startswith("$"):                      # "$ENV_NAME" 表示从环境变量读取
            m["api_key"] = os.environ.get(key[1:], "")
        out.append(m)
    if args.names:
        known = {m["name"] for m in out}
        missing = [n for n in args.names if n not in known]
        if missing:
            sys.exit(f"配置里没有这些模型：{', '.join(missing)}。可选：{', '.join(sorted(known))}")
        out = [m for m in out if m["name"] in args.names]
    for m in out:
        if not m.get("base_url") or not m.get("api_key") or not m.get("model"):
            sys.exit(f"模型 {m.get('name')} 缺少 base_url / api_key / model。")
    return out


# ---------- 接口 ----------
def is_anthropic(m):
    url = m["base_url"].rstrip("/")
    return m["protocol"] == "anthropic" or (not m["protocol"] and ("anthropic.com" in url or url.endswith("/messages")))


def read_stream(resp):
    """把 SSE 流拼回与非流式相同的结构（OpenAI 与 Anthropic 两种事件都认）。"""
    text, usage, model = [], {}, ""
    for line in resp:
        line = line.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line.endswith("[DONE]"):
            continue
        ev = json.loads(line[5:])
        if ev.get("error"):
            raise RuntimeError("流式返回错误：" + json.dumps(ev["error"], ensure_ascii=False)[:300])
        msg = ev.get("message") or {}                # Anthropic message_start
        model = model or ev.get("model") or msg.get("model") or ""
        usage.update({k: v for k, v in (ev.get("usage") or msg.get("usage") or {}).items() if v})
        delta = ev["choices"][0].get("delta", {}) if ev.get("choices") else ev.get("delta") or {}
        text.append(delta.get("content") or delta.get("text") or "")
    return {"model": model, "usage": usage, "content": "".join(text)}


def call_model(m, prompt):
    """发一次请求，返回 (回答文本, 输入 token, 输出 token, 接口回显的模型名)。"""
    url = m["base_url"].rstrip("/")
    if is_anthropic(m):
        if not url.endswith("/messages"):
            url += "/messages"
        headers = {"x-api-key": m["api_key"], "anthropic-version": "2023-06-01"}
        body = {"model": m["model"], "max_tokens": m["max_tokens"], "system": SYSTEM,
                "messages": [{"role": "user", "content": prompt}]}
    else:
        if not url.endswith("/chat/completions"):
            url += "/chat/completions"
        headers = {"Authorization": "Bearer " + m["api_key"]}
        body = {"model": m["model"], "messages": [{"role": "system", "content": SYSTEM},
                                                  {"role": "user", "content": prompt}]}
    if m["stream"]:                                  # 流式能避开中转站对长请求的 524 超时
        body["stream"] = True
        if not is_anthropic(m):
            body["stream_options"] = {"include_usage": True}
    body.update(m["params"])                         # 需要 temperature / max_tokens 等就写在 params 里
    headers.update({"Content-Type": "application/json", "User-Agent": "curl/8.5.0"})
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=m["timeout"]) as resp:
                data = read_stream(resp) if m["stream"] else json.loads(resp.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code not in (408, 429, 500, 502, 503, 504, 529) or attempt == 2:
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            if attempt == 2:
                raise RuntimeError(f"{type(e).__name__}: {e}") from None
        time.sleep(2 * (attempt + 1))
    usage = data.get("usage") or {}
    if "choices" in data:
        content = (data["choices"][0].get("message") or {}).get("content") or ""
    else:
        content = data.get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content.strip():
        raise RuntimeError("接口没有返回回答：" + json.dumps(data, ensure_ascii=False)[:300])
    return (content, usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0,
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0, data.get("model", ""))


# ---------- 出题与判分 ----------
def letters_of(q):
    return q.get("letters") or "ABCDEFGHIJ"[:len(q.get("options", []))]


def build_prompt(q):
    parts = [q["context"]] if q.get("context") else []
    parts.append("题目：\n" + q["q"])
    if q.get("options"):
        parts.append("选项：\n" + "\n".join(f"{c}. {o}" for c, o in zip(letters_of(q), q["options"])))
    if letters_of(q):
        parts.append("最后单独一行输出「答案：X」，X 是一个选项字母。")
    else:
        parts.append("最后单独一行输出「答案：结果」，只写结果本身，不带单位、不加解释。")
    return "\n\n".join(parts)


def norm(text):
    text = re.sub(r"\\(?:boxed|text|mathrm)\{([^{}]*)\}", r"\1", str(text))
    return re.sub(r"[\s`*,，。\"'“”「」$]+", "", text).strip(".")


def grade(q, reply):
    """返回 (是否正确, 从回答里取到的答案)。"""
    text = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
    tagged = re.findall(r"(?:答案|最终答案|answer)\s*(?:是|为|is)?\s*[:：]?\s*([^\n]+)", text, flags=re.I)
    letters = letters_of(q)
    if letters:
        pat = rf"(?<![A-Za-z])([{letters}])(?![A-Za-z])"
        hit = re.search(pat, tagged[-1]) if tagged else None
        if not hit:                                  # 没按格式写：退回到全文最后一个孤立的选项字母
            found = re.findall(pat, text)
            got = found[-1] if found else ""
        else:
            got = hit.group(1)
        return got == q["answer"], got
    got = tagged[-1].strip() if tagged else (text.strip().splitlines() or [""])[-1].strip()
    want, have = norm(q["answer"]), norm(got)
    if re.fullmatch(r"-?\d+(\.\d+)?", want):       # 数值题：取回答里最后一个数比较，容忍“10 次”这类带单位写法
        nums = re.findall(r"-?\d+(?:\.\d+)?", have)
        return bool(nums) and nums[-1] == want, got[:80]
    return want in have, got[:80]


# ---------- 执行 ----------
def run_model(m, questions, smoke=False):
    started = time.time()
    deadline = started + m["budget"]
    seen = {"ok": 0, "bad": 0, "why": ""}

    def ask(q):
        row = {"qid": q["id"], "cat": q["cat"], "expected": q["answer"], "correct": 0, "got": "",
               "latency_s": 0.0, "tokens_in": 0, "tokens_out": 0, "error": "", "reply": "", "returned_model": ""}
        if time.time() > deadline:
            row["error"] = f"超出 {m['budget']} 秒预算，未作答"
            return row
        if not seen["ok"] and seen["bad"] >= 8:     # 开头连续失败（密钥错、地址不通）就不再空耗
            row["error"] = "前 8 次请求全部失败，已中止：" + seen["why"]
            return row
        t0 = time.time()
        try:
            reply, row["tokens_in"], row["tokens_out"], row["returned_model"] = call_model(m, build_prompt(q))
            ok, row["got"] = grade(q, reply)
            row["correct"], row["reply"] = int(ok), reply[-4000:]
        except Exception as e:                       # 单题失败不影响其余题目
            row["error"] = str(e)[:500]
        seen["bad" if row["error"] else "ok"] += 1
        seen["why"] = row["error"] or seen["why"]
        row["latency_s"] = round(time.time() - t0, 2)
        return row

    mixed = [q for k in range(7) for q in questions[k::7]]   # 各类别穿插着问，超预算时不会只砍掉排在后面的类别
    with ThreadPoolExecutor(m["concurrency"]) as pool:
        done = {r["qid"]: r for r in pool.map(ask, mixed)}
    rows = [done[q["id"]] for q in questions]
    answered = [r for r in rows if not r["error"]]
    cat_scores = {}
    for cat in CATS:
        sub = [r for r in rows if r["cat"] == cat]
        if sub:
            cat_scores[cat] = round(100 * sum(r["correct"] for r in sub) / len(sub), 1)
    return {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "name": m["name"], "model": m["model"], "base_url": m["base_url"],
        # 总分 = 各类别正确率的平均；出错和超时的题按答错计
        "score": round(sum(cat_scores.values()) / len(cat_scores), 1),
        "cat_scores": cat_scores, "n_total": len(rows), "n_correct": sum(r["correct"] for r in rows),
        "n_error": len(rows) - len(answered), "duration_s": round(time.time() - started, 1),
        "avg_latency_s": round(sum(r["latency_s"] for r in answered) / len(answered), 2) if answered else 0,
        "tokens_in": sum(r["tokens_in"] for r in rows), "tokens_out": sum(r["tokens_out"] for r in rows),
        "returned_model": next((r["returned_model"] for r in rows if r["returned_model"]), ""),
        "rows": rows,
    }


def save(db, run, suite, note):
    cur = db.execute(
        "INSERT INTO runs (started_at,name,model,base_url,suite,score,cat_scores,n_total,n_correct,n_error,"
        "duration_s,avg_latency_s,tokens_in,tokens_out,returned_model,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run["started_at"], run["name"], run["model"], run["base_url"], suite, run["score"],
         json.dumps(run["cat_scores"]), run["n_total"], run["n_correct"], run["n_error"], run["duration_s"],
         run["avg_latency_s"], run["tokens_in"], run["tokens_out"], run["returned_model"], note))
    db.executemany(
        "INSERT INTO answers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(cur.lastrowid, r["qid"], r["cat"], r["correct"], r["got"], r["expected"], r["latency_s"],
          r["tokens_in"], r["tokens_out"], r["error"], r["reply"]) for r in run["rows"]])
    db.commit()
    return cur.lastrowid


def open_db():
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH, check_same_thread=False)   # 写入由 lock 串行化
    db.executescript(SCHEMA)
    return db


def main():
    ap = argparse.ArgumentParser(description="模型快速体检：固定题库，结果累计进 data/results.db")
    ap.add_argument("names", nargs="*", help="要测的模型 name；不写则测配置里的全部")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--base-url", help="临时测试：接口地址（也可用环境变量 MI_BASE_URL）")
    ap.add_argument("--api-key", help="临时测试：密钥（也可用环境变量 MI_API_KEY）")
    ap.add_argument("--model", help="临时测试：模型 ID")
    ap.add_argument("--name", help="临时测试：结果里显示的名字，默认同 --model")
    ap.add_argument("--protocol", choices=["openai", "anthropic"], help="临时测试：协议，默认按地址判断")
    ap.add_argument("--list", action="store_true", help="列出配置里的模型后退出")
    ap.add_argument("--smoke", action="store_true", help="每个模型只问 3 题验证连通，不入库")
    ap.add_argument("--note", default="", help="给这次执行加备注（如“换了中转站”）")
    ap.add_argument("--no-report", action="store_true", help="跑完不重新生成 report.html")
    args = ap.parse_args()

    models = load_models(args)
    if args.list:
        for m in models:
            print(f"{m['name']:28} {m['model']:32} {m['base_url']}")
        return
    bank = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    questions = bank["questions"][::30] if args.smoke else bank["questions"]
    print(f"题库 {bank['suite']}，{len(questions)} 题；待测模型 {len(models)} 个：{', '.join(m['name'] for m in models)}")

    db, lock, failed = None if args.smoke else open_db(), threading.Lock(), []

    def one(m):
        run = run_model(m, questions, args.smoke)
        with lock:
            cats = "  ".join(f"{CATS[c]} {v:.0f}" for c, v in run["cat_scores"].items())
            print(f"\n[{run['name']}] 总分 {run['score']:.1f}  对 {run['n_correct']}/{run['n_total']}  "
                  f"出错 {run['n_error']}  用时 {run['duration_s']:.0f}s  平均 {run['avg_latency_s']:.1f}s/题  "
                  f"输出 {run['tokens_out']} tokens\n    {cats}")
            errors = [r for r in run["rows"] if r["error"]]
            if errors:
                print(f"    首个错误：{errors[0]['error'][:200]}")
            if run["n_error"] == run["n_total"]:
                failed.append(run["name"])
                print("    全部请求失败，本次不入库。")
            elif db:
                print(f"    已入库，run #{save(db, run, bank['suite'], args.note)}")
            elif args.smoke:
                for r in run["rows"]:
                    print(f"    {r['qid']}: 期望 {r['expected']} 得到 {r['got'] or r['error'][:80]}")

    with ThreadPoolExecutor(len(models)) as pool:    # 各模型并行，整体仍在 10 分钟内结束
        list(pool.map(one, models))
    if db:
        db.close()
        if not args.no_report:
            import report
            print(f"\n报告已更新：{report.build()}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
