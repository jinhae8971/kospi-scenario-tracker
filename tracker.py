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
# 한 번의 실행에서 따라잡을 수 있는 최대 거래일 수.
# GitHub 스케줄이 몇 시간씩 밀리거나 소스가 며칠 죽어도 다음 실행이 스스로 메운다.
MAX_CATCHUP_DAYS = 10
# KRX 마감 15:30 + 정산 버퍼. 이 시각 이전 실행은 당일 종가를 확정된 것으로 보지 않는다.
CLOSE_SETTLED_HOUR = 16
# 코스피 역대 최대 일간 변동은 ±12% 수준. 이를 넘는 값은 실제 급변일 수도 있으나
# 소스 파싱 오류/지수 교체일 가능성이 더 크므로 기록은 하되 반드시 사람에게 알린다.
MOVE_ALERT_PCT = 10.0
# 알림에서 바로 대시보드로 이동할 수 있어야 한다. 포크/이전 대비해 env 로 덮어쓸 수 있게 둔다.
DASHBOARD_URL = os.getenv("DASHBOARD_URL",
                          "https://jinhae8971.github.io/kospi-scenario-tracker/")

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
        # HTTP 200 이어도 ok:false 인 경우가 있어 본문까지 확인해야 "조용한 미전달"을 잡는다
        payload = r.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "ok=false"))
        mid = (payload.get("result") or {}).get("message_id", "?")
        print(f"[telegram] 발송 완료 (message_id={mid})")
    except Exception as e:
        # 알림 실패로 데이터 수집까지 실패시키지는 않되, 로그에서 눈에 띄게 남긴다
        print(f"::warning::[telegram] 발송 실패: {e}")


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

def _market_day_ceiling(now: dt.datetime) -> dt.date:
    """지금 수집 가능한 최신 거래일.

    스케줄이 밀려 KST 자정을 넘겨 실행돼도 '실행 시각의 오늘'을 대상으로 삼지 않는다.
    (밀린 실행이 아직 열리지도 않은 날을 조회해 매번 빈손으로 끝나던 원인)
    """
    d = now.date()
    if now.hour < CLOSE_SETTLED_HOUR:
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _pending_dates(history: list, ceiling: dt.date,
                   limit: int = MAX_CATCHUP_DAYS) -> list:
    """마지막 관측 다음 영업일부터 상한까지 — 놓친 날을 함께 따라잡는다."""
    have = {r.get("date") for r in history}
    last = max((r["date"] for r in history if r.get("kospi") is not None), default=None)
    d = (dt.date.fromisoformat(last) + dt.timedelta(days=1)) if last else ceiling
    out = []
    while d <= ceiling:
        if d.weekday() < 5 and d.isoformat() not in have:
            out.append(d)
        d += dt.timedelta(days=1)
    return out[-limit:]


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

    store = load_json(HISTORY_PATH, {"history": []})
    if not isinstance(store, dict):
        store = {"history": []}
    history = store.get("history") or []

    forced = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else ""
    if forced:
        try:
            one = dt.date.fromisoformat(forced)
        except ValueError:
            print(f"[치명] 날짜 형식 오류: {forced!r} (YYYY-MM-DD)")
            return 1
        if one > now_kst.date():
            print(f"[치명] 미래 일자는 수집할 수 없음: {one}")
            return 1
        if one.weekday() >= 5:
            print(f"{one} 주말 - 종료")
            return 0
        ceiling, targets = one, [one]
    else:
        ceiling = _market_day_ceiling(now_kst)
        targets = _pending_dates(history, ceiling)
        if targets:
            print(f"수집 대상 {len(targets)}건: {targets[0]} ~ {targets[-1]} (상한 {ceiling})")
        else:
            print(f"수집 대상 없음 - 최신 거래일 {ceiling} 까지 이미 반영, 재계산만 수행")

    peak = float(scen_cfg["meta"]["peak"]["value"])
    collected, misses = [], []
    for t in targets:
        t_iso = t.isoformat()
        if any(r.get("date") == t_iso for r in history):
            print(f"{t} 이미 기록됨 - 건너뜀")
            continue
        r = fetch_naver(t) or fetch_yfinance(t)
        if not r or r.get("kospi") is None:
            print(f"{t} 데이터 확보 실패(휴장 또는 소스 장애)")
            misses.append(t)
            continue
        # 폴백 소스가 다른 날짜를 반환하는 경우 방어
        if r.get("date") != t_iso:
            print(f"[경고] 수집 일자 불일치({r.get('date')} != {t_iso}) - 폐기")
            misses.append(t)
            continue

        prev_r = _prev_row(history, t_iso)
        r["from_peak"] = round((r["kospi"] / peak - 1) * 100, 2)
        prev_cum = prev_r.get("foreign_cum") if prev_r else None
        fn = r.get("foreign_net")
        # 외국인 순매수를 한 번도 수집하지 못한 구간은 0이 아니라 미측정(null)로 둔다
        r["foreign_cum"] = None if (fn is None and prev_cum is None) else int((prev_cum or 0) + (fn or 0))
        if r.get("samsung") and r.get("hynix"):
            r["chip_pair"] = round(r["samsung"] + r["hynix"] / 10, 2)
        history.append(r)
        history.sort(key=lambda x: x["date"])
        collected.append(r)

    # 따라잡기까지 시도했는데 한 건도 못 채웠다면 휴장이 아니라 소스 장애 쪽에 무게를 둔다
    if misses and not collected:
        _stale_guard(history, ceiling, cfg)

    if not history:
        print("[치명] 관측 이력이 비어 있음 - 저장 생략")
        return 1

    row = collected[-1] if collected else None
    target_iso = row["date"] if row else history[-1]["date"]
    prev = _prev_row(history, target_iso)
    prev_close = prev["kospi"] if prev else None

    # 이상치·임계 돌파는 따라잡은 날들에 대해 각각 판정한다(한 날만 보면 놓친다)
    anomaly = None
    new_events = []
    for r in collected:
        p = _prev_row(history, r["date"])
        p_close = p["kospi"] if p else None
        if p_close:
            move = (r["kospi"] / p_close - 1) * 100
            if abs(move) > MOVE_ALERT_PCT:
                msg = (f"전일 대비 {move:+.2f}% — 일간 변동 한계({MOVE_ALERT_PCT:.0f}%) 초과. "
                       f"소스 데이터 확인 필요")
                print(f"[경고] {r['date']} {msg}")
                r["anomaly"] = round(move, 2)
                if r is row:
                    anomaly = msg
        for t in check_thresholds(r["kospi"], p_close, scen_cfg):
            new_events.append({"date": r["date"], "level": t["level"],
                               "label": t["label"], "signal": t["signal"]})

    verdict = evaluate(history, scen_cfg)
    latest = next((r for r in history if r.get("date") == target_iso), history[-1])
    hits = check_thresholds(latest["kospi"], prev_close, scen_cfg) if row else []

    body = {
        "history": history,
        "verdict": verdict,
        "events": _dedup_events((store.get("events") or []) + new_events),
    }
    # 실질 내용이 그대로면 updated_at 도 그대로 둔다.
    # 이렇게 해야 휴장일/재계산 실행이 타임스탬프만 바꾼 빈 커밋을 만들지 않는다.
    unchanged = all(store.get(k) == v for k, v in body.items()) and "updated_at" in store
    stamp = store["updated_at"] if unchanged else now_kst.isoformat(timespec="seconds")
    store = {"updated_at": stamp, **body}
    save_json(HISTORY_PATH, store)
    if unchanged:
        print("[저장] 내용 동일 - updated_at 유지 (빈 커밋 방지)")
    print(f"[저장] {latest['date']} 코스피 {latest['kospi']:,} / 추종 시나리오 {verdict['leader']}")

    # 알림 경로 점검이나 놓친 요약 재발송을 위한 수동 스위치
    force_notify = os.getenv("FORCE_NOTIFY", "").strip().lower() in ("1", "true", "yes", "on")
    if not row and not force_notify:
        # 재계산만 한 경우 중복 알림을 보내지 않는다
        print("[telegram] 신규 관측 없음 - 발송 생략")
        return 0
    if not row:
        print("[telegram] 신규 관측 없음 - FORCE_NOTIFY 로 강제 발송")

    send_telegram(build_summary(latest, prev_close, verdict, scen_cfg, hits, anomaly), cfg)
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
                  scen_cfg: dict, hits: list, anomaly: Optional[str] = None) -> str:
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
    if anomaly:
        lines.insert(1, f"\u26a0\ufe0f <b>\uc774\uc0c1\uce58 \uac10\uc9c0</b> \u2014 {_esc(anomaly)}")
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
    lines += ["", f'\U0001F517 <a href="{DASHBOARD_URL}">\ub300\uc2dc\ubcf4\ub4dc\uc5d0\uc11c \uc804\uccb4 \ucc28\ud2b8 \ubcf4\uae30</a>']
    lines += ["", "<i>\uc815\ubcf4 \uc81c\uacf5 \ubaa9\uc801\uc774\uba70 \ud22c\uc790 \uc790\ubb38\uc774 \uc544\ub2d9\ub2c8\ub2e4.</i>"]
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
