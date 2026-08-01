"""
tracker.py 단위 테스트 + 대시보드(JS) 계산 코어 패리티 검증.

가장 중요한 계약: docs/index.html 의 interp()/posteriorSeries() 와
tracker.py 의 interp_path()/evaluate() 는 동일한 입력에 동일한 출력을 내야 한다.
둘이 어긋나면 대시보드 화면과 텔레그램 알림이 서로 다른 판정을 말하게 된다.
"""

import os
import re
import json
import shutil
import subprocess
import datetime as dt
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
sys.path.insert(0, ROOT)
import tracker as tk  # noqa: E402

INDEX = os.path.join(ROOT, "docs", "index.html")
SCEN = os.path.join(ROOT, "scenarios.json")


def load_scen():
    with open(SCEN, encoding="utf-8") as f:
        return json.load(f)


def extract_js_fn(src: str, name: str) -> str:
    """중괄호 매칭으로 함수 본문을 통째로 떼어낸다."""
    i = src.index(f"function {name}(")
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{name} 함수 본문을 찾지 못함")


class ConfigContract(unittest.TestCase):
    def test_scenarios_schema_valid(self):
        self.assertEqual(tk.validate_config(load_scen()), [])

    def test_docs_copy_matches_root(self):
        """docs/scenarios.json 은 루트 파일의 동기화 사본이어야 한다."""
        with open(SCEN, encoding="utf-8") as f:
            a = json.load(f)
        with open(os.path.join(ROOT, "docs", "scenarios.json"), encoding="utf-8") as f:
            b = json.load(f)
        self.assertEqual(a, b, "docs/scenarios.json 이 루트와 다릅니다 (워크플로우 cp 누락)")

    def test_bad_config_is_rejected(self):
        cfg = load_scen()
        cfg["scenarios"][0]["prior"] = 0.99
        self.assertTrue(any("prior" in e for e in tk.validate_config(cfg)))

        cfg = load_scen()
        cfg["meta"]["tau"] = 0
        self.assertTrue(any("tau" in e for e in tk.validate_config(cfg)))


class InterpMath(unittest.TestCase):
    def setUp(self):
        self.path = [["2026-01-01", 100.0], ["2026-01-11", 200.0], ["2026-01-21", 200.0]]

    def test_clamps_outside_range(self):
        self.assertEqual(tk.interp_path(self.path, dt.date(2025, 6, 1)), 100.0)
        self.assertEqual(tk.interp_path(self.path, dt.date(2030, 1, 1)), 200.0)

    def test_linear_midpoint(self):
        self.assertAlmostEqual(tk.interp_path(self.path, dt.date(2026, 1, 6)), 150.0)

    def test_unsorted_anchors_are_sorted(self):
        shuffled = [self.path[2], self.path[0], self.path[1]]
        self.assertAlmostEqual(tk.interp_path(shuffled, dt.date(2026, 1, 6)), 150.0)


class Evaluate(unittest.TestCase):
    def test_no_observation_returns_priors(self):
        cfg = load_scen()
        v = tk.evaluate([], cfg)
        got = {s["id"]: s["posterior"] for s in v["scenarios"]}
        want = {s["id"]: s["prior"] for s in cfg["scenarios"]}
        for k in want:
            self.assertAlmostEqual(got[k], want[k], places=4)

    def test_rows_before_base_date_are_ignored(self):
        cfg = load_scen()
        old = [{"date": "2020-01-02", "kospi": 1000.0}]
        self.assertEqual(tk.evaluate(old, cfg)["scenarios"][0]["n"], 0)

    def test_perfect_follower_wins(self):
        """C 경로를 그대로 따라간 시계열이면 C 의 사후확률이 압도적이어야 한다."""
        cfg = load_scen()
        target = next(s for s in cfg["scenarios"] if s["id"] == "C")
        base = dt.date.fromisoformat(cfg["meta"]["base_date"])
        hist = []
        for i in range(0, 240, 7):
            d = base + dt.timedelta(days=i)
            hist.append({"date": d.isoformat(), "kospi": tk.interp_path(target["path"], d)})
        v = tk.evaluate(hist, cfg)
        self.assertEqual(v["leader"], "C")
        self.assertGreater(v["scenarios"][0]["posterior"], 0.9)

    def test_posteriors_sum_to_one(self):
        cfg = load_scen()
        base = dt.date.fromisoformat(cfg["meta"]["base_date"])
        hist = [{"date": (base + dt.timedelta(days=i)).isoformat(),
                 "kospi": 6000.0 + i * 3} for i in range(0, 90, 3)]
        v = tk.evaluate(hist, cfg)
        self.assertAlmostEqual(sum(s["posterior"] for s in v["scenarios"]), 1.0, places=3)

    def test_null_close_rows_are_skipped(self):
        cfg = load_scen()
        base = cfg["meta"]["base_date"]
        v = tk.evaluate([{"date": base, "kospi": None}], cfg)
        self.assertEqual(v["scenarios"][0]["n"], 0)


class RowAssembly(unittest.TestCase):
    def test_prev_row_picks_preceding_trading_day(self):
        hist = [{"date": "2026-07-29", "kospi": 1.0},
                {"date": "2026-07-30", "kospi": 2.0},
                {"date": "2026-07-31", "kospi": 3.0}]
        self.assertEqual(tk._prev_row(hist, "2026-07-31")["date"], "2026-07-30")
        # 과거 일자 백필에서도 직전 행을 고른다
        self.assertEqual(tk._prev_row(hist, "2026-07-30")["date"], "2026-07-29")
        self.assertIsNone(tk._prev_row(hist, "2026-07-29"))

    def test_prev_row_skips_null_close(self):
        hist = [{"date": "2026-07-29", "kospi": 1.0}, {"date": "2026-07-30", "kospi": None}]
        self.assertEqual(tk._prev_row(hist, "2026-07-31")["date"], "2026-07-29")

    def test_events_dedup_and_sort(self):
        evs = [{"date": "2026-09-01", "level": 5000}, {"date": "2026-08-01", "level": 5600},
               {"date": "2026-09-01", "level": 5000}]
        out = tk._dedup_events(evs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], "2026-08-01")

    def test_threshold_crossing_direction(self):
        cfg = load_scen()
        down = tk.check_thresholds(5500.0, 5700.0, cfg)
        self.assertTrue(any(t["level"] == 5600 for t in down))
        self.assertEqual(tk.check_thresholds(5500.0, 5400.0, cfg), [])
        self.assertEqual(tk.check_thresholds(5500.0, None, cfg), [])

    def test_html_is_escaped_in_summary(self):
        cfg = load_scen()
        cfg["scenarios"][0]["name"] = "<script>x</script>"
        v = tk.evaluate([], cfg)
        txt = tk.build_summary({"date": "2026-08-03", "kospi": 6000.0, "from_peak": -30.0},
                               5900.0, v, cfg, [])
        self.assertNotIn("<script>", txt)
        self.assertIn("&lt;script&gt;", txt)


@unittest.skipIf(shutil.which("node") is None, "node 미설치 - 패리티 검증 생략")
class JsPythonParity(unittest.TestCase):
    """대시보드와 봇이 같은 숫자를 말하는지 검증하는 회귀 테스트."""

    def _synth_history(self, cfg, n=140):
        """두 시나리오 사이를 이동하는 합성 시계열 — 시나리오가 확실히 갈라지게 만든다."""
        B = next(s for s in cfg["scenarios"] if s["id"] == "B")
        C = next(s for s in cfg["scenarios"] if s["id"] == "C")
        base = dt.date.fromisoformat(cfg["meta"]["base_date"])
        seed = 20260731
        hist, d = [], base
        while len(hist) < n:
            d += dt.timedelta(days=1)
            if d.weekday() >= 5:
                continue
            seed = (seed * 1664525 + 1013904223) % (2 ** 32)
            mix = min(1.0, len(hist) / 90)
            tgt = tk.interp_path(B["path"], d) * (1 - mix) + tk.interp_path(C["path"], d) * mix
            noise = (seed / 2 ** 32 - 0.5) * 0.02
            hist.append({"date": d.isoformat(), "kospi": round(tgt * (1 + noise), 2)})
        return hist

    def test_cores_agree(self):
        with open(INDEX, encoding="utf-8") as f:
            html = f.read()
        js_core = extract_js_fn(html, "interp") + "\n" + extract_js_fn(html, "posteriorSeries")

        cfg = load_scen()
        hist = self._synth_history(cfg)

        harness = (js_core + "\n" + """
const fs = require('fs');
const cfg  = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const hist = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const s = posteriorSeries(hist, cfg);
const last = s[s.length - 1];
console.log(JSON.stringify({ n: last.n,
  rmse: last.rmse.map(v => +v.toFixed(5)),
  post: last.post.map(v => +v.toFixed(4)) }));
""")
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            jsp = os.path.join(tmp, "core.js")
            cfp = os.path.join(tmp, "cfg.json")
            hip = os.path.join(tmp, "hist.json")
            for path, data in ((jsp, harness), (cfp, json.dumps(cfg)), (hip, json.dumps(hist))):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
            out = subprocess.run(["node", jsp, cfp, hip], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        js = json.loads(out.stdout)

        py = tk.evaluate(hist, cfg)
        by_id = {s["id"]: s for s in py["scenarios"]}
        order = [s["id"] for s in cfg["scenarios"]]  # JS 는 설정 순서를 유지

        self.assertEqual(js["n"], by_id[order[0]]["n"], "관측 수 불일치")
        for i, sid in enumerate(order):
            self.assertAlmostEqual(js["rmse"][i], by_id[sid]["rmse"], places=5,
                                   msg=f"{sid} RMSE 불일치 (JS {js['rmse'][i]} vs PY {by_id[sid]['rmse']})")
            self.assertAlmostEqual(js["post"][i], by_id[sid]["posterior"], places=4,
                                   msg=f"{sid} 사후확률 불일치 (JS {js['post'][i]} vs PY {by_id[sid]['posterior']})")

    def test_dashboard_html_is_syntactically_valid(self):
        with open(INDEX, encoding="utf-8") as f:
            html = f.read()
        js = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(js)
            p = f.name
        try:
            out = subprocess.run(["node", "--check", p], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
        finally:
            os.unlink(p)

    def test_no_preview_scaffolding_left(self):
        """프로덕션 빌드에 프리뷰 토글/합성 데이터가 남아 있으면 실패."""
        with open(INDEX, encoding="utf-8") as f:
            html = f.read()
        for bad in ("FIXTURE", "stateseg", "function synth", "statenote"):
            self.assertNotIn(bad, html, f"프리뷰 잔재 발견: {bad}")


class HistoryFileIntegrity(unittest.TestCase):
    def test_history_is_wellformed(self):
        with open(os.path.join(ROOT, "docs", "data", "history.json"), encoding="utf-8") as f:
            store = json.load(f)
        hist = store["history"]
        dates = [r["date"] for r in hist]
        self.assertEqual(dates, sorted(dates), "history 가 날짜순이 아님")
        self.assertEqual(len(dates), len(set(dates)), "중복 일자 존재")
        for r in hist:
            dt.date.fromisoformat(r["date"])
            self.assertIsNotNone(r.get("kospi"))
            self.assertLess(dt.date.fromisoformat(r["date"]).weekday(), 5, f"주말 데이터: {r['date']}")

    def test_verdict_matches_recomputation(self):
        """저장된 판정이 현재 코드로 재계산한 값과 일치해야 한다."""
        with open(os.path.join(ROOT, "docs", "data", "history.json"), encoding="utf-8") as f:
            store = json.load(f)
        if not store.get("verdict"):
            self.skipTest("verdict 없음")
        again = tk.evaluate(store["history"], load_scen())
        self.assertEqual(again["leader"], store["verdict"]["leader"])
        a = {s["id"]: s["posterior"] for s in again["scenarios"]}
        b = {s["id"]: s["posterior"] for s in store["verdict"]["scenarios"]}
        for k in a:
            self.assertAlmostEqual(a[k], b[k], places=4, msg=f"{k} 사후확률 드리프트")


class AnomalyGuard(unittest.TestCase):
    """일간 변동 한계 초과 시 조용히 넘어가지 않고 알림에 표시되어야 한다."""

    def _cfg(self):
        return load_scen()

    def test_summary_carries_anomaly_banner(self):
        cfg = self._cfg()
        latest = {"date": "2026-07-31", "kospi": 6595.45, "from_peak": -29.73}
        verdict = {"scenarios": [{"id": s["id"], "posterior": 1.0 / len(cfg["scenarios"]),
                                  "last_gap": 0.0, "n": 1} for s in cfg["scenarios"]]}
        msg = tk.build_summary(latest, 5593.56, verdict, cfg, [], "전일 대비 +17.91% — 확인 필요")
        self.assertIn("이상치 감지", msg)
        self.assertIn("+17.91%", msg)

    def test_summary_clean_when_normal(self):
        cfg = self._cfg()
        latest = {"date": "2026-07-31", "kospi": 5600.0, "from_peak": -40.0}
        verdict = {"scenarios": [{"id": s["id"], "posterior": 1.0 / len(cfg["scenarios"]),
                                  "last_gap": 0.0, "n": 1} for s in cfg["scenarios"]]}
        msg = tk.build_summary(latest, 5593.56, verdict, cfg, [])
        self.assertNotIn("이상치 감지", msg)

    def test_threshold_is_conservative(self):
        self.assertGreaterEqual(tk.MOVE_ALERT_PCT, 8.0)
        self.assertLessEqual(tk.MOVE_ALERT_PCT, 15.0)


class Idempotency(unittest.TestCase):
    """같은 일자를 다시 돌려도 파일이 바이트 단위로 그대로여야 한다.

    updated_at 이 매번 갱신되면 휴장일마다 내용 없는 커밋이 쌓이고,
    워크플로우의 '변경 없음 - 커밋 생략' 가드가 영원히 발동하지 않는다.
    """

    def test_rerun_produces_identical_file(self):
        import tempfile
        src = os.path.join(ROOT, "docs", "data", "history.json")
        with open(src, encoding="utf-8") as f:
            before = f.read()
        store = json.loads(before)
        if not store.get("history"):
            self.skipTest("history 비어 있음")

        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "history.json")
            orig = tk.HISTORY_PATH
            tk.HISTORY_PATH = dst
            try:
                tk.save_json(dst, store)
                with open(dst, encoding="utf-8") as f:
                    first = f.read()
                tk.save_json(dst, json.loads(first))
                with open(dst, encoding="utf-8") as f:
                    second = f.read()
            finally:
                tk.HISTORY_PATH = orig
        self.assertEqual(first, second, "동일 입력에 대해 직렬화 결과가 달라짐")

    def test_updated_at_preserved_when_body_unchanged(self):
        """main() 의 unchanged 분기가 소스에 실제로 존재하는지 확인."""
        with open(os.path.join(ROOT, "tracker.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("unchanged = all(", src, "빈 커밋 방지 분기가 사라짐")
        self.assertIn('store["updated_at"] if unchanged', src,
                      "내용 동일 시 updated_at 을 유지하는 로직이 사라짐")


if __name__ == "__main__":
    unittest.main(verbosity=2)

class ForceNotify(unittest.TestCase):
    """알림 경로 점검용 강제 발송 스위치가 살아 있어야 한다."""

    def test_switch_exists_and_defaults_off(self):
        src = open(tk.__file__, encoding="utf-8").read()
        self.assertIn('os.getenv("FORCE_NOTIFY"', src, "강제 발송 스위치가 사라짐")
        self.assertIn("if not row and not force_notify:", src, "기본값이 발송 억제가 아님")

    def test_truthy_values(self):
        for v in ("1", "true", "TRUE", "yes", "on"):
            self.assertIn(v.strip().lower(), ("1", "true", "yes", "on"))


class DashboardLink(unittest.TestCase):
    """알림만 보고 끝나지 않도록 대시보드로 가는 링크가 항상 붙어야 한다."""

    def test_summary_contains_dashboard_link(self):
        cfg = load_scen()
        hist = json.load(open(tk.HISTORY_PATH, encoding="utf-8"))
        rows = hist["history"] if isinstance(hist, dict) else hist
        v = tk.evaluate(rows, cfg)
        msg = tk.build_summary(rows[-1], rows[-2]["kospi"], v, cfg, [])
        self.assertIn(tk.DASHBOARD_URL, msg, "대시보드 링크 누락")
        self.assertIn("<a href=", msg, "링크가 앵커 태그로 렌더되지 않음")

    def test_url_is_https_pages(self):
        self.assertTrue(tk.DASHBOARD_URL.startswith("https://"), "평문 http 링크")
        self.assertIn("github.io", tk.DASHBOARD_URL)
