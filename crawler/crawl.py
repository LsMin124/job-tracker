#!/usr/bin/env python3
"""다중 소스 채용공고 크롤러 → data/postings.json

소스 3종 (표준 라이브러리만 사용):
1. 자소설닷컴 — 등록 기업 페이지 + 전체 검색 전수 열거 (division=1 신입, 규모 필터)
2. 사람인 — IT 직군 최신 목록 (career 텍스트로 신입 판별)
3. 링커리어 — 최신 채용 목록 (jobTypes=NEW 판별)

공통 필터: 학사 신입 지원 가능(경력·인턴·교육·석박 전용 제외).
자소설은 기업규모(대기업/중견)로 필터하고, 규모 정보가 없는 소스는
공채형 제목이거나 기존 등록 기업일 때만 채택해 중소·비정규 노이즈를 막는다.
직무 키워드로 공고를 걸러내지 않는다(전 도메인·전 직무 수집).
강조 여부는 프런트에서 사용자별 관심 키워드로 판단한다.
"""
import gzip
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REQUEST_INTERVAL_SEC = 1.0
JOBS_PER_POSTING = 40

# 자소설 등록 기업 (기업 페이지 정밀 수집 + 기업명 정규화 기준)
COMPANIES = [
    {"company": "삼성전자 DX", "id": 14665},
    {"company": "현대위아", "id": 25},
    {"company": "현대자동차", "id": 1472},
    {"company": "현대모비스", "id": 27},
    {"company": "LG전자", "id": 789},
    {"company": "LG CNS", "id": 57},
    {"company": "현대오토에버", "id": 269},
    {"company": "포스코DX", "id": 54},
    {"company": "세메스", "id": 966},
    {"company": "HD현대로보틱스", "id": 5492},
    {"company": "삼성SDS", "id": 137},
    {"company": "LS일렉트릭", "id": 14058},
    {"company": "레인보우로보틱스", "id": 14694},
    {"company": "로보스타", "id": 5764},
    {"company": "고영테크놀러지", "id": 1073},
    {"company": "현대로템", "id": 3887},
    {"company": "기아", "id": 1690},
    {"company": "HL만도", "id": 38},
    {"company": "한화시스템", "id": 4332},
    {"company": "한화시스템", "id": 13786},  # 방산부문
    {"company": "에스에프에이", "id": 1370},
    {"company": "LG이노텍", "id": 56},
    {"company": "SK하이닉스", "id": 1511},
    {"company": "삼성디스플레이", "id": 135},
]
ID_TO_COMPANY = {c["id"]: c["company"] for c in COMPANIES}
KNOWN_COMPANIES = set(ID_TO_COMPANY.values()) | {"두산로보틱스", "한화로보틱스"}

ALIASES = {
    "삼성전자": "삼성전자 DX", "삼성전자 DX부문": "삼성전자 DX",
    "LS ELECTRIC": "LS일렉트릭", "엘에스일렉트릭": "LS일렉트릭",
    "고영": "고영테크놀러지",
}
SIZE_MAP = {"big_business": "대기업", "middle_market": "중견",
            "small_business": "중소", "public_institution": "공공"}

# 공채형 제목 — 규모 미상 소스의 채택 조건
PUBLIC_RE = re.compile(r"공채|공개\s*채용|신입\s*사원|대졸|정기\s*채용|수시\s*채용")
MASTERS_ONLY = re.compile(r"석\s*[/·]?\s*박|석박|박사|석사")
BACHELOR_OK = re.compile(r"학\s*[/·]?\s*석|학사")

SEARCH_PAGES_MAX = 4        # 자소설 전체 검색 (perPage=50)
SARAMIN_PAGES = 3           # 사람인 IT 직군 최신순
LINKAREER_PAGES = 8         # 링커리어 최신순 (페이지당 20)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as res:
        body = res.read()
        if res.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body.decode("utf-8", errors="ignore")


def parse_next_data(html: str) -> dict:
    m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found")
    return json.loads(m.group(1))


def normalize_company(name: str) -> str:
    n = re.sub(r"[㈜]|\(주\)|주식회사", "", str(name or "")).strip()
    return ALIASES.get(n, n)


def is_masters_only(text: str) -> bool:
    return bool(MASTERS_ONLY.search(text)) and not BACHELOR_OK.search(text)


# ---------- 소스 1: 자소설 ----------

def emp_divisions(e: dict) -> set:
    d = e.get("division")
    return set(d if isinstance(d, list) else [d]) - {None}


def jaso_job_to_posting(job: dict, company: str, today: str) -> dict | None:
    end = str(job.get("end_time") or "")[:10]
    if not end or end < today:
        return None
    emps = job.get("employments") or []
    divisions = set()
    for e in emps:
        divisions.update(emp_divisions(e))
    if 1 not in divisions:  # 1=신입
        return None
    title = str(job.get("title") or "").strip()
    if is_masters_only(title):
        return None
    newgrad = [e for e in emps if 1 in emp_divisions(e)]
    fields_src = newgrad if newgrad else emps
    fields = [str(e.get("field") or "") for e in fields_src]
    fields = [f for f in fields if f and not is_masters_only(f)]
    if fields_src and any(e.get("field") for e in fields_src) and not fields:
        return None
    return {
        "id": f"js-{job['id']}",
        "source": "jasoseol",
        "company": company,
        "title": title,
        "url": f"https://jasoseol.com/recruit/{job['id']}",
        "start": str(job.get("start_time") or "")[:10],
        "end": end,
        "jobs": fields[:JOBS_PER_POSTING],
    }


def jasoseol_company_pages(today: str) -> list[dict]:
    result = []
    for conf in COMPANIES:
        try:
            url = f"https://jasoseol.com/companies/{conf['id']}/careers"
            data = parse_next_data(fetch(url))
            jobs = data["props"]["pageProps"].get("initialJobs") or []
            if not jobs:
                print(f"[warn] {conf['company']}: initialJobs 비어 있음",
                      file=sys.stderr)
            for job in jobs:
                p = jaso_job_to_posting(job, conf["company"], today)
                if p:
                    p["cg"] = conf["id"]
                    result.append(p)
        except Exception as e:  # 개별 기업 실패는 건너뛴다
            print(f"[warn] {conf['company']}: {e}", file=sys.stderr)
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


def jasoseol_search_all(today: str) -> list[dict]:
    """전체 검색 전수 열거 — 대기업·중견 신입 공고 전부."""
    result, page = [], 1
    while page <= SEARCH_PAGES_MAX:
        qs = urllib.parse.urlencode({
            "division": 1, "excludeClosed": "true",
            "perPage": 50, "page": page})
        data = parse_next_data(fetch(f"https://jasoseol.com/search?{qs}"))
        payload = None
        for q in (data["props"]["pageProps"].get("dehydratedState")
                  or {}).get("queries", []):
            cand = (q.get("state") or {}).get("data")
            if isinstance(cand, dict) and "data" in cand:
                payload = cand
                break
        if not payload:
            break
        for job in payload.get("data", []):
            cg = job.get("company_group") or {}
            size = SIZE_MAP.get(str(cg.get("business_size") or ""), "")
            company = ID_TO_COMPANY.get(
                job.get("company_group_id"),
                normalize_company(cg.get("name")))
            # 중견 이상만 (등록 기업은 규모 무관 유지)
            if size not in ("대기업", "중견") and company not in KNOWN_COMPANIES:
                continue
            p = jaso_job_to_posting(job, company, today)
            if p:
                p["cg"] = job.get("company_group_id")
                p["size"] = size or "기타"
                result.append(p)
        if page * 50 >= int(payload.get("totalCount") or 0):
            break
        page += 1
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


# ---------- 소스 2: 사람인 ----------

def saramin_deadline(text: str, today: datetime) -> str:
    t = text.strip()
    m = re.search(r"~\s*(\d{2})\.(\d{2})", t)
    if m:
        mm, dd = int(m.group(1)), int(m.group(2))
        cand = datetime(today.year, mm, dd, tzinfo=KST)
        if cand < today - timedelta(days=14):  # 연말 경계 보정
            cand = datetime(today.year + 1, mm, dd, tzinfo=KST)
        return cand.strftime("%Y-%m-%d")
    m = re.search(r"D-(\d+)", t)
    if m:
        return (today + timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    if "오늘마감" in t:
        return today.strftime("%Y-%m-%d")
    if "내일마감" in t:
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return ""  # 상시 등 — 제외


def saramin(today_dt: datetime) -> list[dict]:
    import html as H
    result = []
    for page in range(1, SARAMIN_PAGES + 1):
        url = ("https://www.saramin.co.kr/zf_user/jobs/list/job-category"
               f"?cat_mcls=2&page={page}")
        raw = fetch(url)
        for blk in re.split(r'class="box_item"', raw)[1:]:
            career = re.search(r'"career"[^>]*>([^<]+)<', blk)
            if not career or "신입" not in career.group(1):
                continue
            tit = re.search(r'class="job_tit"[^>]*>\s*<a[^>]*title="([^"]+)"', blk)
            corp = re.search(r'str_tit"[^>]*>([^<]+)<', blk)
            rec = re.search(r'rec_idx=(\d+)', blk)
            date = re.search(r'class="date"[^>]*>([^<]+)<', blk)
            if not (tit and corp and rec):
                continue
            title = H.unescape(tit.group(1)).strip()
            company = normalize_company(H.unescape(corp.group(1)))
            if is_masters_only(title):
                continue
            # 규모 미상 → 공채형 제목이거나 등록 기업일 때만
            if company not in KNOWN_COMPANIES and not PUBLIC_RE.search(title):
                continue
            end = saramin_deadline(date.group(1) if date else "", today_dt)
            if not end:
                continue
            result.append({
                "id": f"srm-{rec.group(1)}",
                "source": "saramin",
                "company": company,
                "title": title,
                "url": ("https://www.saramin.co.kr/zf_user/jobs/relay/view"
                        f"?rec_idx={rec.group(1)}"),
                "start": "",
                "end": end,
                "jobs": [],
            })
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


# ---------- 소스 3: 링커리어 ----------

def linkareer(today: str) -> list[dict]:
    result = []
    for page in range(1, LINKAREER_PAGES + 1):
        raw = fetch(f"https://linkareer.com/list/recruit?page={page}")
        data = parse_next_data(raw)
        apollo = data["props"]["pageProps"].get("__APOLLO_STATE__") or {}
        for key, act in apollo.items():
            if not key.startswith("Activity:") or not isinstance(act, dict):
                continue
            if "NEW" not in (act.get("jobTypes") or []):
                continue
            title = str(act.get("title") or "").strip()
            company = normalize_company(act.get("organizationName"))
            if not title or not company or is_masters_only(title):
                continue
            if company not in KNOWN_COMPANIES and not PUBLIC_RE.search(title):
                continue
            close = act.get("recruitCloseAt")
            if not close:
                continue
            end = datetime.fromtimestamp(int(close) / 1000, KST).strftime("%Y-%m-%d")
            if end < today:
                continue
            result.append({
                "id": f"lk-{act.get('id')}",
                "source": "linkareer",
                "company": company,
                "title": title,
                "url": f"https://linkareer.com/activity/{act.get('id')}",
                "start": "",
                "end": end,
                "jobs": [],
            })
        time.sleep(REQUEST_INTERVAL_SEC)
    return result


# ---------- 병합 ----------

def dedupe(postings: list[dict]) -> list[dict]:
    """id 중복 제거 후, 동일 기업·동일 마감일 공고는 자소설 우선."""
    by_id, by_key = {}, {}
    priority = {"jasoseol": 0, "saramin": 1, "linkareer": 2}
    for p in sorted(postings, key=lambda x: priority.get(x["source"], 9)):
        if p["id"] in by_id:
            continue
        key = (p["company"], p["end"])
        if key in by_key and p["source"] != by_key[key]:
            continue
        by_id[p["id"]] = p
        by_key.setdefault(key, p["source"])
    return sorted(by_id.values(), key=lambda p: (p["end"], p["company"]))


def main() -> int:
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    postings, errors = [], []
    stages = [
        ("자소설 기업 페이지", lambda: jasoseol_company_pages(today)),
        ("자소설 전체 검색", lambda: jasoseol_search_all(today)),
        ("사람인", lambda: saramin(now)),
        ("링커리어", lambda: linkareer(today)),
    ]
    for name, fn in stages:
        try:
            found = fn()
            postings.extend(found)
            print(f"[ok] {name}: {len(found)}")
        except Exception as e:  # 개별 소스 실패는 전체를 막지 않는다
            errors.append(f"{name}: {e}")
            print(f"[fail] {name}: {e}", file=sys.stderr)

    merged = dedupe(postings)
    out = {
        "updated": now.isoformat(timespec="seconds"),
        "postings": merged,
        "errors": errors,
    }
    out_path = Path(__file__).resolve().parent.parent / "data" / "postings.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"wrote {out_path} ({len(merged)} postings, {len(errors)} errors)")
    return 1 if errors and not merged else 0


if __name__ == "__main__":
    sys.exit(main())
