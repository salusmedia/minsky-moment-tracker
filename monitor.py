#!/usr/bin/env python3
"""
明斯基时刻预警监控 (Minsky Moment Monitor)

从 FRED（圣路易斯联储）和 Yahoo Finance 拉取信用利差、私人信贷、AI 基础设施、
利率、资金市场与综合金融压力等指标，按 config.json 中的阈值评估风险，
在指标状态升级/回落时通过 macOS 通知、Bark、Server酱、Telegram、邮件或 Webhook 推送预警，
并生成 HTML 看板 reports/latest.html。

仅依赖 Python 3.9+ 标准库。

用法:
    python3 monitor.py                # 正常运行：抓数据、评估、按需推送、生成报告
    python3 monitor.py --dry-run      # 不推送、不更新状态，只打印并生成报告
    python3 monitor.py --force-notify # 无论是否有变化都推送一次当前摘要
    python3 monitor.py --test-notify  # 只发送一条测试通知，检查推送渠道配置
    python3 monitor.py --open         # 运行后用浏览器打开 HTML 看板
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "data" / "state.json"
HISTORY_PATH = BASE / "data" / "history.csv"
REPORT_DIR = BASE / "reports"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) minsky-monitor/1.0"

STATUS_NAME = {0: "正常", 1: "关注", 2: "警报", -1: "无数据"}
STATUS_EMOJI = {0: "🟢", 1: "🟡", 2: "🔴", -1: "⚪"}
STATUS_COLOR = {0: "#1a7f37", 1: "#bf8700", 2: "#cf222e", -1: "#8c959f"}


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def expand_env(obj):
    """把配置中的 ${VAR} 替换为环境变量值。"""
    if isinstance(obj, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), obj)
    if isinstance(obj, list):
        return [expand_env(x) for x in obj]
    if isinstance(obj, dict):
        return {k: expand_env(v) for k, v in obj.items()}
    return obj


def http_request(url: str, data: bytes | None = None, headers: dict | None = None,
                 timeout: int = 20, retries: int = 3, browser_ua: bool = False) -> str:
    """browser_ua=True 时使用浏览器 UA（Yahoo 需要）；FRED 会拦截伪装成浏览器的请求，需用默认 UA。"""
    last = None
    hdrs = {"User-Agent": UA} if browser_ua else {}
    if headers:
        hdrs.update(headers)
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{last}")


# ----------------------------------------------------------------------------
# 数据获取
# ----------------------------------------------------------------------------
Series = list  # list[tuple[str(date ISO), float]]


def fetch_fred(series_id: str, lookback_days: int = 800) -> Series:
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}&cosd={start}"
    text = http_request(url)
    out = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) < 2:
            continue
        try:
            out.append((row[0], float(row[1])))
        except ValueError:
            continue  # FRED 用 "." 表示缺失
    if not out:
        raise RuntimeError("FRED 返回空数据")
    return out


def fetch_yahoo(ticker: str, range_: str = "2y") -> Series:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?range={range_}&interval=1d")
    d = json.loads(http_request(url, browser_ua=True))
    res = (d.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"Yahoo 无数据: {(d.get('chart') or {}).get('error')}")
    res = res[0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), float(c)))
    if not out:
        raise RuntimeError("Yahoo 返回空数据")
    return out


class Fetcher:
    def __init__(self):
        self.cache: dict[str, Series] = {}

    def get(self, spec: dict) -> Series:
        src = spec["source"]
        if src == "spread":
            a, b = self.get(spec["a"]), self.get(spec["b"])
            scale = spec.get("scale", 1)
            bmap = dict(b)
            # 按日期对齐；b 若当日缺失，用最近一个已知值（如 IORB 只在调整时变化）
            out, last_b = [], None
            bdates = sorted(bmap)
            bi = 0
            for d, va in a:
                while bi < len(bdates) and bdates[bi] <= d:
                    last_b = bmap[bdates[bi]]
                    bi += 1
                if last_b is not None:
                    out.append((d, round((va - last_b) * scale, 4)))
            if not out:
                raise RuntimeError("利差序列无法对齐")
            return out
        key = f"{src}:{spec['id']}"
        if key not in self.cache:
            if src == "fred":
                self.cache[key] = fetch_fred(spec["id"])
            elif src == "yahoo":
                self.cache[key] = fetch_yahoo(spec["id"])
            else:
                raise ValueError(f"未知数据源 {src}")
        return self.cache[key]


# ----------------------------------------------------------------------------
# 规则评估
# ----------------------------------------------------------------------------
def fmt(v: float, unit: str = "") -> str:
    if v is None:
        return "-"
    if unit == "bp":
        return f"{v:.1f}bp"
    if unit == "%":
        return f"{v:.2f}%"
    if unit == "$":
        return f"${v:,.2f}"
    if abs(v) >= 100:
        return f"{v:,.1f}{unit}"
    return f"{v:.2f}{unit}"


def grade(x: float, warn: float, alert: float) -> int:
    if x >= alert:
        return 2
    if x >= warn:
        return 1
    return 0


def eval_rule(rule: dict, series: Series, unit: str) -> tuple[int, str, float | None]:
    vals = [v for _, v in series]
    cur = vals[-1]
    t = rule["type"]
    if t == "level":
        if rule.get("direction", "above") == "above":
            s = grade(cur, rule["warn"], rule["alert"])
            return s, f"水平 {fmt(cur, unit)}（关注≥{rule['warn']} / 警报≥{rule['alert']}）", cur
        s = grade(-cur, -rule["warn"], -rule["alert"])
        return s, f"水平 {fmt(cur, unit)}（关注≤{rule['warn']} / 警报≤{rule['alert']}）", cur
    if t == "change":
        w = rule["window"]
        if len(vals) <= w:
            return -1, f"{w} 期变化：数据不足", None
        base = vals[-1 - w]
        if rule.get("mode", "abs") == "pct":
            chg = (cur / base - 1) * 100 if base else 0.0
            label = f"{chg:+.1f}%"
            unit_s = "%"
        else:
            chg = cur - base
            label = f"{chg:+.2f}{'bp' if unit == 'bp' else ('pp' if unit == '%' else '')}"
            unit_s = ""
        x = chg if rule.get("direction", "up") == "up" else -chg
        s = grade(x, rule["warn"], rule["alert"])
        dir_s = "上升" if rule.get("direction", "up") == "up" else "下跌"
        return s, f"近 {w} 期变化 {label}（{dir_s}关注≥{rule['warn']}{unit_s} / 警报≥{rule['alert']}{unit_s}）", chg
    if t == "drawdown":
        w = rule.get("window", 252)
        peak = max(vals[-w:])
        dd = (1 - cur / peak) * 100 if peak else 0.0
        s = grade(dd, rule["warn"], rule["alert"])
        return s, f"较 {w} 日高点回撤 {dd:.1f}%（关注≥{rule['warn']}% / 警报≥{rule['alert']}%）", dd
    raise ValueError(f"未知规则类型 {t}")


def evaluate(cfg: dict) -> dict:
    fetcher = Fetcher()
    results = []
    errors = []
    for ind in cfg["indicators"]:
        r = {k: ind.get(k) for k in ("key", "name", "category", "unit", "weight", "note")}
        r["weight"] = r["weight"] or 1
        try:
            series = fetcher.get(ind["series"])
            r["series"] = series[-260:]
            r["value"] = series[-1][1]
            r["date"] = series[-1][0]
            rule_out = [eval_rule(rule, series, ind.get("unit", "")) for rule in ind["rules"]]
            r["rules"] = [{"status": s, "text": txt} for s, txt, _ in rule_out]
            r["status"] = max([s for s, _, _ in rule_out] + [0])
            # 数据陈旧检查
            age = (date.today() - date.fromisoformat(r["date"])).days
            r["stale"] = age > 14
        except Exception as e:  # noqa: BLE001
            r.update(status=-1, value=None, date=None, series=[], rules=[], stale=False, error=str(e))
            errors.append(f"{ind['name']}: {e}")
            log(f"  ✗ {ind['name']} 获取失败: {e}")
            continue
        log(f"  {STATUS_EMOJI[r['status']]} {ind['name']}: {fmt(r['value'], r['unit'] or '')} ({r['date']})")
        results.append(r)
    # 补回失败项，保持顺序
    keys_ok = {r["key"] for r in results}
    all_results = []
    for ind in cfg["indicators"]:
        if ind["key"] in keys_ok:
            all_results.append(next(r for r in results if r["key"] == ind["key"]))
        else:
            all_results.append({"key": ind["key"], "name": ind["name"], "category": ind["category"],
                                "unit": ind.get("unit", ""), "weight": ind.get("weight", 1),
                                "note": ind.get("note"), "status": -1, "value": None, "date": None,
                                "series": [], "rules": [], "stale": False,
                                "error": next((e for e in errors if e.startswith(ind["name"])), "")})

    valid = [r for r in all_results if r["status"] >= 0]
    tot_w = sum(r["weight"] * 2 for r in valid) or 1
    score = round(sum(r["weight"] * r["status"] for r in valid) / tot_w * 100, 1)
    level = cfg["composite_levels"][0]
    for lv in cfg["composite_levels"]:
        if score >= lv["min"]:
            level = lv
    level_idx = cfg["composite_levels"].index(level)

    cats: dict[str, list] = {}
    for r in valid:
        cats.setdefault(r["category"], []).append(r)
    cat_scores = {c: round(sum(x["weight"] * x["status"] for x in rs) /
                           (sum(x["weight"] * 2 for x in rs) or 1) * 100, 1)
                  for c, rs in cats.items()}
    return {"time": datetime.now().isoformat(timespec="seconds"), "score": score,
            "level": level, "level_idx": level_idx, "indicators": all_results,
            "category_scores": cat_scores, "errors": errors}


# ----------------------------------------------------------------------------
# 状态比对
# ----------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def save_state(res: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev = load_state()
    prev_ind = prev.get("indicators", {})
    ind = {}
    for r in res["indicators"]:
        if r["status"] < 0:  # 获取失败时保留上次状态
            if r["key"] in prev_ind:
                ind[r["key"]] = prev_ind[r["key"]]
            continue
        old = prev_ind.get(r["key"], {})
        since = old.get("since") if old.get("status") == r["status"] else res["time"]
        ind[r["key"]] = {"status": r["status"], "since": since, "value": r["value"], "date": r["date"]}
    state = {"last_run": res["time"], "score": res["score"], "level_idx": res["level_idx"],
             "indicators": ind, "last_summary_date": prev.get("last_summary_date")}
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def diff_state(res: dict, state: dict) -> tuple[list, list, bool, bool]:
    prev = state.get("indicators", {})
    up, down = [], []
    for r in res["indicators"]:
        if r["status"] < 0:
            continue
        old = prev.get(r["key"], {}).get("status", 0)
        if r["status"] > old:
            up.append((r, old))
        elif r["status"] < old:
            down.append((r, old))
    prev_lv = state.get("level_idx", 0)
    return up, down, res["level_idx"] > prev_lv, res["level_idx"] < prev_lv


# ----------------------------------------------------------------------------
# 推送
# ----------------------------------------------------------------------------
def build_message(res: dict, up: list, down: list, lv_up: bool, lv_down: bool,
                  force: bool = False) -> tuple[str, str]:
    lv = res["level"]
    if lv_up:
        title = f"{lv['emoji']} 明斯基监控：综合风险升至「{lv['name']}」"
    elif up:
        title = f"⚠️ 明斯基监控：{len(up)} 项指标风险升级"
    elif lv_down or down:
        title = f"✅ 明斯基监控：{len(down)} 项指标风险回落"
    else:
        title = f"{lv['emoji']} 明斯基监控日报：{lv['name']}"
    lines = [f"综合风险分 {res['score']}/100 · {lv['emoji']}{lv['name']}"]
    for r, old in up:
        trig = "；".join(x["text"] for x in r["rules"] if x["status"] == r["status"])
        lines.append(f"↑ {STATUS_EMOJI[r['status']]}{r['name']}: {STATUS_NAME[old]}→{STATUS_NAME[r['status']]}｜{trig}")
    for r, old in down:
        lines.append(f"↓ {STATUS_EMOJI[r['status']]}{r['name']}: {STATUS_NAME[old]}→{STATUS_NAME[r['status']]}"
                     f"（{fmt(r['value'], r['unit'] or '')}）")
    if force or not (up or down):
        hot = [r for r in res["indicators"] if r["status"] >= 1]
        if hot:
            lines.append("当前非正常指标：")
            for r in sorted(hot, key=lambda x: -x["status"]):
                lines.append(f"  {STATUS_EMOJI[r['status']]}{r['name']} {fmt(r['value'], r['unit'] or '')}")
        else:
            lines.append("所有指标处于正常区间。")
    if res["errors"]:
        lines.append(f"（{len(res['errors'])} 项数据获取失败）")
    lines.append(f"看板: {REPORT_DIR / 'latest.html'}")
    return title, "\n".join(lines)


def notify_macos(ch: dict, title: str, body: str) -> None:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    short = body if len(body) < 230 else body[:227] + "…"
    script = f'display notification "{esc(short)}" with title "{esc(title)}"'
    if ch.get("sound"):
        script += f' sound name "{esc(ch["sound"])}"'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)


def notify_bark(ch: dict, title: str, body: str) -> None:
    payload = {"device_key": ch["device_key"], "title": title, "body": body,
               "group": "minsky", "level": "timeSensitive"}
    http_request(ch.get("server", "https://api.day.app").rstrip("/") + "/push",
                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, retries=2)


def notify_serverchan(ch: dict, title: str, body: str) -> None:
    data = urllib.parse.urlencode({"title": title[:32], "desp": body.replace("\n", "\n\n")}).encode()
    http_request(f"https://sctapi.ftqq.com/{ch['sendkey']}.send", data=data, retries=2)


def notify_telegram(ch: dict, title: str, body: str) -> None:
    data = urllib.parse.urlencode({"chat_id": ch["chat_id"], "text": f"{title}\n\n{body}"}).encode()
    http_request(f"https://api.telegram.org/bot{ch['bot_token']}/sendMessage", data=data, retries=2)


def notify_email(ch: dict, title: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = ch["from"]
    msg["To"] = ", ".join(ch["to"])
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(ch["smtp_host"], int(ch.get("smtp_port", 465)), context=ctx, timeout=20) as s:
        s.login(ch["username"], ch["password"])
        s.sendmail(ch["from"], ch["to"], msg.as_string())


def notify_webhook(ch: dict, title: str, body: str) -> None:
    payload = {"title": title, "body": body, "text": f"{title}\n{body}"}  # text 字段兼容 Slack
    http_request(ch["url"], data=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"}, retries=2)


SENDERS = {"macos": notify_macos, "bark": notify_bark, "serverchan": notify_serverchan,
           "telegram": notify_telegram, "email": notify_email, "webhook": notify_webhook}


def send_all(cfg: dict, title: str, body: str) -> None:
    channels = expand_env(cfg["notify"]["channels"])
    sent = 0
    for name, ch in channels.items():
        if not ch.get("enabled"):
            continue
        try:
            SENDERS[name](ch, title, body)
            log(f"  ✓ 已通过 {name} 推送")
            sent += 1
        except Exception as e:  # noqa: BLE001
            log(f"  ✗ {name} 推送失败: {e}")
    if not sent:
        log("  ! 没有成功的推送渠道，请检查 config.json 中 notify.channels")


# ----------------------------------------------------------------------------
# HTML 看板
# ----------------------------------------------------------------------------
def sparkline(series: Series, color: str, w: int = 160, h: int = 36) -> str:
    vals = [v for _, v in series[-130:]]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{pts}"/></svg>')


def history_svg(rows: list, w: int = 720, h: int = 120) -> str:
    if len(rows) < 2:
        return '<p class="muted">运行多次后将在此显示综合风险分走势。</p>'
    vals = [float(r[1]) for r in rows[-180:]]
    bands = "".join(
        f'<rect x="0" y="{h - (top) / 100 * h:.1f}" width="{w}" height="{(top - bot) / 100 * h:.1f}" fill="{c}" opacity="0.10"/>'
        for bot, top, c in [(0, 20, "#1a7f37"), (20, 40, "#bf8700"), (40, 60, "#fb8500"), (60, 100, "#cf222e")])
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - v / 100 * h:.1f}" for i, v in enumerate(vals))
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="height:{h}px">{bands}'
            f'<polyline fill="none" stroke="#24292f" stroke-width="2" points="{pts}"/></svg>'
            f'<p class="muted">{html.escape(rows[-min(len(rows), 180)][0])} → {html.escape(rows[-1][0])}</p>')


def render_html(cfg: dict, res: dict) -> str:
    lv = res["level"]
    hist = []
    if HISTORY_PATH.exists():
        hist = list(csv.reader(HISTORY_PATH.open(encoding="utf-8")))[1:]
    lv_colors = ["#1a7f37", "#bf8700", "#fb8500", "#cf222e"]
    lv_color = lv_colors[min(res["level_idx"], 3)]

    cats: dict[str, list] = {}
    for r in res["indicators"]:
        cats.setdefault(r["category"], []).append(r)

    cat_cards = "".join(
        f'<div class="cat"><div class="muted">{html.escape(c)}</div>'
        f'<div class="big" style="color:{lv_colors[min(int(s // 20), 3)]}">{s:.0f}</div></div>'
        for c, s in res["category_scores"].items())

    sections = []
    for cat, rs in cats.items():
        rows = []
        for r in rs:
            color = STATUS_COLOR[r["status"]]
            rules = "<br>".join(
                f'<span style="color:{STATUS_COLOR[x["status"]]}">●</span> {html.escape(x["text"])}'
                for x in r["rules"]) or f'<span class="muted">{html.escape(r.get("error") or "")}</span>'
            note = f'<div class="note">{html.escape(r["note"])}</div>' if r.get("note") else ""
            stale = ' <span class="tag">数据陈旧</span>' if r.get("stale") else ""
            rows.append(
                f'<tr><td><span class="badge" style="background:{color}">{STATUS_NAME[r["status"]]}</span></td>'
                f'<td><b>{html.escape(r["name"])}</b>{stale}{note}</td>'
                f'<td class="num">{fmt(r["value"], r["unit"] or "") if r["value"] is not None else "-"}'
                f'<div class="muted">{r["date"] or ""}</div></td>'
                f'<td>{sparkline(r["series"], color)}</td><td class="rules">{rules}</td></tr>')
        sections.append(f'<h2>{html.escape(cat)}</h2><table><thead><tr><th>状态</th><th>指标</th>'
                        f'<th>最新值</th><th>近半年走势</th><th>规则判定</th></tr></thead>'
                        f'<tbody>{"".join(rows)}</tbody></table>')

    manual = "".join(f"<li>{html.escape(m)}</li>" for m in cfg.get("manual_checks", []))
    errs = ("<div class='err'><b>数据获取失败：</b><br>" + "<br>".join(html.escape(e) for e in res["errors"]) +
            "</div>") if res["errors"] else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="明斯基预警">
<link rel="manifest" href="manifest.json">
<title>明斯基时刻预警看板</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; margin: 0; padding: 24px;
       background: #f6f8fa; color: #24292f; }}
@media (prefers-color-scheme: dark) {{ body {{ background:#0d1117; color:#e6edf3; }}
  .card, table {{ background:#161b22 !important; }} th {{ background:#21262d !important; }}
  td, th {{ border-color:#30363d !important; }} polyline[stroke="#24292f"] {{ stroke:#e6edf3; }} }}
.wrap {{ max-width: 1180px; margin: 0 auto; }}
h1 {{ margin: 0 0 4px; font-size: 24px; }} h2 {{ margin: 28px 0 10px; font-size: 18px; }}
.muted {{ color: #8c959f; font-size: 12px; }}
.top {{ display: grid; grid-template-columns: 260px 1fr; gap: 16px; margin-top: 16px; }}
.card {{ background: #fff; border-radius: 12px; padding: 18px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
.score {{ font-size: 56px; font-weight: 700; line-height: 1; }}
.cats {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 16px; }}
.cat {{ flex: 1; min-width: 120px; }} .big {{ font-size: 28px; font-weight: 700; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden;
        box-shadow: 0 1px 3px rgba(0,0,0,.08); }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid #eaeef2; text-align: left; vertical-align: middle; font-size: 13px; }}
th {{ background: #f6f8fa; font-weight: 600; }}
.num {{ font-variant-numeric: tabular-nums; white-space: nowrap; font-size: 15px; }}
.rules {{ font-size: 12px; line-height: 1.6; }}
.badge {{ color: #fff; padding: 3px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }}
.note {{ color: #8c959f; font-size: 12px; margin-top: 3px; max-width: 320px; }}
.tag {{ background:#ffebe9; color:#cf222e; font-size:11px; padding:1px 6px; border-radius:6px; }}
.err {{ background:#ffebe9; color:#82071e; padding:12px; border-radius:8px; margin-top:16px; font-size:13px; }}
@media (max-width: 760px) {{ .top {{ grid-template-columns: 1fr; }} td:nth-child(4), th:nth-child(4) {{ display:none; }} }}
</style></head><body><div class="wrap">
<h1>明斯基时刻预警看板</h1>
<div class="muted">更新时间 {res['time']} · 数据源 FRED / Yahoo Finance · 仅供参考，不构成投资建议</div>
<div class="top">
  <div class="card"><div class="muted">综合风险分（0–100）</div>
    <div class="score" style="color:{lv_color}">{res['score']:.0f}</div>
    <div style="font-size:20px;margin-top:6px">{lv['emoji']} {html.escape(lv['name'])}</div>
    <div class="muted" style="margin-top:8px">0–20 正常 · 20–40 关注 · 40–60 警戒 · 60+ 高危</div></div>
  <div class="card"><div class="muted">综合风险分走势</div>{history_svg(hist)}
    <div class="cats">{cat_cards}</div></div>
</div>
{errs}
{''.join(sections)}
<h2>需要人工跟踪的信号</h2><div class="card"><ul>{manual}</ul></div>
</div></body></html>"""


def write_outputs(cfg: dict, res: dict, record_history: bool) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if record_history:
        new = not HISTORY_PATH.exists()
        with HISTORY_PATH.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "score", "level"] + [i["key"] for i in cfg["indicators"]])
            w.writerow([res["time"], res["score"], res["level"]["name"]] +
                       [r["status"] for r in res["indicators"]])
    out = REPORT_DIR / "latest.html"
    out.write_text(render_html(cfg, res), "utf-8")
    slim = {k: v for k, v in res.items() if k != "indicators"}
    slim["indicators"] = [{k: v for k, v in r.items() if k != "series"} for r in res["indicators"]]
    (REPORT_DIR / "latest.json").write_text(json.dumps(slim, ensure_ascii=False, indent=2), "utf-8")
    return out


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="明斯基时刻预警监控")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--dry-run", action="store_true", help="不推送、不更新状态")
    ap.add_argument("--force-notify", action="store_true", help="强制推送当前摘要")
    ap.add_argument("--test-notify", action="store_true", help="只发送测试通知")
    ap.add_argument("--open", action="store_true", help="运行后打开 HTML 看板")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text("utf-8"))

    if args.test_notify:
        send_all(cfg, "🧪 明斯基监控测试通知", "如果你看到这条消息，说明推送渠道配置成功。")
        return 0

    log("开始抓取指标…")
    res = evaluate(cfg)
    state = load_state()
    first_run = not state
    up, down, lv_up, lv_down = diff_state(res, state)
    log(f"综合风险分 {res['score']} → {res['level']['emoji']} {res['level']['name']}")

    report = write_outputs(cfg, res, record_history=not args.dry_run)
    log(f"看板已生成: {report}")

    ncfg = cfg["notify"]
    today = date.today().isoformat()
    should = False
    if args.force_notify:
        should = True
    elif first_run:
        should = True  # 首次运行推送一次基线摘要
    else:
        if ncfg.get("on_escalation", True) and (up or lv_up):
            should = True
        if ncfg.get("on_recovery", True) and (down or lv_down):
            should = True
        if ncfg.get("daily_summary") and state.get("last_summary_date") != today:
            should = True
    total = len(cfg["indicators"])
    if ncfg.get("notify_on_fetch_errors", True) and len(res["errors"]) >= max(3, total // 3):
        should = True

    if should and not args.dry_run:
        title, body = build_message(res, up, down, lv_up, lv_down,
                                    force=args.force_notify or first_run)
        if first_run and not args.force_notify:
            title = f"{res['level']['emoji']} 明斯基监控已启动：当前「{res['level']['name']}」"
        log("推送预警…")
        print(title + "\n" + body)
        send_all(cfg, title, body)
    elif should:
        title, body = build_message(res, up, down, lv_up, lv_down, force=True)
        log("[dry-run] 将推送：\n" + title + "\n" + body)
    else:
        log("无状态变化，不推送。")

    if not args.dry_run:
        save_state(res)
        if ncfg.get("daily_summary") and should:
            st = load_state()
            st["last_summary_date"] = today
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), "utf-8")

    if args.open:
        subprocess.run(["open", str(report)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
