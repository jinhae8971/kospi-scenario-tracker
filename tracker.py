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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "docs", "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
SCENARIO_PATH = os.path.join(ROOT, "scenarios.json")

KST = dt.timezone(dt.timedelta(hours=9))

TICKERS = {"samsung": "005930", "hynix": "000660"}

# 마지막 관측일이 이 일수보다 오래되면 소스 장애로 간주하고 경보
STALE_ALERT_DAYS = 5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _session() -> requests.Session:
    """지수 백오프 재시도가 붙은 세션. 네이버/텔레그램 일시 장애를 흡수한다."""
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=(408, 429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]),
                  raise_on_status=False)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("https://", ad)
    s.mount("http://", ad)
    s.headers.update({"User-Agent": UA})
    return s


SESSION = _session()


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    local = os.path.join(ROOT, "config.json")
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] config.json 읽기 실패: {e}")
            return cfg
        # TELEGRAM_TOKEN / telegram_token 어느 표기로 써도 인식
        for k, v in raw.items():
            key = str(k).strip().lower()
            if key in cfg and not cfg.get(key):
                cfg[key] = v
    return cfg


def send_telegram(text: str, cfg: dict) -> None:
    token, chat_id = cfg.get("telegram_token"), cfg.get("telegram_chat_id")
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
        return
    try:
        r = SESSION.post(
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


def _sise_rows(symbol: str, target: dt.date) -> list:
    """네이버 siseJson -> [[yyyymmdd, 시가, 고가, 저가, 종가, 거래량, 외국인소진율], ...]"""
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={symbol}&requestType=1"
           f"&startTime={_yyyymmdd(target - dt.timedelta(days=12))}"
           f"&endTime={_yyyymmdd(target)}&timeframe=day")
    r = SESSION.get(url, headers={"Referer": "https://finance.naver.com/"}, timeout=20)
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
        r = SESSION.get("https://m.stock.naver.com/api/index/KOSPI/trend",
                        headers={"Referer": "https://m.stock.naver.com/"}, timeout=20)
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

    hit = next((r for r in idx if r[0] == day and len(r) > 4), None)
    if not hit:
        last = idx[-1][0] if idx else "없음"
        print(f"[naver] {target} 지수 데이터 없음(휴장 추정) - 최신 거래일 {last}")
        return None

    if hit[4] is None:
        print(f"[naver] {target} 종가 필드 비어 있음 - 폴백 소스로 이관")
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
    pts = sorted(((dt.date.fromisoformat(d), float(v)) for d, v in path), key=lambda x: x[0])
    if not pts:
        return None
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
            if expected is None or expected == 0:
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


def validate_config(cfg: dict) -> list:
    """scenarios.json 스키마 검증. 잘못된 설정으로 계산이 조용히 틀어지는 것을 막는다."""
    errs = []
    meta = cfg.get("meta") or {}
    for k in ("base_date", "base_value", "tau", "peak"):
        if meta.get(k) in (None, ""):
            errs.append(f"meta.{k} 누락")
    if not isinstance(meta.get("peak"), dict) or meta.get("peak", {}).get("value") in (None, ""):
        errs.append("meta.peak.value 누락")
    try:
        if float(meta.get("tau", 0)) <= 0:
            errs.append("meta.tau 는 0보다 커야 함")
    except (TypeError, ValueError):
        errs.append("meta.tau 가 숫자가 아님")

    scen = cfg.get("scenarios") or []
    if len(scen) < 2:
        errs.append("scenarios 는 2개 이상이어야 함")
    ids = set()
    for s in scen:
        sid = s.get("id")
        if not sid or sid in ids:
            errs.append(f"scenario id 중복/누락: {sid!r}")
        ids.add(sid)
        for k in ("name", "color", "prior", "path"):
            if s.get(k) in (None, ""):
                errs.append(f"scenario {sid}.{k} 누락")
        path = s.get("path") or []
        if len(path) < 2:
            errs.append(f"scenario {sid}.path 앵커가 2개 미만")
        for pt in path:
            if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
                errs.append(f"scenario {sid}.path 항목 형식 오류: {pt!r}")
                continue
            try:
                dt.date.fromisoformat(str(pt[0]))
                float(pt[1])
            except (TypeError, ValueError):
                errs.append(f"scenario {sid}.path 값 오류: {pt!r}")

    prior_sum = sum(float(s.get("prior") or 0) for s in scen)
    if scen and abs(prior_sum - 1.0) > 1e-6:
        errs.append(f"prior 합이 1이 아님: {prior_sum:.6f}")

    for t in cfg.get("thresholds", []):
        if t.get("dir") not in ("up", "down"):
            errs.append(f"threshold.dir 오류: {t.get('dir')!r}")
        if not isinstance(t.get("level"), (int, float)):
            errs.append(f"threshold.level 오류: {t.get('level')!r}")
    return errs


def save_json(path: str, obj) -> None:
    """임시 파일에 먼저 쓰고 원자적으로 교체 — 중단되어도 기존 파일이 깨지지 않는다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- main

def _prev_row(history: list, target_iso: str) -> Optional[dict]:
    """target 직전 거래일 행. 과거 일자 백필 시에도 항상 올바른 비교 대상을 고른다."""
    prior = [r for r in history if r.get("date", "") < target_iso and r.get("kospi") is not None]
    return prior[-1] if prior else None


def _dedup_events(events: list) -> list:
    seen, out = set(), []
    for e in events:
        key = (e.get("date"), e.get("level"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda e: (e.get("date", ""), e.get("level", 0)))
    return out


def _esc(v) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main() -> int:
    cfg = load_config()
    scen_cfg = load_json(SCENARIO_PATH, None)
    if not scen_cfg:
        print(f"[치명] scenarios.json 없음: {SCENARIO_PATH}")
        return 1

    errs = validate_config(scen_cfg)
    if errs:
        print("[치명] scenarios.json 스키마 오류")
        for e in errs:
            print(f"  - {e}")
        return 1

    now_kst = dt.datetime.now(KST)
    target = now_kst.date()
    if len(sys.argv) > 1 and sys.argv[1].strip():
        try:
            target = dt.date.fromisoformat(sys.argv[1].strip())
        except ValueError:
            print(f"[치명] 날짜 형식 오류: {sys.argv[1]!r} (YYYY-MM-DD)")
            return 1
        if target > now_kst.date():
            print(f"[치명] 미래 일자는 수집할 수 없음: {target}")
            return 1

    if target.weekday() >= 5:
        print(f"{target} 주말 - 종료")
        return 0

    store = load_json(HISTORY_PATH, {"history": []})
    if not isinstance(store, dict):
        store = {"history": []}
    history = store.get("history") or []
    target_iso = target.isoformat()

    already = any(r.get("date") == target_iso for r in history)
    if already:
        print(f"{target} 이미 기록됨 - 재계산만 수행")
        row = None
    else:
        row = fetch_naver(target) or fetch_yfinance(target)
        if not row or row.get("kospi") is None:
            print(f"{target} 데이터 확보 실패(휴장 또는 소스 장애)")
            _stale_guard(history, target, cfg)
            return 0
        # 폴백 소스가 다른 날짜를 반환하는 경우 방어
        if row.get("date") != target_iso:
            print(f"[경고] 수집 일자 불일치({row.get('date')} != {target_iso}) - 폐기")
            _stale_guard(history, target, cfg)
            return 0

    peak = float(scen_cfg["meta"]["peak"]["value"])
    prev = _prev_row(history, target_iso)
    prev_close = prev["kospi"] if prev else None

    if row:
        row["from_peak"] = round((row["kospi"] / peak - 1) * 100, 2)
        prev_cum = prev.get("foreign_cum") if prev else None
        fn = row.get("foreign_net")
        # 외국인 순매수를 한 번도 수집하지 못한 구간은 0이 아니라 미측정(null)로 둔다
        row["foreign_cum"] = None if (fn is None and prev_cum is None) else int((prev_cum or 0) + (fn or 0))
        if row.get("samsung") and row.get("hynix"):
            row["chip_pair"] = round(row["samsung"] + row["hynix"] / 10, 2)
        history.append(row)
        history.sort(key=lambda r: r["date"])

    verdict = evaluate(history, scen_cfg)
    latest = next((r for r in history if r.get("date") == target_iso), history[-1])
    hits = check_thresholds(latest["kospi"], prev_close, scen_cfg) if row else []

    store = {
        "updated_at": now_kst.isoformat(timespec="seconds"),
        "history": history,
        "verdict": verdict,
        "events": _dedup_events((store.get("events") or []) + [
            {"date": latest["date"], "level": t["level"], "label": t["label"], "signal": t["signal"]}
            for t in hits
        ]),
    }
    save_json(HISTORY_PATH, store)
    print(f"[저장] {latest['date']} 코스피 {latest['kospi']:,} / 추종 시나리오 {verdict['leader']}")

    if not row:
        # 재계산만 한 경우 중복 알림을 보내지 않는다
        print("[telegram] 신규 관측 없음 - 발송 생략")
        return 0

    send_telegram(build_summary(latest, prev_close, verdict, scen_cfg, hits), cfg)
    return 0


def _stale_guard(history: list, target: dt.date, cfg: dict) -> None:
    """마지막 관측이 오래되면 휴장이 아니라 소스 장애일 가능성이 높다 - 1회 경보."""
    last = next((r for r in reversed(history) if r.get("kospi") is not None), None)
    if not last:
        return
    gap = (target - dt.date.fromisoformat(last["date"])).days
    if gap >= STALE_ALERT_DAYS:
        send_telegram(
            f"\u26a0\ufe0f <b>\ucf54\uc2a4\ud53c \ud2b8\ub798\ucee4</b> \ub370\uc774\ud130 \uc815\uccb4"
            f"\n\ub9c8\uc9c0\ub9c9 \uad00\uce21 {last['date']} \u00b7 {gap}\uc77c \uacbd\uacfc"
            f"\n\uc218\uc9d1 \uc18c\uc2a4 \uc7a5\uc560 \uac00\ub2a5\uc131 \u2014 Actions \ub85c\uadf8\ub97c \ud655\uc778\ud558\uc138\uc694.",
            cfg)


def build_summary(latest: dict, prev_close: Optional[float], verdict: dict,
                  scen_cfg: dict, hits: list) -> str:
    lead = verdict["scenarios"][0]
    name_map = {s["id"]: s["name"] for s in scen_cfg["scenarios"]}
    peak_v = float(scen_cfg["meta"]["peak"]["value"])
    bars = "\n".join(
        f"  {_esc(s['id'])}. {_esc(name_map.get(s['id'], s['id']))} \u2014 <b>{s['posterior']*100:.0f}%</b>"
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
        f"\uace0\uc810({peak_v:,.2f}) \ub300\ube44 <b>{latest['from_peak']:+.1f}%</b>",
    ]
    if latest.get("samsung"):
        chips = f"\uc0bc\uc131\uc804\uc790 {latest['samsung']:,.0f}"
        if latest.get("hynix"):
            chips += f" / SK\ud558\uc774\ub2c9\uc2a4 {latest['hynix']:,.0f}"
        lines.append(chips)
    if latest.get("foreign_net") is not None:
        cum = latest.get("foreign_cum")
        cum_txt = f" (\ub204\uc801 {cum/1e12:,.2f}\uc870)" if cum is not None else ""
        lines.append(f"\uc678\uad6d\uc778 \uc21c\ub9e4\uc218 {latest['foreign_net']/1e8:,.0f}\uc5b5{cum_txt}")

    lines += [
        "",
        "<b>\uc2dc\ub098\ub9ac\uc624\ubcc4 \uc0ac\ud6c4\ud655\ub960</b>",
        bars,
        "",
        f"\ud604\uc7ac \uacbd\ub85c\ub294 <b>{_esc(lead['id'])}. {_esc(name_map.get(lead['id'], lead['id']))}</b>\uc5d0"
        f" \uac00\uc7a5 \uadfc\uc811 (\ub204\uc801 {lead['n']}\uc77c \uad00\uce21)",
    ]
    if lead["n"] < 8:
        lines.append("<i>\uad00\uce21 8\uc77c \ubbf8\ub9cc \u2014 \uc0ac\ud6c4\ud655\ub960\uc740 \uc544\uc9c1 \uc0ac\uc804\ud655\ub960\uc5d0 \uac00\uae5d\uc2b5\ub2c8\ub2e4.</i>")
    if hits:
        lines += [""] + [f"\u26a0\ufe0f {_esc(t['label'])}" for t in hits]
    lines += ["", "<i>\uc815\ubcf4 \uc81c\uacf5 \ubaa9\uc801\uc774\uba70 \ud22c\uc790 \uc790\ubb38\uc774 \uc544\ub2d9\ub2c8\ub2e4.</i>"]
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
