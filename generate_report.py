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
from datetime import datetime, timezone

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

TOKEN_KEYS = ("in", "out", "cw", "cr")


def model_family(model: str | None) -> str:
    if not model:
        return "other"
    m = model.lower()
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


def aggregate(root: str, months: set[str]) -> dict:
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
    months = set(recent_months(max(1, args.months), now))
    print(f"스캔: {root}\n대상 월: {sorted(months)}")

    data = aggregate(root, months)
    if not data["daily"]:
        print("[경고] 해당 기간에 집계할 사용량 데이터가 없습니다.", file=sys.stderr)

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
