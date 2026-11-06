#!/usr/bin/env python3
"""
Claude Code 토큰 사용량 리포트 생성기.

로컬 Claude Code 대화 로그(~/.claude/projects/**/*.jsonl)를 스캔해
최근 N개월의 토큰 사용량을 집계하고, 단일 HTML 리포트를 생성한다.

집계 결과(data.json)와 생성된 HTML에는 사용자의 프로젝트명 등
개인 정보가 포함될 수 있으므로 저장소에 커밋하지 말 것 (.gitignore 참고).

사용 예:
    python generate_report.py                 # 최근 3개월, ./claude-token-report-YYYY-MM-DD.html
    python generate_report.py --months 6
    python generate_report.py -o report.html --data data.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# 출력 파일명에 -o 미지정 시 사용하는 기본 스템 (확장자 제외)
DEFAULT_OUTPUT_STEM = "claude-token-report"

# ---------------------------------------------------------------------------
# 모델별 추정 단가 (USD / 1M tokens) — 공개 API 가격 기준의 "추정치".
# 실제 청구액·구독 플랜과 다를 수 있으며 참고용이다.
#   in = 입력, out = 출력, cw = 캐시 쓰기, cr = 캐시 읽기
# ---------------------------------------------------------------------------
PRICING = {
    "opus":   {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50},
    "sonnet": {"in": 3.0,  "out": 15.0, "cw": 3.75,  "cr": 0.30},
    "haiku":  {"in": 1.0,  "out": 5.0,  "cw": 1.25,  "cr": 0.10},
    "fable":  {"in": 3.0,  "out": 15.0, "cw": 3.75,  "cr": 0.30},
}
# 가족을 판별하지 못한 모델에 적용할 기본 단가
DEFAULT_PRICING = PRICING["sonnet"]

# 게이트웨이(CLIProxyAPI 등)를 거치면 Claude가 아닌 모델도 같은 로그에 남는다.
# Anthropic 단가를 적용하면 비용이 왜곡되므로 단가 없이 토큰만 집계한다.
# Kiro는 Claude 모델명을 쓰지만 별도 구독이라 Claude 가족보다 먼저 판별한다.
UNPRICED_FAMILIES = {
    "kiro":   ("kiro ",),
    "devin":  ("devin/",),
    "gemini": ("gemini",),
    "gpt":    ("gpt-", "codex"),
}

TOKEN_KEYS = ("in", "out", "cw", "cr")


def model_family(model: str | None) -> str:
    if not model:
        return "other"
    m = model.lower()
    for fam, needles in UNPRICED_FAMILIES.items():
        if any(m.startswith(n) if n.endswith(("/", " ")) else n in m for n in needles):
            return fam
    for fam in ("opus", "sonnet", "haiku", "fable"):
        if fam in m:
            return fam
    return "other"


def recent_months(n: int, now: datetime) -> list[str]:
    """now 기준 최근 n개월의 'YYYY-MM' 목록(과거->현재 순)."""
    months: list[str] = []
    y, mo = now.year, now.month
    for _ in range(n):
        months.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            mo = 12
            y -= 1
    return sorted(months)


def basename_any(path: str) -> str:
    """'/' 와 '\\' 를 모두 구분자로 보고 마지막 세그먼트를 반환.

    os.path.basename 은 실행 중인 OS 의 구분자만 인식하므로, 윈도우에서
    macOS/Linux 로그('/')를 읽거나 그 반대인 경우 경로를 분해하지 못한다.
    이 헬퍼는 어느 OS 에서 기록된 cwd 든 마지막 폴더명을 안전하게 뽑는다.
    """
    seg = re.split(r"[\\/]+", path.rstrip("\\/"))
    return seg[-1] if seg else ""


def clean_project(cwd: str | None, encoded_dir: str) -> str:
    """프로젝트 표시명을 사용자명 노출 없이 추출.

    1순위: 로그의 cwd 필드 basename (예: /Users/<u>/Documents/foo -> foo)
           단, cwd가 홈 디렉토리 자체이면 basename이 사용자명이 되므로 "(home)" 표시.
    2순위: 인코딩된 디렉토리명의 마지막 세그먼트
    """
    home = os.path.expanduser("~")
    username = basename_any(home)
    # 구분자 차이('/' vs '\\')를 무시하고 홈 경로와 비교하기 위한 정규화 키
    home_key = home.rstrip("\\/").replace("\\", "/")
    name = None
    if cwd:
        if cwd.rstrip("\\/").replace("\\", "/") == home_key:   # 홈에서 실행한 세션
            return "(home)"
        base = basename_any(cwd)
        if base:
            name = base
    if name is None:
        # 인코딩된 디렉토리명: 슬래시가 '-'로 치환되어 있다. 마지막 토큰 사용.
        tokens = [t for t in encoded_dir.split("-") if t]
        name = tokens[-1] if tokens else encoded_dir
    # 어떤 경로로 왔든 결과가 사용자명과 같으면 노출 방지
    return "(home)" if name == username else name


def months_between(start: datetime, end: datetime) -> list[str]:
    """start~end 사이에 걸친 'YYYY-MM' 목록(과거->현재 순)."""
    months: list[str] = []
    y, mo = start.year, start.month
    while (y, mo) <= (end.year, end.month):
        months.append(f"{y:04d}-{mo:02d}")
        mo += 1
        if mo == 13:
            mo, y = 1, y + 1
    return months


def aggregate(root: str, months: set[str], since_iso: str | None = None) -> dict:
    """since_iso(UTC ISO 문자열)가 있으면 그 시각 이후 메시지만 집계한다(--days)."""
    daily = defaultdict(lambda: defaultdict(int))
    monthly = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    by_project = defaultdict(lambda: defaultdict(int))
    project_name: dict[str, str] = {}
    project_sessions = defaultdict(set)

    seen: set[tuple] = set()           # (msg_id, requestId) 중복 제거
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    processed = parse_errors = 0

    for path in files:
        rel = os.path.relpath(path, root)
        encoded_dir = rel.split(os.sep)[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if '"usage"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        parse_errors += 1
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message") or {}
                    usage = msg.get("usage") or {}
                    ts = d.get("timestamp")
                    if not usage or not ts or len(ts) < 7:
                        continue
                    month = ts[:7]
                    if month not in months:
                        continue
                    if since_iso and ts < since_iso:
                        continue
                    date = ts[:10]

                    mid, rid = msg.get("id"), d.get("requestId")
                    if mid:
                        key = (mid, rid)
                        if key in seen:
                            continue
                        seen.add(key)

                    rec = {
                        "in":  usage.get("input_tokens", 0) or 0,
                        "out": usage.get("output_tokens", 0) or 0,
                        "cw":  usage.get("cache_creation_input_tokens", 0) or 0,
                        "cr":  usage.get("cache_read_input_tokens", 0) or 0,
                    }
                    fam = model_family(msg.get("model"))
                    pname = clean_project(d.get("cwd"), encoded_dir)
                    project_name[encoded_dir] = pname

                    for bucket in (daily[date], monthly[month], by_model[fam],
                                   by_project[encoded_dir]):
                        for k in TOKEN_KEYS:
                            bucket[k] += rec[k]
                        bucket["total"] += sum(rec.values())
                        bucket["msgs"] += 1
                    sid = d.get("sessionId")
                    if sid:
                        project_sessions[encoded_dir].add(sid)
            processed += 1
        except Exception:
            continue

    # 합계 및 비용
    totals = defaultdict(int)
    for fam, v in by_model.items():
        if fam in UNPRICED_FAMILIES:
            v["cost"] = 0.0
            v["unpriced"] = 1
            continue
        p = PRICING.get(fam, DEFAULT_PRICING)
        v["cost"] = round(
            (v["in"] * p["in"] + v["out"] * p["out"]
             + v["cw"] * p["cw"] + v["cr"] * p["cr"]) / 1_000_000, 2)
    for v in by_project.values():
        for k in (*TOKEN_KEYS, "total", "msgs"):
            totals[k] += v[k]
    total_cost = round(sum(v["cost"] for v in by_model.values()), 2)

    projects = []
    for enc, v in by_project.items():
        if v["total"] <= 0:
            continue
        projects.append({
            "name": project_name.get(enc, enc),
            "total": v["total"], "msgs": v["msgs"],
            "in": v["in"], "out": v["out"], "cw": v["cw"], "cr": v["cr"],
            "sessions": len(project_sessions.get(enc, ())),
        })
    projects.sort(key=lambda x: -x["total"])

    return {
        "totals": dict(totals),
        "total_cost": total_cost,
        "daily": {k: dict(v) for k, v in sorted(daily.items())},
        "monthly": {k: dict(v) for k, v in sorted(monthly.items())},
        "by_model": {k: dict(v) for k, v in by_model.items()},
        "projects": projects,
        "meta": {
            "files_total": len(files),
            "files_processed": processed,
            "parse_errors": parse_errors,
            "unique_messages": len(seen),
            "months_requested": sorted(months),
        },
    }


# ---------------------------------------------------------------------------
# CLIProxyAPI 게이트웨이 로그 집계
#
# 게이트웨이를 쓰면 다른 PC·다른 클라이언트(Codex, OpenCode 등)의 요청은 이 PC의
# ~/.claude/projects 에 남지 않는다. 게이트웨이 로그에는 토큰 수가 없지만, 같은
# 요청 ID로 남는 두 줄을 이어 요청 단위 사용 현황을 만들 수 있다.
#   selector 줄: [시각] [요청ID] ... auth=<계정> provider=<p> model=<모델>
#   gin 줄    : [시각] [요청ID] ... <상태> | <소요> | <클라이언트IP> | POST "<경로>"
# ---------------------------------------------------------------------------
GW_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] \[([0-9a-f]{8})\]")
GW_SELECT = re.compile(r"auth=(\S+) provider=\S+ model=(.+?)\s*$")
GW_GIN = re.compile(r'gin_logger\.go:\d+\]\s+(\d{3}) \|\s*(\S+) \|\s*(\S+) \| (\w+)\s+"([^"]+)"')
# 추론 요청만 센다. count_tokens·models 조회·관리 API는 사용량이 아니다.
GW_INFER_PATH = re.compile(
    r"^/(v1|v1beta)/(messages|chat/completions|responses|models/[^?]*:(generate|stream)\w*)(\?|$)")
EMAIL = re.compile(r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|µs|us|ns|h|m|s)")
DURATION_SEC = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}
TOP_N = 15


def mask_local(local: str) -> str:
    """이메일 로컬파트를 앞 2자·뒤 2자만 남기고 가린다 (짧으면 앞 1자만)."""
    if len(local) <= 4:
        return local[:1] + "***"
    return f"{local[:2]}***{local[-2:]}"


def account_label(auth: str) -> str:
    """게이트웨이 auth 식별자를 '<provider> · <마스킹 이메일>' 형태로 바꾼다."""
    if auth.startswith("openai-compatibility:"):
        parts = auth.split(":")
        return parts[1] if len(parts) > 1 else "openai-compatibility"
    provider = re.split(r"[-:]", auth, maxsplit=1)[0]
    # 파일명은 '<provider>-[<hex id>-]<email>[-<plan>].json' 이라 접두사를 떼야 이메일만 남는다.
    rest = re.sub(rf"^{re.escape(provider)}-([0-9a-f]{{6,}}-)?", "", auth.replace(".json", ""))
    m = EMAIL.search(rest)
    if not m:
        return provider
    domain = re.sub(r"-[a-z]+$", "", m.group(2))   # codex 파일명의 '-plus' 같은 꼬리 제거
    return f"{provider} · {mask_local(m.group(1))}@{domain}"


def duration_seconds(text: str) -> float | None:
    parts = DURATION_PART.findall(text)
    if not parts:
        return None
    return sum(float(v) * DURATION_SEC[u] for v, u in parts)


def aggregate_gateway(sources: list[tuple[str, str]], since_local: datetime | None,
                      months: set[str], client_names: dict[str, str]) -> dict:
    daily = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    by_account = defaultdict(lambda: defaultdict(int))
    by_client = defaultdict(lambda: defaultdict(int))
    by_gateway = defaultdict(lambda: defaultdict(int))
    status = defaultdict(int)
    totals = defaultdict(float)
    since_key = since_local.strftime("%Y-%m-%d %H:%M:%S") if since_local else None

    for label, path in sources:
        pending: dict[str, tuple[str, str]] = {}   # 요청ID -> (모델, 계정)
        with open(os.path.expanduser(path), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                head = GW_LINE.match(line)
                if not head:
                    continue
                date, clock, rid = head.groups()
                if "selector.go" in line:
                    sel = GW_SELECT.search(line)
                    if sel:
                        pending[rid] = (sel.group(2), sel.group(1))
                    continue
                if "gin_logger.go" not in line:
                    continue
                gin = GW_GIN.search(line)
                if not gin:
                    continue
                code, took, ip, method, req_path = gin.groups()
                model, auth = pending.pop(rid, ("(unknown)", ""))
                if method != "POST" or not GW_INFER_PATH.match(req_path):
                    continue
                if date[:7] not in months or (since_key and f"{date} {clock}" < since_key):
                    continue

                ok = 1 if code.startswith("2") else 0
                secs = duration_seconds(took) or 0.0
                client = client_names.get(ip, ip)
                acct = account_label(auth) if auth else "(unknown)"
                for bucket in (daily[date], by_model[model], by_account[acct],
                               by_client[client], by_gateway[label]):
                    bucket["req"] += 1
                    bucket["ok"] += ok
                daily[date][f"gw:{label}"] += 1
                by_model[model]["fam"] = model_family(model)   # type: ignore[assignment]
                status[code] += 1
                totals["req"] += 1
                totals["ok"] += ok
                totals["secs"] += secs

    def ranked(d: dict) -> list[dict]:
        rows = [{"name": k, **v} for k, v in d.items()]
        rows.sort(key=lambda r: -r["req"])
        return rows

    req = int(totals["req"])
    return {
        "requests": req,
        "ok": int(totals["ok"]),
        "avg_sec": round(totals["secs"] / req, 2) if req else 0,
        "gateways": [label for label, _ in sources],
        "daily": {k: dict(v) for k, v in sorted(daily.items())},
        "by_model": ranked(by_model)[:TOP_N],
        "models_total": len(by_model),
        "by_account": ranked(by_account),
        "by_client": ranked(by_client),
        "by_gateway": ranked(by_gateway),
        "status": dict(sorted(status.items(), key=lambda kv: -kv[1])),
    }


def aggregate_gateway_usage(sources: list[tuple[str, str]], since_iso: str | None,
                            months: set[str], client_names: dict[str, str]) -> dict:
    """CLIProxyAPI 사용량 queue를 쌓은 JSONL(cliproxy-usage-collect)에서 토큰을 집계한다.

    게이트웨이를 지나는 모든 클라이언트가 포함되므로 이 PC의 로컬 로그와 겹친다.
    합산하지 말고 '전체 사용량'은 이 값을 기준으로 본다.
    """
    daily = defaultdict(lambda: defaultdict(int))
    by_family = defaultdict(lambda: defaultdict(int))
    by_model = defaultdict(lambda: defaultdict(int))
    by_client = defaultdict(lambda: defaultdict(int))
    by_account = defaultdict(lambda: defaultdict(int))
    totals = defaultdict(int)
    seen: set[str] = set()
    files: list[str] = []
    for label, pattern in sources:
        matched = sorted(glob.glob(os.path.expanduser(pattern)))
        files += [(label, f) for f in matched]   # type: ignore[misc]

    for label, path in files:   # type: ignore[misc]
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = r.get("ts") or ""
                if ts[:7] not in months or (since_iso and ts < since_iso):
                    continue
                rid = r.get("request_id")
                if rid:
                    key = f"{label}:{rid}"
                    if key in seen:
                        continue
                    seen.add(key)
                tk = r.get("tokens") or {}
                rec = {
                    "in":  tk.get("input_tokens") or 0,
                    "out": tk.get("output_tokens") or 0,
                    "cw":  tk.get("cache_creation_tokens") or 0,
                    "cr":  tk.get("cache_read_tokens") or 0,
                }
                total = tk.get("total_tokens") or sum(rec.values())
                model = r.get("alias") or r.get("model") or "(unknown)"
                fam = model_family(model)
                acct = r.get("account") or ""
                m = EMAIL.search(acct)
                acct = (f"{r.get('provider') or ''} · {mask_local(m.group(1))}@{m.group(2)}"
                        if m else (r.get("provider") or "(unknown)"))
                client = client_names.get(r.get("client_ip") or "", r.get("client_ip") or "(unknown)")
                for bucket in (daily[ts[:10]], by_family[fam], by_model[model],
                               by_client[client], by_account[acct], totals):
                    for k in TOKEN_KEYS:
                        bucket[k] += rec[k]
                    bucket["total"] += total
                    bucket["req"] += 1
                by_model[model]["fam"] = fam   # type: ignore[assignment]

    cost = 0.0
    for fam, v in by_family.items():
        if fam in UNPRICED_FAMILIES:
            v["cost"], v["unpriced"] = 0.0, 1   # type: ignore[assignment]
            continue
        p = PRICING.get(fam, DEFAULT_PRICING)
        v["cost"] = round(sum(v[k] * p[k] for k in TOKEN_KEYS) / 1_000_000, 2)   # type: ignore[assignment]
        cost += v["cost"]

    def ranked(d: dict) -> list[dict]:
        rows = [{"name": k, **v} for k, v in d.items()]
        rows.sort(key=lambda r: -r["total"])
        return rows

    days = sorted(daily)
    return {
        "requests": totals["req"],
        "totals": dict(totals),
        "total_cost": round(cost, 2),
        "files": len(files),
        "span": [days[0], days[-1]] if days else [],
        "daily": {k: dict(v) for k, v in sorted(daily.items())},
        "by_family": {k: dict(v) for k, v in by_family.items()},
        "by_model": ranked(by_model)[:TOP_N],
        "by_client": ranked(by_client),
        "by_account": ranked(by_account),
    }


def parse_pairs(items: list[str], what: str) -> list[tuple[str, str]]:
    pairs = []
    for it in items:
        if "=" not in it:
            raise SystemExit(f"[오류] {what} 형식은 이름=값 입니다: {it}")
        k, v = it.split("=", 1)
        pairs.append((k.strip(), v.strip()))
    return pairs


def render(data: dict, template_path: str, generated: str) -> str:
    with open(template_path, "r", encoding="utf-8") as fh:
        tpl = fh.read()
    return (tpl
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__GEN__", generated))


def with_date(path: str, when: datetime) -> str:
    """파일명 스템 뒤에 -YYYY-MM-DD 를 삽입한다.

    예) claude-token-report.html -> claude-token-report-2026-06-30.html
    os.path 로 디렉토리/확장자를 분리하므로 윈도우·리눅스·macOS 에서 동일하게 동작한다.
    """
    head, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    return os.path.join(head, f"{stem}-{when:%Y-%m-%d}{ext}")


def avoid_overwrite(path: str) -> str:
    """경로가 이미 존재하면 -2, -3 ... 순번을 붙여 충돌 없는 경로를 돌려준다.

    같은 날 여러 번 실행해도 이전 리포트를 덮어쓰지 않도록 한다.
    """
    if not os.path.exists(path):
        return path
    head, base = os.path.split(path)
    stem, ext = os.path.splitext(base)
    i = 2
    while True:
        cand = os.path.join(head, f"{stem}-{i}{ext}")
        if not os.path.exists(cand):
            return cand
        i += 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Claude Code 토큰 사용량 HTML 리포트 생성기")
    ap.add_argument("--months", type=int, default=3, help="집계할 최근 개월 수 (기본 3)")
    ap.add_argument("--days", type=int, default=None,
                    help="최근 N일만 집계 (지정하면 --months 대신 사용. 예: --days 30)")
    ap.add_argument("--gateway-log", action="append", default=[], metavar="이름=경로",
                    help="CLIProxyAPI 로그도 집계 (여러 번 지정 가능). "
                         "예: --gateway-log mac=~/Library/Logs/cliproxyapi.log")
    ap.add_argument("--gateway-usage", action="append", default=[], metavar="이름=경로(glob)",
                    help="CLIProxyAPI 사용량 수집 JSONL(토큰 포함)도 집계 (여러 번 지정 가능). "
                         "예: --gateway-usage mac='~/Library/Application Support/cliproxy-usage/*.jsonl'")
    ap.add_argument("--client-name", action="append", default=[], metavar="IP=이름",
                    help="게이트웨이 클라이언트 IP에 표시 이름 지정 (여러 번 지정 가능)")
    ap.add_argument("--projects-dir", default="~/.claude/projects",
                    help="Claude Code 로그 디렉토리 (기본 ~/.claude/projects)")
    ap.add_argument("-o", "--output", default=None,
                    help="출력 HTML 경로 (미지정 시 claude-token-report-YYYY-MM-DD.html, "
                         "같은 날 재실행하면 -2, -3 순번을 붙여 중복 방지)")
    ap.add_argument("--data", default=None, help="집계 JSON도 저장할 경로(선택)")
    ap.add_argument("--template", default=None,
                    help="HTML 템플릿 경로 (기본: 스크립트 옆 template.html)")
    args = ap.parse_args(argv)

    root = os.path.expanduser(args.projects_dir)
    if not os.path.isdir(root):
        print(f"[오류] 로그 디렉토리를 찾을 수 없습니다: {root}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    since_utc = since_local = None
    if args.days:
        since_utc = now - timedelta(days=args.days)
        since_local = since_utc.astimezone().replace(tzinfo=None)   # 게이트웨이 로그는 로컬 시각
        months = set(months_between(since_utc, now))
        print(f"스캔: {root}\n대상 기간: 최근 {args.days}일 ({since_utc:%Y-%m-%d} ~)")
    else:
        months = set(recent_months(max(1, args.months), now))
        print(f"스캔: {root}\n대상 월: {sorted(months)}")

    since_iso = since_utc.strftime("%Y-%m-%dT%H:%M:%S") if since_utc else None
    data = aggregate(root, months, since_iso)
    data["meta"]["days_requested"] = args.days
    if not data["daily"]:
        print("[경고] 해당 기간에 집계할 로컬 사용량 데이터가 없습니다.", file=sys.stderr)

    gateway_sources = parse_pairs(args.gateway_log, "--gateway-log")
    if gateway_sources:
        for label, path in gateway_sources:
            if not os.path.isfile(os.path.expanduser(path)):
                print(f"[오류] 게이트웨이 로그를 찾을 수 없습니다: {label}={path}", file=sys.stderr)
                return 1
    client_names = dict(parse_pairs(args.client_name, "--client-name"))
    if gateway_sources:
        data["gateway"] = aggregate_gateway(gateway_sources, since_local, months, client_names)
    usage_sources = parse_pairs(args.gateway_usage, "--gateway-usage")
    if usage_sources:
        data["gateway_usage"] = aggregate_gateway_usage(usage_sources, since_iso, months, client_names)
        if not data["gateway_usage"]["files"]:
            print("[경고] --gateway-usage 경로에 맞는 파일이 없습니다.", file=sys.stderr)

    template = args.template or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "template.html")
    if not os.path.isfile(template):
        print(f"[오류] 템플릿을 찾을 수 없습니다: {template}", file=sys.stderr)
        return 1

    # 출력 경로 결정: -o 미지정 시 날짜를 붙이고 같은 날 중복은 순번으로 회피.
    # -o 를 명시하면 사용자가 준 경로를 그대로 존중한다.
    if args.output is None:
        output = avoid_overwrite(with_date(f"{DEFAULT_OUTPUT_STEM}.html", now))
    else:
        output = args.output

    html = render(data, template, now.strftime("%Y-%m-%d"))
    with open(output, "w", encoding="utf-8") as fh:
        fh.write(html)

    if args.data:
        with open(args.data, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    t = data["totals"]
    days = list(data["daily"].keys())
    span = f"{days[0]} ~ {days[-1]}" if days else "데이터 없음"
    print(f"완료: {output}")
    print(f"  기간       : {span} ({len(days)}일)")
    print(f"  총 토큰    : {t.get('total', 0):,}")
    print(f"  추정 비용  : ${data['total_cost']:,.2f}")
    print(f"  고유 메시지: {data['meta']['unique_messages']:,}")
    gw = data.get("gateway")
    if gw:
        rate = gw["ok"] / gw["requests"] * 100 if gw["requests"] else 0
        print(f"  게이트웨이 : 요청 {gw['requests']:,}건 (성공 {rate:.1f}%, "
              f"{', '.join(gw['gateways'])})")
    gu = data.get("gateway_usage")
    if gu:
        span = " ~ ".join(gu["span"]) or "데이터 없음"
        print(f"  게이트웨이 토큰: {gu['totals'].get('total', 0):,} ({gu['requests']:,}건, "
              f"추정 ${gu['total_cost']:,.2f}, {span})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
