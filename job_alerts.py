#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import email.message
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "job_watch_config.json"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"
DISCOVERED_SOURCES_PATH = DATA_DIR / "discovered_sources.json"
SEEN_JOBS_PATH = DATA_DIR / "seen_jobs.json"
HISTORY_PATH = DATA_DIR / "job_history.jsonl"
LATEST_MD_PATH = OUTPUT_DIR / "latest_job_matches.md"
LATEST_JSON_PATH = OUTPUT_DIR / "latest_job_matches.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; SaurabhJobAlerts/1.0; "
    "+https://github.com/saukr1006/job-alerts)"
)
NETWORK_SKIPS: dict[str, int] = {}


@dataclasses.dataclass(frozen=True)
class Job:
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str
    source_company: str = ""
    posted_at: str = ""
    external_id: str = ""

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [
                normalize_space(self.company).lower(),
                normalize_space(self.title).lower(),
                normalize_space(self.location).lower(),
                normalize_space(self.url).lower(),
                normalize_space(self.external_id).lower(),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def text_from_html(value: Any) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return normalize_space(value)


def term_matches(term: str, text: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    escaped = re.escape(term)
    if term[0].isalnum() and term[-1].isalnum():
        return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text, re.I) is not None
    return term in text


def extract_experience_requirements(text: str) -> list[dict[str, Any]]:
    text = normalize_space(text).lower()
    if not text:
        return []

    patterns = [
        re.compile(r"(?P<min>\d{1,2})\s*(?:-|–|—|to)\s*(?P<max>\d{1,2})\+?\s*(?:years|yrs)\b"),
        re.compile(r"(?:minimum|at least|more than)\s+(?:of\s+)?(?P<min>\d{1,2})\+?\s*(?:years|yrs)\b"),
        re.compile(r"(?P<min>\d{1,2})\+\s*(?:years|yrs)\b"),
        re.compile(r"(?P<min>\d{1,2})\s*(?:or more|plus)\s*(?:years|yrs)\b"),
        re.compile(r"(?P<min>\d{1,2})\s*(?:years|yrs)\s*\+"),
    ]
    requirements: list[dict[str, Any]] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            start, end = match.span()
            context = text[max(0, start - 90) : min(len(text), end + 90)]
            if not any(marker in context for marker in ["experience", "experienced", " exp", "software development", "engineering"]):
                continue
            min_years = int(match.group("min"))
            max_years = int(match.groupdict().get("max") or 0) or None
            if min_years > 30:
                continue
            requirements.append(
                {
                    "min": min_years,
                    "max": max_years,
                    "text": normalize_space(context),
                }
            )
    return requirements


def slugify(value: str, separator: str = "") -> str:
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", separator, value)
    value = re.sub(rf"{re.escape(separator)}+", separator, value) if separator else value
    return value.strip(separator)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def http_request(
    url: str,
    *,
    timeout: int,
    accept: str = "application/json,text/xml,application/rss+xml,text/html;q=0.8",
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
    )
    context = None
    if os.environ.get("JOB_ALERTS_INSECURE_SSL") == "1":
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.status, response.read()


def record_network_skip(url: str, exc: BaseException) -> None:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "unknown-host"
    reason = f"{host}: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
    NETWORK_SKIPS[reason] = NETWORK_SKIPS.get(reason, 0) + 1


def http_json(url: str, timeout: int) -> Any | None:
    try:
        status, body = http_request(url, timeout=timeout, accept="application/json")
        if status >= 400:
            return None
        return json.loads(body.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        record_network_skip(url, exc)
        return None


def http_xml(url: str, timeout: int) -> ET.Element | None:
    try:
        status, body = http_request(url, timeout=timeout)
        if status >= 400:
            return None
        return ET.fromstring(body)
    except (urllib.error.URLError, TimeoutError, ET.ParseError, OSError) as exc:
        record_network_skip(url, exc)
        return None


CORPORATE_SUFFIXES = [
    " inc", " inc.", " incorporated", " llc", " ltd", " ltd.", " limited",
    " co", " co.", " corp", " corp.", " corporation", " group",
    " technologies", " technology", " systems", " software",
    " services", " solutions", " consultancy services", " consulting",
    " global", " international", " holdings", " company",
    " & co.", " & co", " and co.", " and co",
]


def strip_corporate_suffix(value: str) -> str:
    lowered = value.lower().strip()
    changed = True
    while changed:
        changed = False
        for suffix in CORPORATE_SUFFIXES:
            if lowered.endswith(suffix):
                lowered = lowered[: -len(suffix)].strip()
                changed = True
        lowered = lowered.rstrip("&,. ").strip()
    return lowered


def company_slug_candidates(company: dict[str, Any], limit: int) -> list[str]:
    base_values: list[str] = [company["name"], *company.get("aliases", [])]
    expanded: list[str] = []
    for value in base_values:
        expanded.append(value)
        stripped = strip_corporate_suffix(value)
        if stripped and stripped != value.lower().strip():
            expanded.append(stripped)

    values: list[str] = []
    for value in expanded:
        cleaned = value.replace("+", " plus ").replace("&", " and ")
        no_space = slugify(cleaned, "")
        dashed = slugify(cleaned, "-")
        values.extend(
            [
                no_space,
                dashed,
                f"{no_space}india",
                f"{no_space}-india",
                f"{no_space}careers",
                f"{no_space}-careers",
                f"{no_space}softwareprivatelimited",
                f"{no_space}technologies",
                f"{no_space}group",
            ]
        )

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
        if len(result) >= limit:
            break
    return result


def watched_company_matcher(companies: list[dict[str, Any]]) -> tuple[dict[str, str], re.Pattern[str]]:
    aliases: dict[str, str] = {}
    terms: list[str] = []
    for company in companies:
        names = [company["name"], *company.get("aliases", [])]
        for name in names:
            cleaned = normalize_space(name).lower()
            aliases[cleaned] = company["name"]
            terms.append(re.escape(cleaned))
    terms.sort(key=len, reverse=True)
    pattern = re.compile(r"(?<![a-z0-9])(" + "|".join(terms) + r")(?![a-z0-9])", re.I)
    return aliases, pattern


def infer_company_from_text(text: str, config: dict[str, Any]) -> str:
    _, pattern = watched_company_matcher(config["companies"])
    match = pattern.search(text.lower())
    if match:
        matched = match.group(1).lower()
        for company in config["companies"]:
            names = [company["name"], *company.get("aliases", [])]
            if matched in [normalize_space(name).lower() for name in names]:
                return company["name"]
    return ""


def infer_watched_company(job: Job, config: dict[str, Any]) -> str:
    if job.source_company:
        return job.source_company
    company_match = infer_company_from_text(job.company, config)
    if company_match:
        return company_match
    if job.source == "rss":
        return infer_company_from_text(" ".join([job.title, job.description]), config)
    return ""


def fetch_greenhouse(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = http_json(url, timeout)
    jobs = []
    for item in (data or {}).get("jobs", []):
        location = normalize_space((item.get("location") or {}).get("name"))
        jobs.append(
            Job(
                title=normalize_space(item.get("title")),
                company=company_name,
                location=location,
                url=normalize_space(item.get("absolute_url")),
                description=text_from_html(item.get("content")),
                source="greenhouse",
                source_company=company_name,
                external_id=str(item.get("id") or ""),
            )
        )
    return valid_jobs(jobs)


def fetch_lever(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=100"
    data = http_json(url, timeout)
    jobs = []
    for item in data or []:
        categories = item.get("categories") or {}
        description = " ".join(
            [
                text_from_html(item.get("descriptionPlain") or item.get("description")),
                text_from_html(item.get("additionalPlain") or item.get("additional")),
                text_from_html(item.get("lists")),
            ]
        )
        jobs.append(
            Job(
                title=normalize_space(item.get("text")),
                company=company_name,
                location=normalize_space(categories.get("location")),
                url=normalize_space(item.get("hostedUrl") or item.get("applyUrl")),
                description=normalize_space(description),
                source="lever",
                source_company=company_name,
                external_id=str(item.get("id") or ""),
            )
        )
    return valid_jobs(jobs)


def fetch_ashby(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = http_json(url, timeout)
    jobs = []
    for item in (data or {}).get("jobs", []):
        location = item.get("location")
        if isinstance(location, dict):
            location = location.get("name")
        jobs.append(
            Job(
                title=normalize_space(item.get("title")),
                company=company_name,
                location=normalize_space(location),
                url=normalize_space(item.get("jobUrl") or item.get("url")),
                description=text_from_html(item.get("descriptionPlain") or item.get("descriptionHtml")),
                source="ashby",
                source_company=company_name,
                external_id=str(item.get("id") or ""),
            )
        )
    return valid_jobs(jobs)


def fetch_workable(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
    data = http_json(url, timeout)
    jobs = []
    for item in (data or {}).get("jobs", []):
        location = item.get("location") or {}
        if isinstance(location, dict):
            location = ", ".join(str(v) for v in location.values() if v)
        jobs.append(
            Job(
                title=normalize_space(item.get("title")),
                company=company_name,
                location=normalize_space(location),
                url=normalize_space(item.get("url") or item.get("shortlink")),
                description=text_from_html(item.get("description")),
                source="workable",
                source_company=company_name,
                external_id=str(item.get("shortcode") or item.get("id") or ""),
            )
        )
    return valid_jobs(jobs)


def fetch_recruitee(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://{slug}.recruitee.com/api/offers"
    data = http_json(url, timeout)
    jobs = []
    for item in (data or {}).get("offers", []):
        location = ", ".join(
            normalize_space(item.get(key))
            for key in ["city", "country", "location"]
            if normalize_space(item.get(key))
        )
        jobs.append(
            Job(
                title=normalize_space(item.get("title")),
                company=company_name,
                location=location,
                url=normalize_space(item.get("careers_url") or item.get("url")),
                description=text_from_html(item.get("description")),
                source="recruitee",
                source_company=company_name,
                external_id=str(item.get("id") or ""),
            )
        )
    return valid_jobs(jobs)


def fetch_personio(company_name: str, slug: str, timeout: int) -> list[Job]:
    jobs = []
    for host in [f"{slug}.jobs.personio.de", f"{slug}.jobs.personio.com"]:
        root = http_xml(f"https://{host}/xml?language=en", timeout)
        if root is None:
            continue
        for item in root.findall(".//position"):
            title = normalize_space(item.findtext("name"))
            location = normalize_space(item.findtext("office"))
            url = normalize_space(item.findtext("recruitingCategory") or f"https://{host}")
            description = " ".join(
                text_from_html(node.text)
                for node in item.findall(".//jobDescription")
                if normalize_space(node.text)
            )
            jobs.append(
                Job(
                    title=title,
                    company=company_name,
                    location=location,
                    url=url,
                    description=normalize_space(description),
                    source="personio",
                    source_company=company_name,
                    external_id=normalize_space(item.findtext("id")),
                )
            )
    return valid_jobs(jobs)


RELEVANT_TITLE_TERMS = [
    "software", "backend", "back end", "java", "platform", "distributed",
    "data engineer", "kafka", "genai", "ai ", "machine learning", "sde",
    "developer", "engineer",
]
NOISY_TITLE_TERMS = [
    "intern", "internship", "manager", "principal", "staff", "architect",
    "support", "solutions engineer", "sales", "recruiter", "hr ",
]


def is_relevant_job_title(title: str) -> bool:
    lowered = f" {title.lower()} "
    if not any(term in lowered for term in RELEVANT_TITLE_TERMS):
        return False
    if any(term in lowered for term in NOISY_TITLE_TERMS):
        return False
    return True


def fetch_smartrecruiters_detail(slug: str, posting_id: str, timeout: int) -> str:
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}"
    data = http_json(url, timeout)
    if not isinstance(data, dict):
        return ""
    sections = ((data.get("jobAd") or {}).get("sections")) or {}
    parts = []
    for key in ["jobDescription", "qualifications", "additionalInformation"]:
        text = ((sections.get(key) or {}).get("text")) or ""
        if text:
            parts.append(text_from_html(text))
    return normalize_space(" ".join(parts))


def fetch_smartrecruiters(company_name: str, slug: str, timeout: int) -> list[Job]:
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    data = http_json(url, timeout)
    summaries = []
    for item in (data or {}).get("content", []):
        title = normalize_space(item.get("name"))
        if not title:
            continue
        location = item.get("location") or {}
        location_parts = [
            normalize_space(location.get(key))
            for key in ["city", "region", "country"]
            if normalize_space(location.get(key))
        ]
        posting_url = normalize_space(item.get("ref") or item.get("postingUrl") or item.get("applyUrl"))
        summaries.append(
            {
                "id": str(item.get("id") or ""),
                "title": title,
                "location": ", ".join(location_parts),
                "url": posting_url,
            }
        )

    relevant = [item for item in summaries if is_relevant_job_title(item["title"])][:60]

    def fetch_detail(item: dict[str, Any]) -> tuple[dict[str, Any], str]:
        return item, fetch_smartrecruiters_detail(slug, item["id"], timeout)

    descriptions: dict[str, str] = {}
    if relevant:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for item, description in executor.map(fetch_detail, relevant):
                descriptions[item["id"]] = description

    jobs = []
    for item in summaries:
        jobs.append(
            Job(
                title=item["title"],
                company=company_name,
                location=item["location"],
                url=item["url"],
                description=descriptions.get(item["id"], ""),
                source="smartrecruiters",
                source_company=company_name,
                external_id=item["id"],
            )
        )
    return valid_jobs(jobs)


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "personio": fetch_personio,
    "smartrecruiters": fetch_smartrecruiters,
}


def valid_jobs(jobs: Iterable[Job]) -> list[Job]:
    return [job for job in jobs if job.title and job.url]


def discover_direct_sources(config: dict[str, Any], *, force: bool = False) -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_json(DISCOVERED_SOURCES_PATH, [])
    if existing and not force:
        return existing

    direct = config["sources"]["direct_ats"]
    timeout = int(direct.get("request_timeout_seconds", 8))
    slug_limit = int(direct.get("max_slug_candidates_per_company", 4))
    tasks: list[tuple[str, str, str]] = []
    for company in config["companies"]:
        for slug in company_slug_candidates(company, slug_limit):
            for source_type in ATS_FETCHERS:
                tasks.append((company["name"], source_type, slug))

    discovered: list[dict[str, Any]] = []
    seen_sources: set[tuple[str, str, str]] = set()

    def probe(task: tuple[str, str, str]) -> dict[str, Any] | None:
        company_name, source_type, slug = task
        fetcher = ATS_FETCHERS[source_type]
        jobs = fetcher(company_name, slug, timeout)
        if not jobs:
            return None
        return {
            "company": company_name,
            "source": source_type,
            "slug": slug,
            "jobs_found": len(jobs),
            "discovered_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    workers = int(direct.get("concurrency", 12))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(probe, tasks):
            if not result:
                continue
            key = (result["company"], result["source"], result["slug"])
            if key not in seen_sources:
                discovered.append(result)
                seen_sources.add(key)

    discovered.sort(key=lambda item: (item["company"], item["source"], item["slug"]))
    write_json(DISCOVERED_SOURCES_PATH, discovered)
    return discovered


def fetch_discovered_sources(config: dict[str, Any], sources: list[dict[str, Any]]) -> list[Job]:
    if not config["sources"]["direct_ats"].get("enabled", True):
        return []
    timeout = int(config["sources"]["direct_ats"].get("request_timeout_seconds", 8))

    def fetch(source: dict[str, Any]) -> list[Job]:
        fetcher = ATS_FETCHERS.get(source["source"])
        if not fetcher:
            return []
        return fetcher(source["company"], source["slug"], timeout)

    jobs: list[Job] = []
    workers = int(config["sources"]["direct_ats"].get("concurrency", 12))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(fetch, sources):
            jobs.extend(result)
    return jobs


def workday_search(source: dict[str, Any], query: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    host = source["host"].strip().rstrip("/")
    tenant = source["tenant"].strip()
    site = source["site"].strip()
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    payload = json.dumps(
        {
            "appliedFacets": {},
            "limit": limit,
            "offset": 0,
            "searchText": query,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = None
    if os.environ.get("JOB_ALERTS_INSECURE_SSL") == "1":
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
            return list(data.get("jobPostings") or [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        record_network_skip(url, exc)
        return []


def workday_detail(source: dict[str, Any], external_path: str, timeout: int) -> dict[str, Any] | None:
    host = source["host"].strip().rstrip("/")
    tenant = source["tenant"].strip()
    site = source["site"].strip()
    path = "/" + external_path.lstrip("/")
    url = f"https://{host}/wday/cxs/{tenant}/{site}{path}"
    data = http_json(url, timeout=timeout)
    if not isinstance(data, dict):
        return None
    return data.get("jobPostingInfo") or data


def is_relevant_workday_summary(item: dict[str, Any], config: dict[str, Any]) -> bool:
    title = normalize_space(item.get("title")).lower()
    location = normalize_space(item.get("locationsText") or item.get("location")).lower()
    if not title:
        return False

    relevant_title_terms = [
        "software",
        "backend",
        "back end",
        "java",
        "platform",
        "distributed",
        "data engineer",
        "kafka",
        "genai",
        "ai ",
        "machine learning",
    ]
    if not any(term in f"{title} " for term in relevant_title_terms):
        return False

    noisy_title_terms = [
        "intern",
        "internship",
        "manager",
        "principal",
        "staff",
        "architect",
        "support",
        "solutions engineer",
        "sales",
    ]
    if any(term in title for term in noisy_title_terms):
        return False

    target_locations = [location.lower() for location in config["profile"].get("target_locations", [])]
    if any(term_matches(target, location) for target in target_locations):
        return True
    if re.search(r"\b\d+\s+locations?\b", location):
        return True
    if not location:
        return True
    return False


def fetch_workday(config: dict[str, Any]) -> list[Job]:
    workday = config["sources"].get("workday", {})
    if not workday.get("enabled", False):
        return []

    queries = workday.get("queries", [])
    limit = int(workday.get("limit_per_query", 20))
    timeout = int(workday.get("request_timeout_seconds", 12))
    max_details = int(workday.get("max_detail_fetches_per_source", 80))
    search_workers = int(workday.get("search_concurrency", 16))
    detail_workers = int(workday.get("detail_concurrency", 8))
    jobs: list[Job] = []
    sources = list(workday.get("manual_sources", []))
    source_results: dict[int, dict[str, dict[str, Any]]] = {index: {} for index, _ in enumerate(sources)}

    search_tasks = [(index, source, query) for index, source in enumerate(sources) for query in queries]

    def run_search(task: tuple[int, dict[str, Any], str]) -> tuple[int, list[dict[str, Any]]]:
        index, source, query = task
        return index, workday_search(source, query, limit, timeout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=search_workers) as executor:
        for index, postings in executor.map(run_search, search_tasks):
            for item in postings:
                external_path = normalize_space(item.get("externalPath"))
                if external_path and is_relevant_workday_summary(item, config):
                    source_results[index].setdefault(external_path, item)

    for index, source in enumerate(sources):
        found = source_results[index]
        candidates = list(found.items())[:max_details]

        def fetch_detail(candidate: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
            external_path, item = candidate
            return external_path, item, workday_detail(source, external_path, timeout)

        with concurrent.futures.ThreadPoolExecutor(max_workers=detail_workers) as executor:
            details = list(executor.map(fetch_detail, candidates))

        for external_path, item, detail in details:
            title = normalize_space((detail or {}).get("title") or item.get("title"))
            location = normalize_space(
                (detail or {}).get("location")
                or item.get("locationsText")
                or item.get("location")
            )
            description = text_from_html((detail or {}).get("jobDescription") or item.get("description"))
            bullet_fields = item.get("bulletFields") or []
            if isinstance(bullet_fields, list):
                bullet_fields = " ".join(str(value) for value in bullet_fields)
            job_req_id = normalize_space((detail or {}).get("jobReqId") or bullet_fields or external_path)
            site = source["site"].strip()
            url = normalize_space((detail or {}).get("externalUrl"))
            if not url:
                url = f"https://{source['host'].strip().rstrip('/')}/en-US/{site}{external_path}"
            jobs.append(
                Job(
                    title=title,
                    company=source["company"],
                    location=location,
                    url=url,
                    description=description,
                    source="workday",
                    source_company=source["company"],
                    posted_at=normalize_space((detail or {}).get("postedOn") or item.get("postedOn")),
                    external_id=job_req_id,
                )
            )
    return valid_jobs(dedupe_jobs(jobs))


def fetch_remoteok(config: dict[str, Any]) -> list[Job]:
    remoteok = config["sources"].get("remoteok", {})
    if not remoteok.get("enabled", False):
        return []
    jobs: list[Job] = []
    for tag in remoteok.get("tags", []):
        encoded = urllib.parse.quote(tag)
        url = f"https://remoteok.com/api?tag={encoded}"
        data = http_json(url, timeout=20)
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict) or "position" not in item:
                continue
            jobs.append(
                Job(
                    title=normalize_space(item.get("position")),
                    company=normalize_space(item.get("company")),
                    location=normalize_space(item.get("location") or "Remote"),
                    url=normalize_space(item.get("url") or item.get("apply_url")),
                    description=text_from_html(
                        " ".join(
                            [
                                str(item.get("description") or ""),
                                " ".join(item.get("tags") or []),
                            ]
                        )
                    ),
                    source="remoteok",
                    posted_at=normalize_space(item.get("date")),
                    external_id=str(item.get("id") or ""),
                )
            )
        time.sleep(1)
    return valid_jobs(dedupe_jobs(jobs))


def fetch_adzuna(config: dict[str, Any]) -> list[Job]:
    adzuna = config["sources"].get("adzuna", {})
    if not adzuna.get("enabled", False):
        return []
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        return []

    country = adzuna.get("country", "in")
    jobs: list[Job] = []
    for query in adzuna.get("queries", []):
        params = urllib.parse.urlencode(
            {
                "app_id": app_id,
                "app_key": app_key,
                "what": query,
                "where": "India",
                "results_per_page": 50,
                "sort_by": "date",
                "content-type": "application/json",
            }
        )
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{params}"
        data = http_json(url, timeout=20)
        for item in (data or {}).get("results", []):
            company = item.get("company") or {}
            location = item.get("location") or {}
            jobs.append(
                Job(
                    title=normalize_space(item.get("title")),
                    company=normalize_space(company.get("display_name")),
                    location=normalize_space(location.get("display_name")),
                    url=normalize_space(item.get("redirect_url")),
                    description=text_from_html(item.get("description")),
                    source="adzuna",
                    posted_at=normalize_space(item.get("created")),
                    external_id=str(item.get("id") or ""),
                )
            )
    return valid_jobs(dedupe_jobs(jobs))


def fetch_rss(config: dict[str, Any]) -> list[Job]:
    rss = config["sources"].get("rss", {})
    if not rss.get("enabled", False):
        return []
    jobs: list[Job] = []
    for feed in rss.get("feeds", []):
        url = feed.get("url") if isinstance(feed, dict) else str(feed)
        source_name = feed.get("name", "rss") if isinstance(feed, dict) else "rss"
        root = http_xml(url, timeout=20)
        if root is None:
            continue
        for item in root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title")
            link = item.findtext("link") or ""
            atom_link = item.find("{http://www.w3.org/2005/Atom}link")
            if atom_link is not None:
                link = atom_link.attrib.get("href", link)
            description = item.findtext("description") or item.findtext("summary") or ""
            jobs.append(
                Job(
                    title=normalize_space(title),
                    company=source_name,
                    location="",
                    url=normalize_space(link),
                    description=text_from_html(description),
                    source="rss",
                    external_id=normalize_space(link),
                )
            )
    return valid_jobs(dedupe_jobs(jobs))


def dedupe_jobs(jobs: Iterable[Job]) -> list[Job]:
    seen: set[str] = set()
    result: list[Job] = []
    for job in jobs:
        key = job.fingerprint
        if key in seen:
            continue
        seen.add(key)
        result.append(job)
    return result


def score_job(job: Job, config: dict[str, Any]) -> dict[str, Any]:
    profile = config["profile"]
    experience_profile = profile.get("experience", {})
    title = job.title.lower()
    body = " ".join([job.title, job.company, job.location, job.description]).lower()
    score = 0
    reasons: list[str] = []
    penalties: list[str] = []
    excluded = False
    exclude_reason = ""

    requirements = extract_experience_requirements(" ".join([job.title, job.description]))
    title_requirements = extract_experience_requirements(job.title)
    max_required_min_years = int(
        experience_profile.get(
            "max_required_min_years",
            experience_profile.get("candidate_years", 4),
        )
    )
    minimum_target_years = int(experience_profile.get("minimum_target_years", 3))
    junior_title_requirements = [
        requirement
        for requirement in title_requirements
        if int(requirement["min"]) < minimum_target_years
        and requirement.get("max")
        and int(requirement["max"]) < minimum_target_years
    ]
    if junior_title_requirements:
        requirement = junior_title_requirements[0]
        penalties.append(f"title_experience:{requirement['min']}-{requirement['max']} years")
        excluded = True
        exclude_reason = f"title targets {requirement['min']}-{requirement['max']} years"
    too_senior_requirements = [
        requirement for requirement in requirements if int(requirement["min"]) > max_required_min_years
    ]
    matching_requirements = [
        requirement for requirement in requirements if int(requirement["min"]) <= max_required_min_years
    ]
    if too_senior_requirements:
        requirement = min(too_senior_requirements, key=lambda item: int(item["min"]))
        penalties.append(f"experience:{requirement['min']}+ years required")
        score -= 80
        if experience_profile.get("exclude_if_minimum_above_candidate", True):
            excluded = True
            exclude_reason = f"requires {requirement['min']}+ years"
    elif matching_requirements:
        requirement = max(matching_requirements, key=lambda item: int(item["min"]))
        if int(requirement["min"]) < minimum_target_years:
            score -= 18
            penalties.append(f"junior_experience:{requirement['min']}+ years")
        else:
            score += 8
            reasons.append(f"experience-fit:{requirement['min']}+ years")

    for term, weight in profile["title_boosts"].items():
        if term_matches(term, title):
            score += int(weight)
            reasons.append(f"title:{term}")

    for term, penalty in profile.get("title_penalties", {}).items():
        if term_matches(term, title):
            score -= int(penalty)
            penalties.append(f"title:{term}")

    for category, payload in profile["positive_keywords"].items():
        matches = [term for term in payload["terms"] if term_matches(term, body)]
        if matches:
            score += int(payload["weight"])
            reasons.append(f"{category}:{', '.join(matches[:3])}")

    for category, payload in profile["negative_keywords"].items():
        matches = [term for term in payload["terms"] if term_matches(term, body)]
        if matches:
            score -= int(payload["penalty"])
            penalties.append(f"{category}:{', '.join(matches[:3])}")

    watched_company = infer_watched_company(job, config)
    if watched_company:
        score += 12
        reasons.append(f"watchlist:{watched_company}")

    location_text = job.location.lower()
    location_matches_target = any(term_matches(location.lower(), location_text) for location in profile["target_locations"])
    if location_matches_target:
        score += 8
        reasons.append("location-match")
    elif location_text and not any(term_matches(term, body) for term in ["remote", "relocation provided", "india"]):
        penalty = int(profile.get("non_target_location_penalty", 0))
        if penalty:
            score -= penalty
            penalties.append("non_target_location")

    score = max(score, 0)
    return {
        "score": score,
        "reasons": reasons[:6],
        "penalties": penalties[:4],
        "watched_company": watched_company,
        "excluded": excluded,
        "exclude_reason": exclude_reason,
        "experience_requirements": requirements[:3],
    }


def load_seen() -> dict[str, Any]:
    return load_json(SEEN_JOBS_PATH, {"seen": {}})


def update_seen(seen: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    entries = seen.setdefault("seen", {})
    for item in jobs:
        entries[item["id"]] = {
            "first_seen_at": entries.get(item["id"], {}).get("first_seen_at", now),
            "last_seen_at": now,
            "title": item["title"],
            "company": item["company"],
            "url": item["url"],
            "score": item["score"],
        }
    write_json(SEEN_JOBS_PATH, seen)


def build_matches(jobs: list[Job], config: dict[str, Any]) -> list[dict[str, Any]]:
    min_score = int(config["profile"].get("minimum_score_to_notify", 45))
    matches: list[dict[str, Any]] = []
    for job in dedupe_jobs(jobs):
        scored = score_job(job, config)
        if scored.get("excluded"):
            continue
        watched_company = scored["watched_company"]
        if not watched_company and job.source not in {"greenhouse", "lever", "ashby", "workable", "recruitee", "personio"}:
            continue
        if scored["score"] < min_score:
            continue
        matches.append(
            {
                "id": job.fingerprint,
                "score": scored["score"],
                "title": job.title,
                "company": watched_company or job.company,
                "source_company": job.source_company,
                "location": job.location,
                "url": job.url,
                "source": job.source,
                "posted_at": job.posted_at,
                "reasons": scored["reasons"],
                "penalties": scored["penalties"],
                "experience_requirements": scored.get("experience_requirements", []),
                "description_preview": textwrap.shorten(job.description, width=280, placeholder="..."),
            }
        )
    matches.sort(key=lambda item: (-item["score"], item["company"], item["title"]))
    max_per_company = int(config["profile"].get("max_results_per_company", 5))
    max_per_company_title = int(config["profile"].get("max_results_per_company_title", 2))
    diversified: list[dict[str, Any]] = []
    company_counts: dict[str, int] = {}
    company_title_counts: dict[tuple[str, str], int] = {}
    for item in matches:
        company = item["company"]
        normalized_title = re.sub(r"\s+", " ", item["title"].lower()).strip()
        company_title_key = (company, normalized_title)
        if company_counts.get(company, 0) >= max_per_company:
            continue
        if company_title_counts.get(company_title_key, 0) >= max_per_company_title:
            continue
        diversified.append(item)
        company_counts[company] = company_counts.get(company, 0) + 1
        company_title_counts[company_title_key] = company_title_counts.get(company_title_key, 0) + 1
        if len(diversified) >= int(config["profile"].get("max_results_per_run", 25)):
            break
    return diversified


def render_markdown(
    matches: list[dict[str, Any]],
    *,
    only_new: bool,
    discovered_count: int,
    network_skips: dict[str, int],
) -> str:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "# Latest Job Matches",
        "",
        f"Generated: {now}",
        f"Mode: {'new jobs only' if only_new else 'top current matches'}",
        f"Discovered direct ATS sources: {discovered_count}",
        f"Matches: {len(matches)}",
        "",
    ]
    if network_skips:
        lines.extend(["## Source Warnings", ""])
        for reason, count in sorted(network_skips.items(), key=lambda item: (-item[1], item[0]))[:10]:
            lines.append(f"- {count} skipped request(s): {reason}")
        lines.append("")
    if not matches:
        lines.extend(
            [
                "No matching jobs crossed the configured score threshold in this run.",
                "",
                "Check whether your direct ATS discovery found sources, or add RSS feeds/API keys in config.",
            ]
        )
        return "\n".join(lines) + "\n"

    for index, item in enumerate(matches, 1):
        lines.extend(
            [
                f"## {index}. {item['title']}",
                "",
                f"- Company: {item['company']}",
                f"- Score: {item['score']}",
                f"- Location: {item['location'] or 'Not specified'}",
                f"- Source: {item['source']}",
                f"- Apply: {item['url']}",
                f"- Reasons: {', '.join(item['reasons']) or 'n/a'}",
            ]
        )
        if item["penalties"]:
            lines.append(f"- Penalties: {', '.join(item['penalties'])}")
        if item.get("experience_requirements"):
            requirements = []
            for requirement in item["experience_requirements"][:2]:
                if requirement.get("max"):
                    requirements.append(f"{requirement['min']}-{requirement['max']} years")
                else:
                    requirements.append(f"{requirement['min']}+ years")
            lines.append(f"- Experience signal: {', '.join(requirements)}")
        if item["description_preview"]:
            lines.append(f"- Preview: {item['description_preview']}")
        lines.append("")
    return "\n".join(lines)


def append_history(matches: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        for item in matches:
            payload = {"recorded_at": now, **item}
            handle.write(json.dumps(payload, sort_keys=True) + "\n")


def notify_telegram(matches: list[dict[str, Any]], markdown_path: Path) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not matches:
        print("Skipping Telegram notification: no reported matches.")
        return
    if not token or not chat_id:
        print("Skipping Telegram notification: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        return

    lines = ["Latest job matches:"]
    for item in matches[:10]:
        lines.append(f"{item['score']} - {item['company']} - {item['title']}\n{item['url']}")
    lines.append(f"\nFull report: {markdown_path.name}")
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": "\n\n".join(lines)})
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST")
    with urllib.request.urlopen(request, timeout=20):
        pass
    print(f"Telegram notification sent for {min(len(matches), 10)} job(s).")


def notify_discord(matches: list[dict[str, Any]]) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook or not matches:
        return
    lines = ["**Latest job matches**"]
    for item in matches[:10]:
        lines.append(f"- **{item['score']}** [{item['company']}] {item['title']} - {item['url']}")
    payload = json.dumps({"content": "\n".join(lines)}).encode("utf-8")
    request = urllib.request.Request(
        webhook,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20):
        pass


def notify_email(matches: list[dict[str, Any]], markdown: str) -> None:
    if not matches:
        return

    host = os.environ.get("SMTP_HOST", "").strip()
    port_raw = os.environ.get("SMTP_PORT", "").strip() or "587"
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    recipient = os.environ.get("SMTP_TO", "").strip()
    if not all([host, user, password, sender, recipient]) or not matches:
        return
    try:
        port = int(port_raw)
    except ValueError:
        print(f"Skipping email notification: invalid SMTP_PORT={port_raw!r}")
        return

    message = email.message.EmailMessage()
    message["Subject"] = f"{len(matches)} job matches to apply"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(markdown)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message)


def notify_safely(name: str, callback: Any) -> None:
    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - notifications must not break the job report.
        print(f"Skipping {name} notification after error: {type(exc).__name__}: {exc}")


def run(config: dict[str, Any], *, only_new: bool, force_discovery: bool, notify: bool) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    discovered = []
    if config["sources"]["direct_ats"].get("enabled", True):
        discovered = discover_direct_sources(config, force=force_discovery)

    jobs: list[Job] = []
    jobs.extend(fetch_discovered_sources(config, discovered))
    jobs.extend(fetch_workday(config))
    jobs.extend(fetch_remoteok(config))
    jobs.extend(fetch_adzuna(config))
    jobs.extend(fetch_rss(config))

    matches = build_matches(jobs, config)
    seen = load_seen()
    if only_new:
        existing_ids = set(seen.get("seen", {}).keys())
        new_matches = [item for item in matches if item["id"] not in existing_ids]
        if not existing_ids:
            new_matches = matches
        matches_to_report = new_matches
    else:
        matches_to_report = matches

    update_seen(seen, matches)
    append_history(matches_to_report)

    markdown = render_markdown(
        matches_to_report,
        only_new=only_new,
        discovered_count=len(discovered),
        network_skips=NETWORK_SKIPS,
    )
    LATEST_MD_PATH.write_text(markdown, encoding="utf-8")
    write_json(LATEST_JSON_PATH, matches_to_report)

    if notify and not matches_to_report:
        print("No reported matches; all notifications skipped.")
    elif notify:
        notify_safely("Telegram", lambda: notify_telegram(matches_to_report, LATEST_MD_PATH))
        notify_safely("Discord", lambda: notify_discord(matches_to_report))
        notify_safely("email", lambda: notify_email(matches_to_report, markdown))

    print(f"Fetched jobs: {len(jobs)}")
    print(f"Matches above threshold: {len(matches)}")
    print(f"Reported matches: {len(matches_to_report)}")
    if NETWORK_SKIPS:
        print("Network skips:")
        for reason, count in sorted(NETWORK_SKIPS.items(), key=lambda item: (-item[1], item[0]))[:5]:
            print(f"  {count}x {reason}")
    print(f"Report: {LATEST_MD_PATH}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Free job-opening watcher and notifier.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_parser = subparsers.add_parser("discover", help="Discover public ATS sources for watched companies.")
    discover_parser.add_argument("--force", action="store_true", help="Rebuild discovered source cache.")

    run_parser = subparsers.add_parser("run", help="Fetch, score, report, and optionally notify.")
    run_parser.add_argument("--only-new", action="store_true", help="Report only jobs not seen in previous runs.")
    run_parser.add_argument("--force-discovery", action="store_true", help="Refresh direct ATS source discovery before running.")
    run_parser.add_argument("--notify", action="store_true", help="Send Telegram/Discord/email notifications if env vars are set.")

    args = parser.parse_args(argv)
    config = load_json(CONFIG_PATH, {})

    if args.command == "discover":
        sources = discover_direct_sources(config, force=args.force)
        print(f"Discovered direct ATS sources: {len(sources)}")
        print(f"Cache: {DISCOVERED_SOURCES_PATH}")
        return 0

    if args.command == "run":
        return run(config, only_new=args.only_new, force_discovery=args.force_discovery, notify=args.notify)

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))