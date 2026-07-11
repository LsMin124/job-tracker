#!/usr/bin/env python3
"""자소설닷컴 기업 페이지에서 진행중 공고를 수집해 data/postings.json 생성.

표준 라이브러리만 사용 (GitHub Actions에서 의존성 설치 불필요).
기업 페이지 SSR에 임베드된 __NEXT_DATA__.props.pageProps.initialJobs를 파싱한다.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REQUEST_INTERVAL_SEC = 1.0

# company: 보드 카드의 기업명과 정확히 일치해야 프런트에서 매칭됨
# id: 자소설 company_group id (jasoseol.com/companies/{id}/careers)
# keywords: 제목/직무명에 하나라도 포함되면 hit=true (관심 직무 여부)
COMMON_KEYWORDS = ["로봇", "로보틱스", "Robot", "생산기술", "제조", "스마트팩토리",
                   "소프트웨어", "SW", "S/W", "제어", "자율주행", "비전", "AI"]
COMPANIES = [
    {"company": "삼성전자 DX", "id": 14665, "keywords": COMMON_KEYWORDS},
    {"company": "현대위아", "id": 25, "keywords": COMMON_KEYWORDS},
    {"company": "현대자동차", "id": 1472, "keywords": COMMON_KEYWORDS},
    {"company": "현대모비스", "id": 27, "keywords": COMMON_KEYWORDS},
    {"company": "LG전자", "id": 789, "keywords": COMMON_KEYWORDS},
    {"company": "LG CNS", "id": 57, "keywords": COMMON_KEYWORDS},
    {"company": "현대오토에버", "id": 269, "keywords": COMMON_KEYWORDS},
    {"company": "포스코DX", "id": 54, "keywords": COMMON_KEYWORDS},
    {"company": "세메스", "id": 966, "keywords": COMMON_KEYWORDS},
    {"company": "HD현대로보틱스", "id": 5492, "keywords": COMMON_KEYWORDS},
    {"company": "삼성SDS", "id": 137, "keywords": COMMON_KEYWORDS},
    # LS그룹 페이지에는 전 계열사 공고가 섞여 있어 키워드를 좁힌다
    {"company": "LS일렉트릭", "id": 14058,
     "keywords": ["LS ELECTRIC", "일렉트릭", "자동화", "R&D"]},
    # 두산로보틱스: 자소설 미등재, 나인하이어는 클라이언트 렌더링이라 v1 미지원
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read().decode("utf-8", errors="ignore")


def parse_next_data(html: str) -> dict:
    m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found")
    return json.loads(m.group(1))


def active_postings(conf: dict, today: str) -> list[dict]:
    url = f"https://jasoseol.com/companies/{conf['id']}/careers"
    data = parse_next_data(fetch(url))
    jobs = data["props"]["pageProps"].get("initialJobs") or []
    result = []
    for job in jobs:
        end = str(job.get("end_time") or "")[:10]
        if not end or end < today:
            continue
        # division: 1=신입, 2=경력, 3=인턴, 7=교육/아카데미 — 신입 포함 공고만
        divisions = set()
        for e in (job.get("employments") or []):
            d = e.get("division")
            divisions.update(d if isinstance(d, list) else [d])
        if 1 not in divisions:
            continue
        fields = [str(e.get("field") or "")
                  for e in (job.get("employments") or [])]
        haystack = " ".join([str(job.get("title") or "")] + fields).lower()
        hit = any(k.lower() in haystack for k in conf["keywords"])
        result.append({
            "company": conf["company"],
            "title": str(job.get("title") or "").strip(),
            "url": f"https://jasoseol.com/recruit/{job['id']}",
            "start": str(job.get("start_time") or "")[:10],
            "end": end,
            "jobs": [f for f in fields if f][:12],
            "hit": hit,
        })
    return result


def main() -> int:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    postings, errors = [], []
    for conf in COMPANIES:
        try:
            found = active_postings(conf, today)
            postings.extend(found)
            print(f"[ok] {conf['company']}: {len(found)} active")
        except Exception as e:  # 개별 기업 실패는 전체를 막지 않는다
            errors.append(f"{conf['company']}: {e}")
            print(f"[fail] {conf['company']}: {e}", file=sys.stderr)
        time.sleep(REQUEST_INTERVAL_SEC)

    postings.sort(key=lambda p: (p["end"], p["company"]))
    out = {
        "updated": datetime.now(KST).isoformat(timespec="seconds"),
        "postings": postings,
        "errors": errors,
    }
    out_path = Path(__file__).resolve().parent.parent / "data" / "postings.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"wrote {out_path} ({len(postings)} postings, {len(errors)} errors)")
    # 전 기업 실패 시에만 비정상 종료 (부분 실패는 데이터 갱신 우선)
    return 1 if errors and not postings else 0


if __name__ == "__main__":
    sys.exit(main())
