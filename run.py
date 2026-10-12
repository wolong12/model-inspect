#!/usr/bin/env python3
"""model-inspect：10 分钟内给模型做一次固定题库体检，结果累计进 SQLite。

    python run.py                 # 测配置里的全部模型（配置默认从 ct secret 的 secret://model-inspect 读取）
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
DB_PATH = Path(os.environ.get("MI_DB") or ROOT / "data" / "results.db")   # MI_DB：测试时指到别处，不碰正式数据
SECRET_CONFIG = "secret://model-inspect"
DEFAULTS = {"protocol": "", "concurrency": 8, "timeout": 180, "budget": 600, "max_tokens": 8192,
            "rpm": 0, "stream": True, "params": {}}
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
def secret_resolver():
    """camtools 的 ct secret SDK；当前解释器用不了它（缺依赖或 Python 版本太低）时，换到 camtools 的 uv 环境重新执行。"""
    home = Path(os.environ.get("CAMTOOLS_HOME", "~/qb/camtools")).expanduser()
    sys.path.append(str(home))
    try:
        from local_secrets import LocalSecretProvider, SecretResolver
    except Exception as e:                           # 旧 Python 导入 SDK 抛的是 TypeError/SyntaxError，不只是 ImportError
        if not (home / "local_secrets").is_dir():
            sys.exit(f"找不到 ct secret SDK：{home}/local_secrets 不存在。用环境变量 CAMTOOLS_HOME 指定 camtools 的位置。")
        if os.environ.get("MI_REEXEC"):
            sys.exit(f"在 camtools 的 uv 环境里仍无法加载 ct secret SDK（{type(e).__name__}: {e}）。")
        os.environ["MI_REEXEC"] = "1"
        try:
            os.execvp("uv", ["uv", "run", "--project", str(home), "python", str(Path(__file__).resolve()), *sys.argv[1:]])
        except OSError:
            sys.exit(f"当前 Python {sys.version.split()[0]} 无法加载 ct secret SDK（{type(e).__name__}: {e}），"
                     f"并且找不到 uv 来切换到 camtools 的环境。请安装 uv，或改用：uv run --project {home} python run.py")
    return SecretResolver(LocalSecretProvider(profile=os.environ.get("MI_SECRET_PROFILE", "default")))


def load_config(source):
    """source 是 secret:// 引用（整份配置存在 ct secret 里）或本地文件路径；内容是 JSON 或 YAML。"""
    if source.startswith("secret://"):
        try:
            text = secret_resolver().resolve(source)
        except Exception as e:                       # SDK 的报错只含引用名和原因，不含机密内容
            hint = {"SecretNotInitializedError": "本机还没初始化，先执行 ct secret init，再 import。",
                    "SecretNotFoundError": f"还没有这条配置，执行：ct secret import {source} --stdin（粘贴后按 Ctrl-D）。",
                    }.get(type(e).__name__, f"用 ct secret doctor {source} 检查 keyring 和密文是否完好。")
            sys.exit(f"读取 {source} 失败：{type(e).__name__}: {e}\n{hint}")
    else:
        path = Path(source)
        if not path.exists():
            sys.exit(f"找不到 {path}。")
        text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml                              # 走到这里说明已在 camtools 环境里，或用户自己装了 pyyaml
            cfg = yaml.safe_load(text)
        except Exception as e:                       # 不打印 yaml 的报错原文：它会带出配置片段，可能含密钥
            line = getattr(getattr(e, "problem_mark", None), "line", None)
            sys.exit(f"{source} 的内容不是合法的 JSON 或 YAML（{type(e).__name__}"
                     + (f"，问题在第 {line + 1} 行附近" if line is not None else "") + "）。修正后重新 import。")
        if not isinstance(cfg, dict) or not cfg.get("models"):
            sys.exit(f"{source} 里没有 models 列表，格式见 config.example.json。")
        return cfg


def expand_efforts(models):
    """带 "efforts": [...] 的条目展开成每档一个变体，名字是 name@effort，各自成一条长期曲线。"""
    for m in models:
        efforts = m.get("efforts")
        if not efforts:
            yield m
            continue
        base = m.get("name") or m.get("model")
        for e in efforts:
            anthropic = m.get("protocol") == "anthropic" or "anthropic.com" in m.get("base_url", "")
            extra = {"output_config": {"effort": e}} if anthropic else {"reasoning_effort": e}
            yield {**{k: v for k, v in m.items() if k != "efforts"}, "name": f"{base}@{e}", "base": base,
                   "params": {**extra, **m.get("params", {})}}


def load_models(args):
    """返回待测模型列表，每项是合并了 defaults 的 dict。"""
    if args.model:
        one = {"name": args.name or args.model, "model": args.model,
               "base_url": args.base_url or os.environ.get("MI_BASE_URL", ""),
               "api_key": args.api_key or os.environ.get("MI_API_KEY", ""),
               "protocol": args.protocol or ""}
        models, defaults = [one], {}
    else:
        local = ROOT / "config.json"
        cfg = load_config(args.config or os.environ.get("MI_CONFIG") or (str(local) if local.exists() else SECRET_CONFIG))
        models, defaults = cfg.get("models", []), cfg.get("defaults", {})
    out = []
    for m in expand_efforts(models):
        m = {**DEFAULTS, **defaults, **m}
        m.setdefault("name", m.get("model"))
        key = str(m.get("api_key", ""))
        if key.startswith("$"):                      # "$ENV_NAME" 表示从环境变量读取
            m["api_key"] = os.environ.get(key[1:], "")
        out.append(m)
    if args.names:
        known = {m["name"] for m in out} | {m["base"] for m in out if m.get("base")}
        missing = [n for n in args.names if n not in known]
        if missing:
            sys.exit(f"配置里没有这些模型：{', '.join(missing)}。可选：{', '.join(sorted(known))}")
        out = [m for m in out if m["name"] in args.names or m.get("base") in args.names]   # 写基础名即选中它的全部 effort
    if '"secret://' in json.dumps(out):              # 配置里单个字段也可以写成 secret://名字 或 secret://名字#字段
        try:
            out = secret_resolver().resolve_tree(out)
        except Exception as e:
            sys.exit(f"解析配置里的 secret:// 引用失败（{type(e).__name__}：{e}）。用 ct secret status <引用> 检查是否已配置。")
    for m in out:
        if not m.get("base_url") or not m.get("api_key") or not m.get("model"):
            sys.exit(f"模型 {m.get('name')} 缺少 base_url / api_key / model。")
        if not str(m["api_key"]).isascii():          # 多半是示例里的中文占位符没换成真密钥
            sys.exit(f"模型 {m['name']} 的 api_key 含有非英文字符，像是没替换的占位符。"
                     f"检查该模型自己的 api_key，没写的话用的是 defaults 里的。")
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


class Limiter:
    """一个账号（接口地址 + 密钥）一个：限制同时在途的请求数和每分钟请求数，遇到 429 自动降速。"""

    def __init__(self, concurrency, rpm):
        self.limit, self.busy, self.gap = concurrency, 0, 60 / rpm if rpm else 0
        self.next_at = self.cut_at = 0.0
        self.hits = 0                                # 被限流的次数
        self.cv = threading.Condition()

    def __enter__(self):
        with self.cv:
            while self.busy >= self.limit:
                self.cv.wait()
            self.busy += 1
            start = max(time.time(), self.next_at)
            self.next_at = start + self.gap
        time.sleep(max(0, start - time.time()))

    def __exit__(self, *exc):
        with self.cv:
            self.busy -= 1
            self.cv.notify_all()

    def slow_down(self, wait):
        """收到 429：所有请求暂停 wait 秒；并发减半（10 秒内只减一次，避免一批 429 把并发直接砍到 1）。"""
        with self.cv:
            now = time.time()
            self.hits += 1
            self.next_at = max(self.next_at, now + wait)
            if now - self.cut_at > 10:
                self.limit, self.cut_at = max(1, self.limit // 2), now


def call_model(m, prompt, limiter, deadline):
    """发一次请求，返回 (回答文本, 输入 token, 输出 token, 接口回显的模型名, 本次请求耗时)。"""
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
    fails = limited = 0
    while True:
        try:
            with limiter:
                t0 = time.time()
                with urllib.request.urlopen(req, timeout=m["timeout"]) as resp:
                    data = read_stream(resp) if m["stream"] else json.loads(resp.read().decode("utf-8", "replace"))
                took = time.time() - t0
            break
        except urllib.error.HTTPError as e:
            detail = f"HTTP {e.code}: " + e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:                        # 限流不算失败次数：降速后重试，直到预算用完
                limited += 1
                after = e.headers.get("Retry-After", "")
                wait = min(60, int(after) if after.isdigit() else 5 * 2 ** min(limited, 3))
                if limited > 8 or time.time() + wait > deadline:
                    raise RuntimeError(detail) from None
                limiter.slow_down(wait)
                continue
            fails += 1
            if e.code not in (408, 500, 502, 503, 504, 529) or fails == 3:
                raise RuntimeError(detail) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            fails += 1
            if fails == 3:
                raise RuntimeError(f"{type(e).__name__}: {e}") from None
        time.sleep(2 * fails)
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
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0, data.get("model", ""), took)


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
def run_model(m, questions, limiter):
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
        try:
            reply, row["tokens_in"], row["tokens_out"], row["returned_model"], took = call_model(
                m, build_prompt(q), limiter, deadline)
            row["latency_s"] = round(took, 2)        # 只算成功那一次请求，不含排队和重试等待
            ok, row["got"] = grade(q, reply)
            row["correct"], row["reply"] = int(ok), reply[-4000:]
        except Exception as e:                       # 单题失败不影响其余题目
            row["error"] = str(e)[:500]
        seen["bad" if row["error"] else "ok"] += 1
        seen["why"] = row["error"] or seen["why"]
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
        # 总分 = 全部题目的正确率；出错和超时的题按答错计
        "score": round(100 * sum(r["correct"] for r in rows) / len(rows), 1),
        "cat_scores": cat_scores, "n_total": len(rows), "n_correct": sum(r["correct"] for r in rows),
        "n_error": len(rows) - len(answered), "duration_s": round(time.time() - started, 1),
        "avg_latency_s": round(sum(r["latency_s"] for r in answered) / len(answered), 2) if answered else 0,
        "tokens_in": sum(r["tokens_in"] for r in rows), "tokens_out": sum(r["tokens_out"] for r in rows),
        "returned_model": next((r["returned_model"] for r in rows if r["returned_model"]), ""),
        "limit_hits": limiter.hits, "limit_now": limiter.limit,
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
    ap.add_argument("--config", help="配置来源：secret:// 引用或文件路径。默认用 config.json（存在时），否则 " + SECRET_CONFIG)
    ap.add_argument("--base-url", help="临时测试：接口地址（也可用环境变量 MI_BASE_URL）")
    ap.add_argument("--api-key", help="临时测试：密钥（也可用环境变量 MI_API_KEY）")
    ap.add_argument("--model", help="临时测试：模型 ID")
    ap.add_argument("--name", help="临时测试：结果里显示的名字，默认同 --model")
    ap.add_argument("--protocol", choices=["openai", "anthropic"], help="临时测试：协议，默认按地址判断")
    ap.add_argument("--list", action="store_true", help="列出配置里的模型后退出")
    ap.add_argument("--smoke", action="store_true", help="每个模型只问 3 题验证连通，不入库")
    ap.add_argument("--budget", type=int, help="覆盖配置里的单模型总预算秒数（做一次性的完整测量时放宽）")
    ap.add_argument("--timeout", type=int, help="覆盖配置里的单题超时秒数")
    ap.add_argument("--note", default="", help="给这次执行加备注（如“换了中转站”）")
    ap.add_argument("--no-report", action="store_true", help="跑完不重新生成 report.html")
    args = ap.parse_args()

    models = load_models(args)
    for m in models:
        m["budget"], m["timeout"] = args.budget or m["budget"], args.timeout or m["timeout"]
    if args.list:
        for m in models:
            print(f"{m['name']:28} {m['model']:32} {m['base_url']}")
        return
    bank = json.loads((ROOT / "questions.json").read_text(encoding="utf-8"))
    questions = bank["questions"][::15] if args.smoke else bank["questions"]
    print(f"题库 {bank['suite']}，{len(questions)} 题；待测模型 {len(models)} 个：{', '.join(m['name'] for m in models)}")

    db, lock, failed = None if args.smoke else open_db(), threading.Lock(), []

    def one(m, limiter):
        hits = limiter.hits
        with lock:
            print(f"[{time.strftime('%H:%M:%S')}] 开始 {m['name']}", flush=True)
        run = run_model(m, questions, limiter)
        with lock:
            cats = "  ".join(f"{CATS[c]} {v:.0f}" for c, v in run["cat_scores"].items())
            print(f"\n[{run['name']}] 总分 {run['score']:.1f}  对 {run['n_correct']}/{run['n_total']}  "
                  f"出错 {run['n_error']}  用时 {run['duration_s']:.0f}s  平均 {run['avg_latency_s']:.1f}s/题  "
                  f"输出 {run['tokens_out']} tokens\n    {cats}")
            if run["limit_hits"] > hits:
                print(f"    被限流 {run['limit_hits'] - hits} 次，已自动降速，当前并发 {run['limit_now']}"
                      f"（可在配置里调低 concurrency 或设置 rpm）")
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

    accounts = {}                                    # 同一账号（地址 + 密钥）的模型共用限速器并排队跑，不同账号之间并行
    for m in models:
        accounts.setdefault((m["base_url"], m["api_key"]), []).append(m)
    if len(accounts) < len(models):
        print(f"同一账号下的模型依次执行（共 {len(accounts)} 个账号并行），每个模型各自有 {models[0]['budget']} 秒预算。")

    def account(group):
        limiter = Limiter(group[0]["concurrency"], group[0]["rpm"])
        for m in group:
            limiter.limit = min(limiter.limit, m["concurrency"]) if limiter.hits else m["concurrency"]
            one(m, limiter)

    with ThreadPoolExecutor(len(accounts)) as pool:
        list(pool.map(account, accounts.values()))
    if db:
        db.close()
        if not args.no_report:
            import report
            print(f"\n报告已更新：{report.build()}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
