# DASH-pages

DASH 앱의 홈페이지·기능 소개·개인정보처리방침·서비스 약관, 그리고 앱이 내려받는 D-Day 데이터. GitHub Pages로 배포됩니다.

https://dash-worklife.org/

## 구조

- 루트 — 한국어 페이지: `index.html`, `features/dashboard.html`, `features/dday.html`, `privacy.html`, `terms.html`, `delete-data.html`
- `en/` — 같은 구조의 영어 페이지. 각 페이지는 `<link rel="alternate" hreflang>`로 상대 언어 페이지를 가리키고,
  헤더의 EN / 한국어 버튼도 같은 페이지의 다른 언어판으로 이동합니다.
- `style.css`, `nav.js` — 공용 스타일과 헤더 내비게이션(드롭다운 + 폰 드로어). 스타일을 바꾸면 모든 페이지의
  `style.css?v=YYYYMMDD` 캐시 버스터를 함께 올립니다.
- `data/v1/` — 앱의 D-Day 사이드바가 내려받는 국가별 휴일·이벤트 목록(JSON). `tools/dday/`의 생성기와
  `.github/workflows/dday-feed.yml`이 관리하며, 손으로 고치지 않습니다.

## 페이지를 고칠 때

- 헤더의 "기능" 드롭다운은 페이지마다 복사되어 있습니다. 기능 페이지를 추가하면 루트와 `en/`의 **모든** 페이지에
  링크를 넣고, 두 `index.html`의 기능 그리드에도 카드를 추가합니다.
- 개인정보처리방침은 한국어판이 기준이며, 영어판은 번역입니다. 두 판의 개정일을 같이 올립니다.
- `main`이 배포되는 브랜치입니다.
