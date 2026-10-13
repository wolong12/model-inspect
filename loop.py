#!/usr/bin/env python3
"""持续体检：每隔一段时间执行一次 run.py，默认 2 小时一轮，一直跑到被停掉。

    python loop.py                        # 立刻跑第一轮，之后每 2 小时一轮
    python loop.py --every 30m            # 换个间隔：90s / 30m / 2h / 1d，纯数字按秒
    python loop.py gpt-5.5 --budget 900   # 其余参数原样传给 run.py
    nohup python3 loop.py >> data/loop.log 2>&1 &   # 放到后台，kill 它就停
"""
import argparse
import fcntl
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("MI_DB") or ROOT / "data" / "results.db")
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(text):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip().lower())
    if not m or not float(m.group(1)):
        raise argparse.ArgumentTypeError(f"看不懂的间隔 {text!r}，写成 90s / 30m / 2h / 1d 这样")
    return float(m.group(1)) * UNITS[m.group(2) or "s"]


def span(s):
    return f"{round(s / 3600, 1):g} 小时" if s >= 3600 else f"{s / 60:.0f} 分钟" if s >= 60 else f"{s:.0f} 秒"


def say(text):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="每隔一段时间执行一次 run.py，一直跑到被停掉。不认识的参数原样传给 run.py。")
    ap.add_argument("--every", type=seconds, default=7200, metavar="间隔",
                    help="两轮开始时间的间隔：90s / 30m / 2h / 1d，纯数字按秒。默认 2h")
    args, rest = ap.parse_known_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = open(DB_PATH.with_name("loop.lock"), "w")  # 同一个库只允许一个 loop.py，免得重复测、重复花钱
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"已经有一个 loop.py 在往 {DB_PATH} 写了，不重复启动。")

    for sig in (signal.SIGTERM, signal.SIGHUP):      # kill、systemctl stop、终端断开也走下面的 finally
        if signal.getsignal(sig) is not signal.SIG_IGN:   # nohup 启动时 SIGHUP 是忽略的，保持不动
            signal.signal(sig, lambda *_: sys.exit(0))
    cmd = [sys.executable, str(ROOT / "run.py"), *rest]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}    # 输出重定向到文件时也逐行落盘，方便 tail -f
    say(f"每 {span(args.every)}一轮：run.py {' '.join(rest)}".rstrip())
    n, child = 0, None
    try:
        while True:
            n, start = n + 1, time.time()
            say(f"第 {n} 轮开始")
            child = subprocess.Popen(cmd, env=env, start_new_session=True)   # 独立进程组，停的时候连 uv 换出来的子进程一起停
            code = child.wait()
            due = start + args.every
            late = time.time() >= due
            say(f"第 {n} 轮结束，用时 {span(time.time() - start)}"
                + (f"，run.py 退出码 {code}（有模型失败、不完整或没跑起来，见上面的输出）" if code else "")
                + ("；已超过间隔，马上开始下一轮" if late else f"；下一轮 {time.strftime('%m-%d %H:%M:%S', time.localtime(due))}"))
            while time.time() < due:                 # 按墙上时钟等，机器休眠过也不会多睡
                time.sleep(max(0, min(60, due - time.time())))
    except KeyboardInterrupt:
        pass
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            say("已停止，正在跑的这一轮被中断（已测完的模型已入库）")
        else:
            say("已停止")


if __name__ == "__main__":
    main()
