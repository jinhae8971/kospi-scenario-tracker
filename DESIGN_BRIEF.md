# 대시보드 리디자인 핸드오프 — Claude Code용 브리프

이 문서 하나만 읽고도 작업이 가능하도록 작성되었습니다. 대상 파일은 **`docs/index.html` 단 하나**입니다.

```bash
git clone https://github.com/jinhae8971/kospi-scenario-tracker.git
cd kospi-scenario-tracker
# 로컬 확인 (fetch가 file:// 에서 막히므로 반드시 HTTP 서버로)
python3 -m http.server 8000 --directory docs
# → http://localhost:8000
```

라이브: https://jinhae8971.github.io/kospi-scenario-tracker/

---

## 1. 제품 맥락

2026년 6월 코스피 고점(9,385.59) 이후 진행 중인 급락장을, 역사적 유사사례에서 도출한 **3개 시나리오 경로**와 매 영업일 종가로 대조해 "지금 시장이 어느 시나리오를 따라가는가"를 확률로 갱신하는 개인 리서치 대시보드입니다.

읽는 사람은 **작성자 본인 1명**, 사용 상황은 **평일 장 마감 후 모바일에서 30초 확인 → 주말에 데스크톱에서 정독**. 따라서 최상단에서 "오늘 뭐가 달라졌나"가 3초 안에 읽혀야 하고, 아래로 갈수록 근거를 파고드는 구조여야 합니다.

시나리오 정의:

| ID | 이름 | 색상 | 사전확률 | 준거 사례 |
|----|------|------|---------|-----------|
| A | V자 정상화 | `#1D9E75` | 25% | 코스피 2020 / 2024.8 엔캐리 청산 |
| B | W자 재시험 후 박스권 | `#378ADD` | 45% | 상하이 2015~2016 |
| C | 구조적 베어마켓 | `#E24B4A` | 30% | 대만 1990 / 코스닥 2000 |

색상은 `scenarios.json`에서 주입됩니다. **하드코딩하지 말고 반드시 데이터에서 읽으세요.**

---

## 2. 기술 제약 (협상 불가)

| 제약 | 내용 |
|------|------|
| 빌드 | **없음.** GitHub Pages 정적 호스팅. npm/webpack/Vite 도입 불가 |
| 파일 | `docs/index.html` 단일 파일. CSS/JS 분리 파일 생성 금지 |
| 의존성 | CDN만 허용. 현재 Chart.js 4.4.1 (`cdn.jsdelivr.net`). 교체는 가능하나 CDN 단일 스크립트여야 함 |
| 프레임워크 | React/Vue 불가 (빌드 필요). Alpine.js 등 CDN 단일 파일 라이브러리는 허용 |
| 스토리지 | `localStorage`/`sessionStorage` 사용 금지 |
| 언어 | 한국어. `lang="ko"` 유지 |
| 폰트 | 시스템 폰트 스택 유지 권장. 웹폰트 추가 시 초기 로딩 blocking 없어야 함 |

---

## 3. 데이터 계약 ★ 가장 중요

두 개의 정적 JSON을 `fetch`로 읽습니다. **스키마를 바꾸지 마세요** — `tracker.py`가 매 평일 이 형식으로 덮어씁니다.

### `docs/data/history.json`

```json
{
  "updated_at": "2026-08-01T11:40:41+09:00",
  "history": [
    {
      "date": "2026-07-31",
      "kospi": 6595.45,
      "samsung": 262500.0,
      "hynix": 1718000.0,
      "foreign_net": 7241000000000,
      "foreign_cum": 7241000000000,
      "from_peak": -29.73,
      "chip_pair": 434300.0,
      "volume_value": null,
      "source": "naver"
    }
  ],
  "verdict": {
    "leader": "B",
    "scenarios": [
      { "id": "B", "name": "W자 재시험 후 박스권", "color": "#378ADD",
        "prior": 0.45, "posterior": 0.45, "rmse": 0.00007, "last_gap": 0.00007, "n": 1 }
    ]
  },
  "events": [
    { "date": "2026-09-12", "level": 5600, "label": "5,600 하향 이탈 — W자 2차 저점 진입", "signal": "B" }
  ]
}
```

**필드 주의사항 — 이걸 놓치면 화면이 깨집니다:**

- `samsung` / `hynix` / `foreign_net` / `volume_value`는 **`null`일 수 있습니다.** 백필 실행이나 폴백 소스(yfinance) 사용 시 비어 있습니다. 모든 렌더링에 null 가드 필요
- `foreign_net`, `foreign_cum` 단위는 **원**. 화면에는 억(`/1e8`) 또는 조(`/1e12`)로 환산해 표시
- `from_peak`는 이미 **퍼센트 값** (−29.73 = −29.73%). 다시 100을 곱하지 마세요
- `rmse`, `last_gap`, `prior`, `posterior`는 **비율** (0.45 = 45%)
- `verdict.scenarios`는 사후확률 **내림차순 정렬**되어 있음. `[0]`이 선두
- `history`는 날짜 오름차순. 첫 로드 시 3~5행뿐일 수 있고 1년 뒤엔 250행이 넘습니다. **양 극단 모두 보기 좋아야 합니다**
- `events`는 대부분의 날에 **빈 배열**입니다. 빈 상태 디자인이 필요합니다

### `docs/scenarios.json`

```json
{
  "meta": {
    "peak": { "date": "2026-06-19", "value": 9385.59, "label": "고점(장중)" },
    "base_date": "2026-07-31", "base_value": 6595.45, "tau": 0.055,
    "note": "…면책 문구…"
  },
  "thresholds": [
    { "level": 7500, "dir": "up", "signal": "A", "label": "7,500 상향 돌파 — V자 정상화 유효" }
  ],
  "scenarios": [
    { "id": "A", "name": "V자 정상화", "prior": 0.25, "color": "#1D9E75",
      "reference": "코스피 2020 코로나 / 2024년 8월 엔캐리 청산",
      "thesis": "메모리 계약가와 하이퍼스케일러 capex 유지…",
      "path": [["2026-07-31", 6595], ["2026-08-31", 6100], ["2027-08-31", 9600]] }
  ]
}
```

`thesis` 필드는 **현재 화면에서 쓰이지 않습니다.** 시나리오 카드에 접기/펼치기로 노출하면 좋은 후보입니다.

---

## 4. 절대 건드리지 말 것 — 계산 로직

`index.html` 안의 아래 두 함수는 `tracker.py`의 Python 구현을 1:1 미러링한 것입니다. 값이 어긋나면 화면과 텔레그램 알림이 서로 다른 확률을 말하게 됩니다.

```js
function interp(path, target)          // 앵커 포인트 간 일자 기준 선형보간
function posteriorSeries(history, cfg) // 일자별 누적 RMSE → prior × exp(−rmse/τ) → 정규화
```

수식:

```
오차_t    = (실제종가_t − 기대값_t) / 기대값_t
RMSE_i    = sqrt( mean( 오차_t² ) )          # base_date 이후 전 관측치
사후확률_i ∝ prior_i × exp( −RMSE_i / τ )    # τ = 0.055
```

**시각적 표현은 자유롭게 바꾸되, 이 두 함수의 입출력은 그대로 두세요.**

---

## 5. 화면 구성요소 인벤토리

현재 7개 블록입니다. 순서·형태·병합 모두 재설계 대상입니다.

| # | 블록 | DOM ID | 데이터 | 현재 형태 | 리디자인 관점 |
|---|------|--------|--------|-----------|--------------|
| 0 | 헤더 + 타임스탬프 | `stamp` | `updated_at`, 행 수 | 텍스트 1줄 | "신선도"가 잘 안 보임. 오래된 데이터 경고 필요 |
| 1 | 지표 카드 5개 | `metrics` | 최근 종가 / 고점 대비 / 추종 시나리오 / 경로 이탈도 / 외국인 누적 | 균등 그리드 | **위계가 평평함.** 5개가 같은 크기라 뭘 먼저 봐야 할지 모름 |
| 2 | 시나리오 카드 3개 | `scen` | `verdict.scenarios` + `reference` | 좌측 컬러바 + 확률 + 진행바 | 사전→사후 **변화량**이 안 드러남 |
| 3 | 경로 차트 | `c1` | 실제 종가 + 3개 시나리오 보간 경로 | 라인 4개 (실제=굵은 검정, 시나리오=색상 점선) | **핵심 차트.** 현재 관측 3점 vs 시나리오 13개월 → 실제선이 왼쪽 끝에 뭉개짐 |
| 4 | 사후확률 추이 | `c2` | `posteriorSeries()` | 면적 라인 3개, y 0~100% | 관측 1일이면 직선 하나. 초기 빈약함 처리 필요 |
| 5 | 보조 지표 | `c3` | 삼성전자 / SK하이닉스÷10 / 외국인 누적 | 혼합(라인2+막대1), 이중 y축 | ÷10 스케일 트릭이 **오독을 유발**. 정규화·소형 다중 차트 등 대안 검토 |
| 6 | 임계선 이벤트 | `events` | `events[]` | 카드 목록 / 빈 상태 문구 | 발생 시 가장 중요한 정보인데 **아래로 밀려 있음** |
| 7 | 일별 기록 | `tbl` | `history` 최근 40행 | 7열 테이블 | 모바일에서 가로 스크롤 발생 |
| 8 | 면책 | `disclaimer` | `meta.note` | 회색 박스 | 유지 |

---

## 6. 현재 디자인 토큰

```css
:root{
  --bg:#faf9f7; --card:#fff; --ink:#1f1e1c; --mute:#6b6a66;
  --line:rgba(0,0,0,.12);
  --red:#A32D2D; --green:#0F6E56; --blue:#185FA5;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#171614; --card:#211f1d; --ink:#eceae5; --mute:#9c9a94;
    --line:rgba(255,255,255,.14);
    --red:#F09595; --green:#5DCAA5; --blue:#85B7EB;
  }
}
```

컨테이너 `max-width:1040px`, 카드 `border-radius:12px`, 본문 16px / 행간 1.7.
**다크모드는 `prefers-color-scheme` 자동 전환이며 반드시 유지해야 합니다** (야간 확인 용도).

Chart.js 다크 대응은 부트 시점에 `matchMedia`로 한 번 읽어 `Chart.defaults.color`에 주입하는 방식입니다. 리디자인 시 **런타임 테마 토글을 추가한다면 차트 재생성 로직도 함께** 넣어야 합니다.

---

## 7. 해결해야 할 문제 (우선순위 순)

1. **시간축 압축** — 실제 관측(3일~수개월)과 시나리오 경로(13개월)를 한 축에 그리면 실제선이 왼쪽 끝에 뭉칩니다. 확대 뷰/브러시/분리 축 등 해법이 필요합니다.
2. **정보 위계 부재** — 지표 카드 5개가 동등한 무게입니다. "추종 시나리오"와 "최근 종가"가 지배적이어야 합니다.
3. **변화량 미표시** — 어제 대비 사후확률이 몇 %p 움직였는지가 이 대시보드의 존재 이유인데 어디에도 없습니다.
4. **빈/희소 상태** — 초기 3행, 이벤트 0건 상태가 "고장난 것처럼" 보입니다.
5. **모바일** — 테이블 가로 스크롤, 차트 높이 330px 고정, 카드 5개 세로 적층.
6. **÷10 트릭** — SK하이닉스를 10으로 나눠 같은 축에 올린 것은 정직하지 않습니다.
7. **접근성** — 시나리오 구분이 **색상에만** 의존합니다. 색각 이상 사용자를 위해 패턴/라벨 병행 필요. 대비비 WCAG AA 확인.

---

## 8. 수용 기준

- [ ] `docs/index.html` 단일 파일, 빌드 스텝 없음
- [ ] `history.json` / `scenarios.json` 스키마 변경 없음
- [ ] `interp()` / `posteriorSeries()` 산출값이 기존과 동일 (숫자 검증)
- [ ] 모든 null 가능 필드에 가드 존재 — `samsung`, `hynix`, `foreign_net`, `volume_value`가 전부 `null`인 행을 넣어도 렌더링 성공
- [ ] `history` 3행 / 250행 양쪽에서 레이아웃 정상
- [ ] `events` 빈 배열에서 의미 있는 빈 상태 표시
- [ ] 라이트/다크 모드 모두 정상
- [ ] 375px 폭에서 가로 스크롤 없음
- [ ] 색상 없이 흑백 인쇄해도 3개 시나리오 구분 가능
- [ ] `fetch` 실패 시 에러 메시지 표시 (현재 동작 유지)
- [ ] `python3 -m http.server` 로컬 확인 + push 후 Pages 실 URL 확인

---

## 9. 테스트 픽스처

극단 케이스 검증용. 로컬에서 `docs/data/history.json`을 임시 교체해 확인하세요. **커밋하지 마세요** — 다음 워크플로우 실행 때 덮어써지지만 그 전에 라이브에 노출됩니다.

```json
{
  "updated_at": "2026-11-20T17:02:00+09:00",
  "history": [
    { "date": "2026-07-31", "kospi": 6595.45, "samsung": 262500, "hynix": 1718000,
      "foreign_net": 7241000000000, "foreign_cum": 7241000000000, "from_peak": -29.73, "source": "naver" },
    { "date": "2026-09-15", "kospi": 5210.00, "samsung": null, "hynix": null,
      "foreign_net": null, "foreign_cum": 7241000000000, "from_peak": -44.49, "source": "yfinance" },
    { "date": "2026-11-20", "kospi": 4180.33, "samsung": 141000, "hynix": 902000,
      "foreign_net": -3120000000000, "foreign_cum": 1810000000000, "from_peak": -55.46, "source": "naver" }
  ],
  "verdict": {
    "leader": "C",
    "scenarios": [
      { "id": "C", "name": "구조적 베어마켓", "color": "#E24B4A", "prior": 0.30, "posterior": 0.7412, "rmse": 0.0121, "last_gap": -0.0043, "n": 3 },
      { "id": "B", "name": "W자 재시험 후 박스권", "color": "#378ADD", "prior": 0.45, "posterior": 0.2301, "rmse": 0.0729, "last_gap": -0.3468, "n": 3 },
      { "id": "A", "name": "V자 정상화", "color": "#1D9E75", "prior": 0.25, "posterior": 0.0287, "rmse": 0.1904, "last_gap": -0.4641, "n": 3 }
    ]
  },
  "events": [
    { "date": "2026-09-15", "level": 5600, "label": "5,600 하향 이탈 — W자 2차 저점 진입", "signal": "B" },
    { "date": "2026-10-02", "level": 5000, "label": "5,000 하향 이탈 — 구조적 베어마켓 경고", "signal": "C" },
    { "date": "2026-11-20", "level": 4200, "label": "4,200 하향 이탈 — 고점 대비 -55% 구간", "signal": "C" }
  ]
}
```

이 픽스처는 `foreign_net` 음수, 전 필드 `null`인 행, 이벤트 3건, 사후확률 극단 쏠림(74% vs 3%)을 동시에 담고 있습니다.

---

## 10. 배포

`main` 브랜치 push → Pages 자동 재배포 (약 1분). 별도 워크플로우 트리거 불필요.

`docs/scenarios.json`은 루트 `scenarios.json`의 복사본이며 **평일 워크플로우가 자동 동기화**합니다. 시나리오 내용을 고칠 일이 있으면 루트 파일을 고치세요. 디자인 작업 중에는 둘 다 건드릴 필요가 없습니다.
