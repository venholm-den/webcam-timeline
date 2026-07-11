from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "WebcamTimelineReferenceFetcher/1.0"
METADATA_COLUMNS = [
    "name",
    "aircraft_type",
    "query",
    "filename",
    "source_page",
    "source_image_url",
    "author",
    "license",
    "license_url",
    "commons_title",
    "sha256",
    "downloaded_at_utc",
]
AIRCRAFT_TYPE_ALIASES = {
    "A109": "AgustaWestland AW109 helicopter",
    "P28A": "Piper PA-28 aircraft",
    "PA28": "Piper PA-28 aircraft",
    "PC12": "Pilatus PC-12 aircraft",
}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def request_bytes(url: str, retries: int = 3) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*",
        },
    )
    last_error: BaseException | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(attempt * 3)

    if last_error is not None:
        raise last_error

    raise urllib.error.URLError(f"Unable to download {url}")


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "aircraft"


def read_flight_queries(csv_path: Path) -> list[tuple[str, str, str]]:
    if not csv_path.exists():
        return []

    queries: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            callsign = clean_text(row.get("callsign"))
            registration = clean_text(row.get("registration"))
            aircraft_type = clean_text(row.get("aircraft_type"))
            name = registration or callsign or aircraft_type
            parts = [part for part in [registration, callsign, aircraft_type, "aircraft"] if part]
            query = " ".join(parts)

            if not name or not query:
                continue

            key = query.lower()
            if key in seen:
                continue

            seen.add(key)
            queries.append((name, aircraft_type, query))

            alias = AIRCRAFT_TYPE_ALIASES.get(aircraft_type.upper())
            if alias and alias.lower() not in seen:
                seen.add(alias.lower())
                queries.append((name, aircraft_type, alias))

    return queries


def commons_search(query: str, limit: int) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": "6",
        "gsrsearch": query,
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime",
        "iiurlwidth": "900",
        "origin": "*",
    }
    url = f"{COMMONS_API}?{urllib.parse.urlencode(params)}"
    data = request_json(url)
    pages = data.get("query", {}).get("pages", {})
    return sorted(pages.values(), key=lambda page: page.get("index", 9999))


def image_extension(url: str, mime: str) -> str:
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if mime == "image/png":
        return ".png"
    if mime == "image/webp":
        return ".webp"
    return ".jpg"


def metadata_value(extmetadata: dict[str, Any], key: str) -> str:
    value = extmetadata.get(key, {})
    if isinstance(value, dict):
        return clean_text(value.get("value"))
    return clean_text(value)


def existing_metadata(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_metadata(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped: dict[str, dict[str, str]] = {}

    for row in rows:
        key = row.get("source_image_url") or row.get("sha256") or row.get("filename")
        if key:
            deduped[key] = row

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(deduped.values(), key=lambda row: (row.get("name", ""), row.get("filename", ""))))


def download_references(
    output_dir: Path,
    metadata_path: Path,
    queries: list[tuple[str, str, str]],
    limit_per_query: int,
    pause_seconds: float,
    dry_run: bool,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = existing_metadata(metadata_path)
    known_urls = {row.get("source_image_url", "") for row in rows}
    downloaded = 0

    for name, aircraft_type, query in queries:
        print(f"Searching Wikimedia Commons: {query}")
        pages = commons_search(query, limit_per_query)

        for page in pages:
            image_infos = page.get("imageinfo") or []
            if not image_infos:
                continue

            info = image_infos[0]
            image_url = info.get("thumburl") or info.get("url")
            source_url = info.get("descriptionurl") or ""
            mime = info.get("mime") or ""

            if not image_url or image_url in known_urls or not str(mime).startswith("image/"):
                continue

            extmetadata = info.get("extmetadata") or {}
            title = clean_text(page.get("title", "")).replace("File:", "")
            extension = image_extension(image_url, mime)
            base = safe_filename(f"{name}_{aircraft_type}_{title}")[:120]
            try:
                body = b"" if dry_run else request_bytes(image_url)
            except Exception as exc:
                print(f"  Skipped download: {image_url} ({exc})")
                continue
            sha = hashlib.sha256(body or image_url.encode("utf-8")).hexdigest()
            filename = f"{base}_{sha[:10]}{extension}"
            target = output_dir / filename

            if dry_run:
                print(f"  DRY RUN {filename} <- {source_url}")
            else:
                target.write_bytes(body)
                print(f"  Saved {target}")

            rows.append(
                {
                    "name": name,
                    "aircraft_type": aircraft_type,
                    "query": query,
                    "filename": filename,
                    "source_page": source_url,
                    "source_image_url": image_url,
                    "author": metadata_value(extmetadata, "Artist") or metadata_value(extmetadata, "Credit"),
                    "license": metadata_value(extmetadata, "LicenseShortName") or metadata_value(extmetadata, "UsageTerms"),
                    "license_url": metadata_value(extmetadata, "LicenseUrl"),
                    "commons_title": clean_text(page.get("title", "")),
                    "sha256": sha,
                    "downloaded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            )
            known_urls.add(image_url)
            downloaded += 1

            if pause_seconds:
                time.sleep(pause_seconds)

    if not dry_run:
        write_metadata(metadata_path, rows)

    return downloaded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch labelled aircraft reference images from Wikimedia Commons.")
    parser.add_argument("--query", action="append", help="Search query. Can be supplied more than once.")
    parser.add_argument("--flights-csv", default="data/flights.csv", help="Flight CSV used to build default queries.")
    parser.add_argument("--output-dir", default="data/aircraft_crops", help="Where downloaded crop/reference images are saved.")
    parser.add_argument("--metadata", default="data/aircraft_crops/references.csv", help="Metadata CSV for downloaded references.")
    parser.add_argument("--limit-per-query", type=int, default=3, help="Maximum Commons results per query.")
    parser.add_argument("--pause-seconds", type=float, default=2.0, help="Pause between image downloads.")
    parser.add_argument("--dry-run", action="store_true", help="Search and print matches without downloading.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = PROJECT_ROOT / args.output_dir
    metadata_path = PROJECT_ROOT / args.metadata
    queries = read_flight_queries(PROJECT_ROOT / args.flights_csv)

    if args.query:
        for query in args.query:
            queries.append((query, "", query))

    if not queries:
        print("No queries found. Add --query or populate data/flights.csv.")
        return 1

    downloaded = download_references(
        output_dir=output_dir,
        metadata_path=metadata_path,
        queries=queries,
        limit_per_query=max(args.limit_per_query, 1),
        pause_seconds=max(args.pause_seconds, 0),
        dry_run=args.dry_run,
    )
    print(f"downloaded={downloaded} metadata={metadata_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
