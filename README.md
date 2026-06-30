# claude-token-report

로컬 **Claude Code** 대화 로그(`~/.claude/projects/**/*.jsonl`)를 스캔해
최근 N개월의 **토큰 사용량**을 집계하고, 보기 좋은 **단일 HTML 리포트**를 생성하는 작은 파이썬 도구입니다.

- 🗂 일별 / 월별 사용량 추이
- 🧩 토큰 구성 (입력 · 출력 · 캐시 쓰기 · 캐시 읽기)
- 🤖 모델별 토큰 & 추정 비용 (Opus / Sonnet / Haiku / Fable)
- 📁 프로젝트별 Top 15
- 📄 의존성 없는 단일 HTML (차트는 Chart.js CDN 사용)

> 예시 리포트: [`examples/sample-report.html`](examples/sample-report.html) (더미 데이터)

---

## ⚠️ 개인정보 주의

이 저장소는 **도구(스크립트·템플릿)만** 공개합니다.
스크립트가 만들어내는 **집계 결과(`data.json`)와 생성된 리포트 HTML에는
본인의 프로젝트명 등 개인 정보가 포함될 수 있습니다.**

- 생성물은 `.gitignore`로 기본 차단됩니다. (`data.json`, `claude-token-report-*.html` 등)
- 리포트를 공유하기 전에 내용을 반드시 직접 확인하세요.
- 리포트는 **본인 PC의 로컬 로그**만 반영합니다. claude.ai 웹/앱, 다른 기기,
  직접 API 호출, 실제 청구액은 포함되지 않습니다. 정확한 청구 정보는
  [Anthropic Console](https://console.anthropic.com)에서 확인하세요.

---

## 요구사항

- Python 3.10+
- Claude Code를 사용한 적이 있어 `~/.claude/projects/` 에 로그가 존재할 것
- 리포트의 차트를 보려면 인터넷 연결 (Chart.js CDN)

## 사용법

### 🤖 Claude Code로 한 번에 (클론 + 단가 최신화 + 실행)

[Claude Code](https://claude.com/claude-code)를 쓴다면, 아래 프롬프트를 **그대로 복사해 붙여넣기**만 하면
저장소 클론 → 모델 단가 최신화 → 리포트 생성 → 브라우저 열기까지 자동으로 처리됩니다.

```text
claude-token-report로 내 Claude Code 토큰 사용량 리포트를 만들어줘. 순서대로 진행해:

1. https://github.com/okdohyuk/claude-token-report 를 ~/claude-token-report 에 클론해줘.
   이미 디렉토리가 있으면 새로 클론하지 말고 git pull 로 최신화만 해줘.
2. Anthropic 공식 가격 페이지(https://www.anthropic.com/pricing#api)에서 현재 Claude 모델
   단가를 확인해서 generate_report.py 의 PRICING 딕셔너리를 최신 값으로 업데이트해줘.
   - 단위: USD per 1M tokens
   - 각 모델(opus / sonnet / haiku / fable)의 키: in(입력) / out(출력) / cw(캐시 쓰기) / cr(캐시 읽기)
   - 가격을 확인하지 못한 항목은 기존 값을 유지하고, 무엇을 어떻게 바꿨는지 표로 알려줘.
3. python generate_report.py --months 3 를 실행하고, 생성된 리포트 HTML(claude-token-report-<날짜>.html)을 열어줘.

주의: data.json 과 생성된 *.html 같은 산출물에는 내 프로젝트명 등 개인정보가 들어 있으니
절대 커밋하지 마. (PRICING 수정 자체는 코드 변경이라 커밋해도 괜찮아.)
```

> 단가는 수시로 바뀌므로, 정확한 비용을 보려면 실행 전에 이 프롬프트로 `PRICING`을 한 번 갱신하는 것을 권장합니다.

### 수동 실행

```bash
# 1) 저장소 클론
git clone https://github.com/okdohyuk/claude-token-report.git
cd claude-token-report

# 2) 실행 (최근 3개월, ./claude-token-report-<오늘날짜>.html 생성)
python generate_report.py
# 출력 파일명에 실행 날짜가 붙고, 같은 날 다시 실행하면 -2, -3 순번이 붙어 이전 리포트를 덮어쓰지 않습니다.

# 3) 브라우저로 열기 (아래는 2026-06-30 에 생성한 예시)
open claude-token-report-2026-06-30.html        # macOS
# xdg-open claude-token-report-2026-06-30.html  # Linux
# start claude-token-report-2026-06-30.html     # Windows
```

### 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--months N` | `3` | 집계할 최근 개월 수 |
| `--projects-dir PATH` | `~/.claude/projects` | Claude Code 로그 디렉토리 |
| `-o, --output PATH` | `claude-token-report-<날짜>.html` | 출력 HTML 경로. 미지정 시 실행 날짜가 붙고 같은 날 재실행하면 `-2`, `-3` 순번으로 중복 방지 |
| `--data PATH` | (없음) | 집계 JSON도 함께 저장 |
| `--template PATH` | `template.html` | HTML 템플릿 경로 |

```bash
# 예: 최근 6개월, 파일명 지정
python generate_report.py --months 6 -o my-report.html

# 집계 원본 데이터도 같이 저장
python generate_report.py --data data.json
```

---

## 동작 방식

1. `~/.claude/projects/**/*.jsonl` 의 모든 세션 로그를 읽습니다.
2. `type: assistant` 메시지의 `usage` 필드(input / output / cache_creation / cache_read)를 합산합니다.
3. **`message.id` + `requestId`** 기준으로 중복 메시지를 제거합니다.
4. 프로젝트명은 로그의 `cwd` **basename**만 사용해 사용자 홈 경로(사용자명)를 노출하지 않습니다. 경로 구분자(`/`·`\`)를 모두 처리하므로 Windows·Linux·macOS 어디서 기록된 로그든 동일하게 동작합니다.
5. `template.html` 에 데이터를 주입해 단일 HTML로 출력합니다.

### 비용 추정에 대해

모델별 비용은 `generate_report.py`의 `PRICING` 표(공개 API 단가 **추정치**)로 계산합니다.
구독 플랜·캐시 할인·실제 청구액과 다를 수 있으므로 **참고용**입니다. 단가는 직접 수정할 수 있습니다.

```python
PRICING = {
    "opus":   {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50},
    "sonnet": {"in": 3.0,  "out": 15.0, "cw": 3.75,  "cr": 0.30},
    ...
}
```

## 파일 구성

```
claude-token-report/
├── generate_report.py   # 집계 + HTML 생성 CLI
├── template.html        # 리포트 HTML 템플릿 (__DATA__ 주입)
├── examples/
│   ├── sample-data.json   # 더미 데이터
│   └── sample-report.html # 더미로 생성한 예시 리포트
├── .gitignore
├── LICENSE
└── README.md
```

## 라이선스

MIT © okdohyuk
