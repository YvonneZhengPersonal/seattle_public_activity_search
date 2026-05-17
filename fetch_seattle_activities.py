#!/usr/bin/env python3
"""
Download Seattle Parks community activities from the same API the official site uses,
then write a deduplicated JSON file for the static web UI.

Source listing page: https://anc.apm.activecommunities.com/seattle/activity/search?viewMode=list

Usage:
  python3 fetch_seattle_activities.py
  python3 fetch_seattle_activities.py --per-page 100 --sleep 0.25
  python3 fetch_seattle_activities.py --quiet
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from difflib import get_close_matches
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://anc.apm.activecommunities.com"
SEARCH_URL = f"{BASE}/seattle/activity/search?viewMode=list"
LIST_URL = f"{BASE}/seattle/rest/activities/list"
MAP_URL = f"{BASE}/seattle/rest/activities/map"

# Listing labels that still don't match map "center_name" values even after normalization/fuzzy match.
LOCATION_ALIASES: Dict[str, str] = {
    "IDCCC": "International District/Chinatown C.C.",
}


def new_opener() -> urllib.request.OpenerDirector:
    cj = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def http_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    h = {"User-Agent": "Mozilla/5.0 (SeattleActivityLocalTool/1.0)", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", "ignore")[:800]
        raise RuntimeError(f"HTTP {e.code} for {url}: {snippet}") from e


def get_csrf(opener: urllib.request.OpenerDirector) -> str:
    req = urllib.request.Request(
        SEARCH_URL,
        headers={"User-Agent": "Mozilla/5.0 (SeattleActivityLocalTool/1.0)"},
    )
    with opener.open(req, timeout=120) as resp:
        html = resp.read().decode("utf-8", "ignore")
    m = re.search(r'window\.__csrfToken\s*=\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError("Could not find CSRF token in search page HTML.")
    return m.group(1)


def api_headers(csrf: str, page_info: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-TOKEN": csrf,
        "page_info": json.dumps(page_info),
    }


def change_param_key(filter_body: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror frontend changeParamKey for price fields."""
    asp = filter_body.setdefault("activity_search_pattern", {})
    asp["custom_price_from"] = asp.get("price_from")
    asp["custom_price_to"] = asp.get("price_to")
    asp.pop("price_from", None)
    asp.pop("price_to", None)
    return filter_body


def fetch_map_centers(opener: urllib.request.OpenerDirector, csrf: str) -> Dict[str, Tuple[float, float]]:
    asp = {
        "activity_keyword": "",
        "landing_page_search_date": None,
        "price_from": None,
        "price_to": None,
    }
    body = change_param_key({"activity_search_pattern": dict(asp), "activity_transfer_pattern": {}})
    page_info = {"order_by": "NAME", "page_number": 1, "total_records_per_page": 20}
    j = http_json(
        opener,
        MAP_URL,
        method="POST",
        body=body,
        headers=api_headers(csrf, page_info),
    )
    out: Dict[str, Tuple[float, float]] = {}
    for p in j.get("body", {}).get("map_points", []) or []:
        name = (p.get("center_name") or "").strip()
        lat = p.get("latitude")
        lon = p.get("longitude")
        if name and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            out[name] = (float(lat), float(lon))
    return out


def fetch_all_items(
    opener: urllib.request.OpenerDirector,
    csrf: str,
    *,
    per_page: int,
    sleep_s: float,
    order_by: str = "NAME",
    progress: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    asp = {
        "activity_keyword": "",
        "landing_page_search_date": None,
        "price_from": None,
        "price_to": None,
    }
    base_body = change_param_key({"activity_search_pattern": dict(asp), "activity_transfer_pattern": {}})

    page = 1
    items: List[Dict[str, Any]] = []
    last_meta: Dict[str, Any] = {}
    t0 = time.perf_counter()
    while True:
        page_info = {"order_by": order_by, "page_number": page, "total_records_per_page": per_page}
        j = http_json(
            opener,
            LIST_URL,
            method="POST",
            body=dict(base_body),
            headers=api_headers(csrf, page_info),
        )
        hdrs = j.get("headers") or {}
        body = j.get("body") or {}
        chunk = body.get("activity_items") or []
        last_meta = hdrs.get("page_info") or {}
        items.extend(chunk)
        total_page = int(last_meta.get("total_page") or 1)
        total_records = last_meta.get("total_records")
        if progress:
            elapsed = time.perf_counter() - t0
            tr = f" / ~{total_records}" if total_records is not None else ""
            print(
                f"[list] 第 {page}/{total_page} 页 · 已拉取 {len(items)} 条{tr} · {elapsed:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        if page >= total_page or not chunk:
            break
        page += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return items, last_meta


def normalize_location_label(label: str) -> str:
    s = " ".join(label.strip().split())
    if not s or s == "N/A":
        return s
    s = re.sub(r"\bCmty Ctr\b", "Community Center", s, flags=re.I)
    s = re.sub(r"\bCtr\b", "Center", s, flags=re.I)
    s = re.sub(r"\bPk\b", "Park", s, flags=re.I)
    s = re.sub(r"\bGardn\b", "Garden", s, flags=re.I)
    s = re.sub(r"\bAudobon\b", "Audubon", s, flags=re.I)
    return " ".join(s.split())


def augment_centers(centers: Dict[str, Tuple[float, float]]) -> Dict[str, Tuple[float, float]]:
    augmented = dict(centers)
    for name, latlon in centers.items():
        variants = {name, normalize_location_label(name)}
        for v in variants:
            if v and v not in augmented:
                augmented[v] = latlon
    return augmented


def resolve_latlon(
    label: str,
    centers: Dict[str, Tuple[float, float]],
    *,
    cutoff: float = 0.82,
) -> Optional[Tuple[float, float]]:
    raw = " ".join((label or "").strip().split())
    if not raw or raw == "N/A":
        return None
    if raw in LOCATION_ALIASES:
        raw = LOCATION_ALIASES[raw]
    candidates = [raw, normalize_location_label(raw)]

    for c in candidates:
        if c in centers:
            return centers[c]

    names = list(centers.keys())
    for c in candidates:
        match = get_close_matches(c, names, n=1, cutoff=cutoff)
        if match:
            return centers[match[0]]
    return None


def norm_loc(label: str) -> str:
    return " ".join(label.strip().split())


def dedupe_key(item: Dict[str, Any]) -> Tuple[str, str]:
    name = (item.get("name") or "").strip()
    loc = norm_loc((item.get("location") or {}).get("label") or "")
    return name, loc


@dataclass
class Group:
    name: str
    location: str
    age_description: str
    ages: str
    age_min_year: int
    age_min_month: int
    age_min_week: int
    age_max_year: int
    age_max_month: int
    age_max_week: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    slots: List[Dict[str, Any]] = field(default_factory=list)


def build_groups(
    items: List[Dict[str, Any]],
    centers: Dict[str, Tuple[float, float]],
) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Group] = {}
    order: List[Tuple[str, str]] = []

    for it in items:
        name, loc = dedupe_key(it)
        if not name:
            continue
        key = (name, loc)
        if key not in groups:
            groups[key] = Group(
                name=name,
                location=loc,
                age_description=(it.get("age_description") or "").strip(),
                ages=(it.get("ages") or "").strip(),
                age_min_year=int(it.get("age_min_year") or 0),
                age_min_month=int(it.get("age_min_month") or 0),
                age_min_week=int(it.get("age_min_week") or 0),
                age_max_year=int(it.get("age_max_year") or 0),
                age_max_month=int(it.get("age_max_month") or 0),
                age_max_week=int(it.get("age_max_week") or 0),
            )
            order.append(key)
            latlon = resolve_latlon(loc, centers)
            if latlon:
                g = groups[key]
                g.lat, g.lon = latlon

        slot = {
            "id": it.get("id"),
            "activity_number": it.get("number"),
            "days_of_week": (it.get("days_of_week") or "").strip(),
            "time_range": (it.get("time_range") or it.get("time_range_landing_page") or "").strip(),
            "date_range": (it.get("date_range") or "").strip(),
            "date_range_description": (it.get("date_range_description") or "").strip(),
            "openings": (it.get("openings") or "").strip(),
            "detail_url": it.get("detail_url"),
        }
        groups[key].slots.append(slot)

    # stable output
    out: List[Dict[str, Any]] = []
    for key in order:
        gdict = groups[key].__dict__.copy()
        # sort slots for deterministic UX
        gdict["slots"].sort(
            key=lambda s: (
                s.get("days_of_week") or "",
                s.get("time_range") or "",
                str(s.get("date_range") or ""),
                str(s.get("id") or ""),
            )
        )
        out.append(gdict)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-page", type=int, default=100)
    ap.add_argument("--sleep", type=float, default=0.25, help="Delay between paginated list requests.")
    ap.add_argument("--quiet", action="store_true", help="No progress lines on stderr.")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent / "web" / "data.json",
        help="Output JSON path for the static site.",
    )
    args = ap.parse_args()

    run0 = time.perf_counter()
    opener = new_opener()
    csrf = get_csrf(opener)
    t_map0 = time.perf_counter()
    centers_raw = fetch_map_centers(opener, csrf)
    if not args.quiet:
        print(
            f"[map] 场地坐标 {len(centers_raw)} 个 · {time.perf_counter() - t_map0:.1f}s",
            file=sys.stderr,
            flush=True,
        )
    centers = augment_centers(centers_raw)
    items, page_meta = fetch_all_items(
        opener,
        csrf,
        per_page=args.per_page,
        sleep_s=args.sleep,
        progress=not args.quiet,
    )
    grouped = build_groups(items, centers)

    geocoded = sum(1 for g in grouped if g.get("lat") is not None and g.get("lon") is not None)

    payload = {
        "source": SEARCH_URL,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api_total_records": page_meta.get("total_records"),
        "raw_item_count": len(items),
        "deduped_group_count": len(grouped),
        "map_center_count": len(centers_raw),
        "groups_with_coordinates": geocoded,
        "activities": grouped,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total_s = time.perf_counter() - run0
    print(
        f"Wrote {args.out} ({len(items)} rows -> {len(grouped)} groups, "
        f"{payload['api_total_records']} official total records, "
        f"{geocoded}/{len(grouped)} with map coords, "
        f"{total_s:.1f}s total)."
    )


if __name__ == "__main__":
    main()
