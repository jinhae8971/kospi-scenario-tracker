"""
코스피 2026 급락장 시나리오 트래커
평일 장 마감 후 종가를 누적하고, 3개 시나리오 중 어느 경로를 추종 중인지 베이지안 갱신.

데이터 소스: 네이버 금융 API(1순위) -> yfinance(폴백)
  * pykrx 1.2.x 부터 KRX 로그인(KRX_ID/KRX_PW)이 필수라 사용하지 않음
출력: docs/data/history.json (GitHub Pages 대시보드가 읽음)
"""

import os
import re
import sys
import json
import math
import datetime as dt
from typing import Optional

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
SCENARIO_PATH = os.path.join(ROOT, "scenarios.json")

KST = dt.timezone(dt.timedelta(hours=9))

TICKERS = {"samsung": "005930", "hynix": "000660"}


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    local = os.path.join(ROOT, "config.json")
    if os.path.exists(local):
        with open(local, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


def send_telegram(text: str, cfg: dict) -> None:
    token, chat_id = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        r.raise_for_status()
        print("[telegram] 발송 완료")
    except Exception as e:
        print(f"[telegram] 발송 실패: {e}")


# ---------------------------------------------------------------- fetch

def _yyyymmdd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _sise_rows(symbol: str, target: dt.date) -> list:
    """네이버 siseJson -> [[yyyymmdd, 시가, 고가, 저가, 종가, 거래량, 외국인소진율], ...]"""
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={symbol}&requestType=1"
           f"&startTime={_yyyymmdd(target - dt.timedelta(days=12))}"
           f"&endTime={_yyyymmdd(target)}&timeframe=day")
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://finance.naver.com/"},
                     timeout=20)
    r.raise_for_status()
    rows = []
    for m in re.finditer(r'\["(\d{8})"\s*,([^\]]*)\]', r.text):
        vals = []
        for tok in m.group(2).split(","):
            tok = tok.strip()
            try:
                vals.append(float(tok))
            except ValueError:
                vals.append(None)
        rows.append([m.group(1)] + vals)
    return rows


def _foreign_net(target: dt.date) -> Optional[int]:
    """네이버 모바일 API 투자자별 매매동향(당일 기준, 단위 억원 -> 원)."""
    try:
        r = requests.get("https://m.stock.naver.com/api/index/KOSPI/trend",
                         headers={"User-Agent": UA, "Referer": "https://m.stock.naver.com/"},
                         timeout=20)
        r.raise_for_status()
        d = r.json()
        if d.get("bizdate") != _yyyymmdd(target):
            print(f"[naver] 외국인 수급 기준일 불일치({d.get('bizdate')}) - 생략")
            return None
        v = str(d.get("foreignValue", "")).replace(",", "").replace("+", "")
        return int(round(float(v) * 1e8))
    except Exception as e:
        print(f"[naver] 외국인 수급 조회 실패: {e}")
        return None


def fetch_naver(target: dt.date) -> Optional[dict]:
    """1순위 소스. 네이버 금융 시세 API (인증 불필요, KRX 정산 직후 반영)."""
    day = _yyyymmdd(target)
    try:
        idx = _sise_rows("KOSPI", target)
    except Exception as e:
        print(f"[naver] 지수 조회 실패: {e}")
        return None

    hit = next((r for r in idx if r[0] == day), None)
    if not hit:
        last = idx[-1][0] if idx else "없음"
        print(f"[naver] {target} 지수 데이터 없음(휴장 추정) - 최신 거래일 {last}")
        return None

    out = {
        "date": target.isoformat(),
        "kospi": round(hit[4], 2),
        "volume_value": None,
        "source": "naver",
    }

    for key, code in TICKERS.items():
        try:
            rows = _sise_rows(code, target)
            r = next((x for x in rows if x[0] == day), None)
            out[key] = round(r[4], 2) if r else None
        except Exception as e:
            print(f"[naver] {key} 조회 실패: {e}")
            out[key] = None

    out["foreign_net"] = _foreign_net(target)
    return out


def fetch_yfinance(target: dt.date) -> Optional[dict]:
    try:
        import yfinance as yf
    except ImportError:
        print("[yfinance] 미설치")
        return None

    symbols = {"kospi": "^KS11", "samsung": "005930.KS", "hynix": "000660.KS"}
    out = {"foreign_net": None, "volume_value": None, "source": "yfinance"}
    start = target - dt.timedelta(days=10)
    end = target + dt.timedelta(days=1)

    for key, sym in symbols.items():
        try:
            df = yf.download(sym, start=start, end=end, progress=False, auto_adjust=False)
            if df is None or df.empty:
                out[key] = None
                continue
            df = df.dropna()
            last_date = df.index[-1].date()
            if key == "kospi":
                if last_date != target:
                    print(f"[yfinance] {target} 데이터 없음 - 최신 {last_date}")
                    return None
                out["date"] = last_date.isoformat()
            close = df["Close"].iloc[-1]
            out[key] = round(float(close.item() if hasattr(close, "item") else close), 2)
        except Exception as e:
            print(f"[yfinance] {key} 실패: {e}")
            out[key] = None

    return out if out.get("kospi") else None


# ---------------------------------------------------------------- scenario math

def interp_path(path: list, target: dt.date) -> Optional[float]:
    """앵커 포인트 사이를 일자 기준 선형보간."""
    pts = [(dt.date.fromisoformat(d), float(v)) for d, v in path]
    if target <= pts[0][0]:
        return pts[0][1]
    if target >= pts[-1][0]:
        return pts[-1][1]
    for i in range(len(pts) - 1):
        d0, v0 = pts[i]
        d1, v1 = pts[i + 1]
        if d0 <= target <= d1:
            span = (d1 - d0).days
            if span == 0:
                return v1
            w = (target - d0).days / span
            return v0 + (v1 - v0) * w
    return None


def evaluate(history: list, scen_cfg: dict) -> dict:
    """누적 관측치로 시나리오별 RMSE 및 사후확률 계산."""
    scenarios = scen_cfg["scenarios"]
    tau = float(scen_cfg["meta"].get("tau", 0.055))
    base = dt.date.fromisoformat(scen_cfg["meta"]["base_date"])
    results = []

    for s in scenarios:
        errs = []
        for row in history:
            if row.get("kospi") is None:
                continue
            d = dt.date.fromisoformat(row["date"])
            if d < base:  # 시나리오 기산일 이전 관측치는 평가에서 제외
                continue
            expected = interp_path(s["path"], d)
            if not expected:
                continue
            errs.append((row["kospi"] - expected) / expected)
        n = len(errs)
        rmse = math.sqrt(sum(e * e for e in errs) / n) if n else 0.0
        last_err = errs[-1] if errs else 0.0
        results.append({
            "id": s["id"], "name": s["name"], "color": s["color"],
            "prior": s["prior"], "rmse": round(rmse, 5),
            "last_gap": round(last_err, 5), "n": n,
        })

    # 사후확률 = prior x exp(-RMSE/tau), 정규화
    weights = [r["prior"] * math.exp(-r["rmse"] / tau) for r in results]
    total = sum(weights) or 1.0
    for r, w in zip(results, weights):
        r["posterior"] = round(w / total, 4)

    results.sort(key=lambda r: -r["posterior"])
    return {"scenarios": results, "leader": results[0]["id"]}


def check_thresholds(kospi: float, prev: Optional[float], scen_cfg: dict) -> list:
    hits = []
    if prev is None:
        return hits
    for t in scen_cfg.get("thresholds", []):
        lvl = t["level"]
        if t["dir"] == "down" and prev >= lvl > kospi:
            hits.append(t)
        if t["dir"] == "up" and prev <= lvl < kospi:
            hits.append(t)
    return hits


# ---------------------------------------------------------------- io

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- main

def main() -> int:
    cfg = load_config()
    scen_cfg = load_json(SCENARIO_PATH, None)
    if not scen_cfg:
        print("scenarios.json 없음")
        return 1

    now_kst = dt.datetime.now(KST)
    target = now_kst.date()
    if len(sys.argv) > 1:
        target = dt.date.fromisoformat(sys.argv[1])

    if target.weekday() >= 5:
        print(f"{target} 주말 - 종료")
        return 0

    store = load_json(HISTORY_PATH, {"history": []})
    history = store.get("history", [])

    if any(r["date"] == target.isoformat() for r in history):
        print(f"{target} 이미 기록됨 - 재계산만 수행")
        row = None
    else:
        row = fetch_naver(target) or fetch_yfinance(target)
        if not row:
            print(f"{target} 데이터 확보 실패(휴장 또는 소스 장애) - 종료")
            return 0

    peak = float(scen_cfg["meta"]["peak"]["value"])
    prev_close = history[-1]["kospi"] if history else None

    if row:
        row["from_peak"] = round((row["kospi"] / peak - 1) * 100, 2)
        prev_cum = history[-1].get("foreign_cum") if history else None
        fn = row.get("foreign_net")
        # 외국인 순매수를 한 번도 수집하지 못한 구간은 0이 아니라 미측정(null)로 둔다
        row["foreign_cum"] = None if (fn is None and prev_cum is None) else int((prev_cum or 0) + (fn or 0))
        if row.get("samsung") and row.get("hynix"):
            row["chip_pair"] = round(row["samsung"] + row["hynix"] / 10, 2)
        history.append(row)
        history.sort(key=lambda r: r["date"])

    verdict = evaluate(history, scen_cfg)
    latest = history[-1]
    hits = check_thresholds(latest["kospi"], prev_close, scen_cfg) if row else []

    store = {
        "updated_at": now_kst.isoformat(timespec="seconds"),
        "history": history,
        "verdict": verdict,
        "events": (store.get("events", []) if isinstance(store, dict) else []) + [
            {"date": latest["date"], "level": t["level"], "label": t["label"], "signal": t["signal"]}
            for t in hits
        ],
    }
    save_json(HISTORY_PATH, store)
    print(f"[저장] {latest['date']} 코스피 {latest['kospi']:,} / 추종 시나리오 {verdict['leader']}")

    # ---- 텔레그램 요약
    lead = verdict["scenarios"][0]
    name_map = {s["id"]: s["name"] for s in scen_cfg["scenarios"]}
    bars = "\n".join(
        f"  {s['id']}. {name_map[s['id']]} \u2014 <b>{s['posterior']*100:.0f}%</b>"
        f" (\uacbd\ub85c \uc774\ud0c8 {s['last_gap']*100:+.1f}%)"
        for s in verdict["scenarios"]
    )

    chg = ""
    if prev_close:
        d = (latest["kospi"] / prev_close - 1) * 100
        chg = f" ({d:+.2f}%)"

    lines = [
        f"\U0001F4CA <b>\ucf54\uc2a4\ud53c \uc2dc\ub098\ub9ac\uc624 \ud2b8\ub798\ucee4</b> \u00b7 {latest['date']}",
        "",
        f"\uc885\uac00 <b>{latest['kospi']:,.2f}</b>{chg}",
        f"\uace0\uc810(9,385.59) \ub300\ube44 <b>{latest['from_peak']:+.1f}%</b>",
    ]
    if latest.get("samsung"):
        lines.append(
            f"\uc0bc\uc131\uc804\uc790 {latest['samsung']:,.0f}"
            f" / SK\ud558\uc774\ub2c9\uc2a4 {latest.get('hynix') or 0:,.0f}"
        )
    if latest.get("foreign_net") is not None:
        lines.append(
            f"\uc678\uad6d\uc778 \uc21c\ub9e4\uc218 {latest['foreign_net']/1e8:,.0f}\uc5b5"
            f" (\ub204\uc801 {latest['foreign_cum']/1e12:,.2f}\uc870)"
        )

    lines += [
        "",
        "<b>\uc2dc\ub098\ub9ac\uc624\ubcc4 \uc0ac\ud6c4\ud655\ub960</b>",
        bars,
        "",
        f"\ud604\uc7ac \uacbd\ub85c\ub294 <b>{lead['id']}. {name_map[lead['id']]}</b>\uc5d0"
        f" \uac00\uc7a5 \uadfc\uc811 (\ub204\uc801 {lead['n']}\uc77c \uad00\uce21)",
    ]
    if hits:
        lines += [""] + [f"\u26a0\ufe0f {t['label']}" for t in hits]
    lines += ["", "<i>\uc815\ubcf4 \uc81c\uacf5 \ubaa9\uc801\uc774\uba70 \ud22c\uc790 \uc790\ubb38\uc774 \uc544\ub2d9\ub2c8\ub2e4.</i>"]

    send_telegram("\n".join(lines), cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
