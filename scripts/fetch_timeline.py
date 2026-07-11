from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGE_URLS = [
    "https://www.egpgmet.net/html/webcam_1.html",
    "https://www.egpgmet.net/html/webcam_2.html",
]
DEFAULT_USER_AGENT = "WebcamTimelineFetcher/1.0"
DEFAULT_RETRIES = 3
EGPG_LAT = 55.9747009
EGPG_LON = -3.9755599
DEFAULT_FLIGHT_RADIUS_NM = 3.0
DEFAULT_FLIGHT_MAX_ALTITUDE_FT = 3000
TARGET_ICAO = "4016D2"
CAMERA_NAME_RE = re.compile(r"Cam(\d+)", re.IGNORECASE)
FLIGHT_COLUMNS = [
    "event_time_utc",
    "callsign",
    "registration",
    "aircraft_type",
    "origin",
    "destination",
    "direction",
    "altitude_ft",
    "groundspeed_kt",
    "vertical_rate_fpm",
    "track_deg",
    "squawk",
    "emergency",
    "seen_seconds",
    "on_ground",
    "notes",
    "source_url",
    "hex",
    "latitude",
    "longitude",
    "distance_nm",
    "provider",
]
CAMERA_COLUMNS = [
    "page_name",
    "latitude",
    "longitude",
    "heading_deg",
    "horizontal_fov_deg",
    "vertical_fov_deg",
    "pitch_deg",
    "elevation_ft",
]


@dataclass(frozen=True)
class ImageRef:
    url: str
    alt: str
    title: str


class ImageParser(HTMLParser):
    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.images: list[ImageRef] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return

        values = {key.lower(): value or "" for key, value in attrs}
        src = values.get("src", "").strip()

        if not src:
            return

        url = urllib.parse.urljoin(self.page_url, src)

        if "/Uploads/Cam" not in url or not url.lower().endswith(".jpg"):
            return

        self.images.append(
            ImageRef(
                url=url,
                alt=values.get("alt", ""),
                title=values.get("title", ""),
            )
        )


def request_headers() -> dict[str, str]:
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,image/avif,image/webp,image/jpeg,image/png,*/*",
    }


def fetch_bytes(url: str, retries: int = DEFAULT_RETRIES) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers=request_headers())
    last_error: BaseException | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                headers = {key: value for key, value in response.headers.items()}
                return response.read(), headers
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last_error = exc

            if attempt >= retries:
                break

            time.sleep(min(attempt * 2, 10))

    if last_error is not None:
        raise urllib.error.URLError(last_error)

    raise urllib.error.URLError(f"Unable to fetch {url}")


def fetch_text(url: str) -> str:
    body, headers = fetch_bytes(url)
    content_type = headers.get("Content-Type", "")
    encoding = "utf-8"

    if "charset=" in content_type:
        encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()

    return body.decode(encoding, errors="replace")


def image_refs_from_page(page_url: str) -> list[ImageRef]:
    parser = ImageParser(page_url)
    parser.feed(fetch_text(page_url))
    return parser.images


def read_existing(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_flights(csv_path: Path) -> list[dict[str, str]]:
    if not csv_path.exists():
        return []

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_cameras(csv_path: Path) -> dict[str, dict[str, str]]:
    if not csv_path.exists():
        return {}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    cameras: dict[str, dict[str, str]] = {}
    for row in rows:
        page_name = str(row.get("page_name", "")).strip()
        if not page_name:
            continue
        cameras[page_name] = {key: str(row.get(key, "")).strip() for key in CAMERA_COLUMNS}

    return cameras


def normalize_flight_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    notes = str(normalized.get("notes", ""))

    if not normalized.get("track_deg"):
        normalized["track_deg"] = normalized.get("direction", "")

    if not normalized.get("groundspeed_kt"):
        speed_match = re.search(r"groundspeed=([0-9.]+)", notes)
        if speed_match:
            normalized["groundspeed_kt"] = speed_match.group(1)
        else:
            velocity_match = re.search(r"velocity_mps=([0-9.]+)", notes)
            if velocity_match:
                normalized["groundspeed_kt"] = f"{float(velocity_match.group(1)) * 1.94384:.1f}"

    if not normalized.get("vertical_rate_fpm"):
        vertical_match = re.search(r"vertical_rate_mps=([-0-9.]+)", notes)
        if vertical_match:
            normalized["vertical_rate_fpm"] = f"{float(vertical_match.group(1)) * 196.850394:.0f}"

    if not normalized.get("on_ground"):
        normalized["on_ground"] = "true" if str(normalized.get("altitude_ft", "")).lower() == "ground" else "false"

    return normalized


def write_flights(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [normalize_flight_row(row) for row in rows]
    rows = sorted(rows, key=lambda row: str(row.get("event_time_utc", "")), reverse=True)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FLIGHT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def fetch_json(url: str) -> Any:
    body, _headers = fetch_bytes(url)
    return json.loads(body.decode("utf-8", errors="replace"))


def iso_from_unix(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_nm = 3440.065
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def flight_key(row: dict[str, Any]) -> str:
    aircraft = str(row.get("hex") or row.get("callsign") or "").strip()
    return f"{row.get('provider', '')}|{aircraft}|{row.get('event_time_utc', '')}"


def flight_distance_nm(row: dict[str, Any]) -> float | None:
    try:
        return float(row.get("distance_nm", ""))
    except (TypeError, ValueError):
        return None


def flight_altitude_ft(row: dict[str, Any]) -> float | None:
    value = str(row.get("altitude_ft", "")).strip().lower()

    if not value or value == "ground":
        return None

    try:
        return float(value)
    except ValueError:
        return None


def is_airport_local_flight(row: dict[str, Any], radius_nm: float, max_altitude_ft: float) -> bool:
    distance = flight_distance_nm(row)

    if distance is not None and distance > radius_nm:
        return False

    altitude = flight_altitude_ft(row)

    if altitude is not None and altitude > max_altitude_ft:
        return False

    return True


def filter_airport_local_flights(rows: list[dict[str, Any]], radius_nm: float, max_altitude_ft: float) -> list[dict[str, Any]]:
    return [row for row in rows if is_airport_local_flight(row, radius_nm, max_altitude_ft)]


def merge_flight_rows(existing: list[dict[str, str]], new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for row in existing:
        key = flight_key(row)
        if key.strip("|"):
            merged[key] = dict(row)

    for row in new_rows:
        key = flight_key(row)
        if key.strip("|"):
            merged[key] = dict(row)

    return list(merged.values())


def fetch_adsb_lol_flights(lat: float, lon: float, radius_nm: float, max_altitude_ft: float) -> list[dict[str, Any]]:
    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius_nm:g}"
    data = fetch_json(url)
    rows: list[dict[str, Any]] = []

    for aircraft in data.get("ac", []):
        aircraft_lat = aircraft.get("lat")
        aircraft_lon = aircraft.get("lon")

        if aircraft_lat is None or aircraft_lon is None:
            continue

        event_time = datetime.now(timezone.utc) - timedelta(seconds=float(aircraft.get("seen", 0) or 0))
        altitude = aircraft.get("alt_baro", aircraft.get("alt_geom", ""))
        altitude_ft = "" if altitude == "ground" else str(altitude or "")
        distance_nm = haversine_nm(lat, lon, float(aircraft_lat), float(aircraft_lon))
        vertical_rate = aircraft.get("baro_rate", aircraft.get("geom_rate", ""))
        row = (
            {
                "event_time_utc": event_time.isoformat(timespec="seconds"),
                "callsign": str(aircraft.get("flight", "")).strip(),
                "registration": str(aircraft.get("r", "")).strip(),
                "aircraft_type": str(aircraft.get("t", "")).strip(),
                "origin": "",
                "destination": "",
                "direction": str(aircraft.get("track", "")),
                "altitude_ft": altitude_ft,
                "groundspeed_kt": str(aircraft.get("gs", "")),
                "vertical_rate_fpm": str(vertical_rate or ""),
                "track_deg": str(aircraft.get("track", "")),
                "squawk": str(aircraft.get("squawk", "")),
                "emergency": str(aircraft.get("emergency", "")),
                "seen_seconds": str(aircraft.get("seen", "")),
                "on_ground": "true" if altitude == "ground" else "false",
                "notes": f"ADSB.lol live snapshot; groundspeed={aircraft.get('gs', '')}",
                "source_url": "https://globe.adsb.lol/",
                "hex": str(aircraft.get("hex", "")).strip(),
                "latitude": str(aircraft_lat),
                "longitude": str(aircraft_lon),
                "distance_nm": f"{distance_nm:.2f}",
                "provider": "ADSB.lol",
            }
        )
        if is_airport_local_flight(row, radius_nm, max_altitude_ft):
            rows.append(row)

    return rows


def fetch_opensky_flights(lat: float, lon: float, radius_nm: float, max_altitude_ft: float) -> list[dict[str, Any]]:
    lat_delta = radius_nm / 60.0
    lon_delta = radius_nm / (60.0 * max(math.cos(math.radians(lat)), 0.1))
    url = (
        "https://opensky-network.org/api/states/all"
        f"?lamin={lat - lat_delta:.6f}&lomin={lon - lon_delta:.6f}"
        f"&lamax={lat + lat_delta:.6f}&lomax={lon + lon_delta:.6f}"
    )
    data = fetch_json(url)
    rows: list[dict[str, Any]] = []

    for state in data.get("states") or []:
        if len(state) < 17 or state[5] is None or state[6] is None:
            continue

        aircraft_lon = float(state[5])
        aircraft_lat = float(state[6])
        distance_nm = haversine_nm(lat, lon, aircraft_lat, aircraft_lon)

        if distance_nm > radius_nm:
            continue

        altitude_ft = ""
        if state[8]:
            altitude_ft = "ground"
        elif state[7] is not None:
            altitude_ft = str(round(float(state[7]) * 3.28084))

        groundspeed = ""
        if state[9] is not None:
            groundspeed = f"{float(state[9]) * 1.94384:.1f}"

        vertical_rate = ""
        if state[11] is not None:
            vertical_rate = f"{float(state[11]) * 196.850394:.0f}"

        row = (
            {
                "event_time_utc": iso_from_unix(state[4] or data.get("time")),
                "callsign": str(state[1] or "").strip(),
                "registration": "",
                "aircraft_type": "",
                "origin": str(state[2] or "").strip(),
                "destination": "",
                "direction": str(state[10] or ""),
                "altitude_ft": altitude_ft,
                "groundspeed_kt": groundspeed,
                "vertical_rate_fpm": vertical_rate,
                "track_deg": str(state[10] or ""),
                "squawk": str(state[14] or "") if len(state) > 14 else "",
                "emergency": "",
                "seen_seconds": "",
                "on_ground": "true" if state[8] else "false",
                "notes": f"OpenSky live snapshot; velocity_mps={state[9] or ''}; vertical_rate_mps={state[11] or ''}",
                "source_url": "https://opensky-network.org/",
                "hex": str(state[0] or "").strip(),
                "latitude": f"{aircraft_lat:.6f}",
                "longitude": f"{aircraft_lon:.6f}",
                "distance_nm": f"{distance_nm:.2f}",
                "provider": "OpenSky",
            }
        )
        if is_airport_local_flight(row, radius_nm, max_altitude_ft):
            rows.append(row)

    return rows


def update_flights_csv(csv_path: Path, lat: float, lon: float, radius_nm: float, max_altitude_ft: float) -> tuple[int, str]:
    existing = filter_airport_local_flights(read_flights(csv_path), radius_nm, max_altitude_ft)
    provider = "ADSB.lol"

    try:
        new_rows = fetch_adsb_lol_flights(lat, lon, radius_nm, max_altitude_ft)
    except Exception as exc:
        print(f"ADSB.lol flight fetch failed: {exc}")
        new_rows = []

    if not new_rows:
        provider = "OpenSky"
        try:
            new_rows = fetch_opensky_flights(lat, lon, radius_nm, max_altitude_ft)
        except Exception as exc:
            print(f"OpenSky flight fetch failed: {exc}")
            new_rows = []

    before_count = len(existing)
    merged = filter_airport_local_flights(merge_flight_rows(existing, new_rows), radius_nm, max_altitude_ft)
    write_flights(csv_path, merged)
    return max(len(merged) - before_count, 0), provider


def write_timeline(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "captured_at_utc",
        "timeline_timestamp_utc",
        "image_timestamp_utc",
        "timeline_observations_utc",
        "first_seen_at_utc",
        "last_seen_at_utc",
        "seen_count",
        "timestamp_source",
        "camera_slot",
        "page_name",
        "page_url",
        "source_name",
        "source_url",
        "local_path",
        "sha256",
        "content_length",
        "last_modified",
        "etag",
    ]
    rows = [normalize_row(row) for row in rows]

    rows = sorted(
        rows,
        key=lambda row: (
            str(row.get("timeline_timestamp_utc", "")),
            str(row.get("source_name", "")),
        ),
        reverse=True,
    )

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_last_modified(value: str) -> str:
    if not value:
        return ""

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def camera_slot(source_name: str) -> int | None:
    match = CAMERA_NAME_RE.search(source_name)
    if not match:
        return None

    return int(match.group(1)) % 1000


def page_name_from_url(page_url: str) -> str:
    name = Path(urllib.parse.urlparse(page_url).path).stem

    if name == "webcam_1":
        return "Webcam 1"

    if name == "webcam_2":
        return "Webcam 2"

    return name.replace("_", " ").title() or "Webcam"


def page_name_from_source(source_name: str) -> str:
    match = CAMERA_NAME_RE.search(source_name)

    if not match:
        return "Webcam"

    family = int(match.group(1)) // 1000
    return f"Webcam {family}" if family else "Webcam"


def page_url_from_source(source_name: str) -> str:
    match = CAMERA_NAME_RE.search(source_name)

    if not match:
        return ""

    family = int(match.group(1)) // 1000

    if family == 1:
        return DEFAULT_PAGE_URLS[0]

    if family == 2:
        return DEFAULT_PAGE_URLS[1]

    return ""


def estimate_timeline_timestamp(image_timestamp: str, captured_at: str, source_name: str) -> str:
    base = parse_iso_datetime(image_timestamp) or parse_iso_datetime(captured_at)
    slot = camera_slot(source_name)

    if base is None:
        return image_timestamp or captured_at

    if slot is not None:
        base = base - timedelta(minutes=slot)

    return base.astimezone(timezone.utc).isoformat(timespec="seconds")


def observation_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def median_timestamp(values: list[str]) -> str:
    parsed = sorted(dt for dt in (parse_iso_datetime(value) for value in values) if dt is not None)

    if not parsed:
        return values[0] if values else ""

    return parsed[len(parsed) // 2].astimezone(timezone.utc).isoformat(timespec="seconds")


def add_timeline_observation(row: dict[str, Any], observation: str, captured_at: str) -> None:
    observations = observation_values(str(row.get("timeline_observations_utc", "")))

    if observation and observation not in observations:
        observations.append(observation)

    if observations:
        row["timeline_observations_utc"] = ";".join(sorted(observations))
        row["timeline_timestamp_utc"] = median_timestamp(observations)
        row["seen_count"] = str(len(observations))
        row["timestamp_source"] = "rolling_slot_median" if len(observations) > 1 else "rolling_slot"

    if not row.get("first_seen_at_utc"):
        row["first_seen_at_utc"] = captured_at

    row["last_seen_at_utc"] = captured_at


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    source_name = str(normalized.get("source_name", ""))
    slot = camera_slot(source_name)

    if not normalized.get("camera_slot") and slot is not None:
        normalized["camera_slot"] = str(slot)

    if not normalized.get("timeline_timestamp_utc"):
        estimated = estimate_timeline_timestamp(
            str(normalized.get("image_timestamp_utc", "")),
            str(normalized.get("captured_at_utc", "")),
            source_name,
        )
        normalized["timeline_timestamp_utc"] = estimated
        normalized["timestamp_source"] = normalized.get("timestamp_source") or "rolling_slot"

    if not normalized.get("timeline_observations_utc"):
        normalized["timeline_observations_utc"] = str(normalized.get("timeline_timestamp_utc", ""))

    if not normalized.get("first_seen_at_utc"):
        normalized["first_seen_at_utc"] = str(normalized.get("captured_at_utc", ""))

    if not normalized.get("last_seen_at_utc"):
        normalized["last_seen_at_utc"] = str(normalized.get("captured_at_utc", ""))

    if not normalized.get("seen_count"):
        normalized["seen_count"] = str(len(observation_values(str(normalized.get("timeline_observations_utc", "")))) or 1)

    if not normalized.get("page_name"):
        normalized["page_name"] = page_name_from_source(source_name)

    page_url = str(normalized.get("page_url", ""))
    if not page_url or "/Uploads/" in page_url:
        normalized["page_url"] = page_url_from_source(source_name) or page_url

    return normalized


def safe_name(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in ["-", "_"])
    return cleaned or "image"


def row_key(page_name: str, sha: str) -> str:
    return f"{page_name}|{sha}"


def save_images(page_urls: list[str], output_dir: Path, csv_path: Path) -> tuple[int, int]:
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = [normalize_row(row) for row in read_existing(csv_path)]
    known_keys = {row_key(str(row.get("page_name", "")), str(row.get("sha256", ""))) for row in existing}
    rows_by_key = {
        row_key(str(row.get("page_name", "")), str(row.get("sha256", ""))): row
        for row in existing
        if row.get("sha256", "")
    }
    rows: list[dict[str, Any]] = list(existing)
    new_count = 0
    skipped_count = 0

    output_dir.mkdir(parents=True, exist_ok=True)

    for page_url in page_urls:
        page_name = page_name_from_url(page_url)

        try:
            refs = image_refs_from_page(page_url)
        except urllib.error.URLError as exc:
            print(f"Fetch failed for page {page_url}: {exc}")
            continue

        for ref in refs:
            try:
                body, headers = fetch_bytes(ref.url)
            except urllib.error.URLError as exc:
                print(f"Fetch failed for {ref.url}: {exc}")
                continue

            sha = hashlib.sha256(body).hexdigest()

            last_modified = headers.get("Last-Modified", "")
            image_timestamp = parse_last_modified(last_modified) or captured_at
            source_name = ref.alt or ref.title or Path(urllib.parse.urlparse(ref.url).path).stem
            slot = camera_slot(source_name)
            timeline_timestamp = estimate_timeline_timestamp(image_timestamp, captured_at, source_name)
            key = row_key(page_name, sha)

            if key in known_keys:
                existing_row = rows_by_key.get(key)
                if existing_row is not None:
                    add_timeline_observation(existing_row, timeline_timestamp, captured_at)
                skipped_count += 1
                continue

            timestamp_slug = timeline_timestamp.replace(":", "").replace("-", "").replace("+00:00", "Z")
            filename = f"{timestamp_slug}_{safe_name(page_name)}_{safe_name(source_name)}_{sha[:12]}.jpg"
            image_path = output_dir / filename
            image_path.write_bytes(body)

            row = {
                "captured_at_utc": captured_at,
                "timeline_timestamp_utc": timeline_timestamp,
                "image_timestamp_utc": image_timestamp,
                "timeline_observations_utc": timeline_timestamp,
                "first_seen_at_utc": captured_at,
                "last_seen_at_utc": captured_at,
                "seen_count": "1",
                "timestamp_source": "rolling_slot",
                "camera_slot": "" if slot is None else str(slot),
                "page_name": page_name,
                "page_url": page_url,
                "source_name": source_name,
                "source_url": ref.url,
                "local_path": str(image_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "sha256": sha,
                "content_length": headers.get("Content-Length", str(len(body))),
                "last_modified": last_modified,
                "etag": headers.get("ETag", ""),
            }
            rows.append(row)
            known_keys.add(key)
            rows_by_key[key] = row
            new_count += 1

    rows = [normalize_row(row) for row in rows]
    write_timeline(csv_path, rows)

    return new_count, skipped_count


def write_html(
    path: Path,
    rows: list[dict[str, Any]],
    flights_path: Path | None = None,
    cameras_path: Path | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [normalize_row(row) for row in rows]
    rows = sorted(rows, key=lambda row: str(row.get("timeline_timestamp_utc", "")))
    cards = []
    frames = []
    flights_path = flights_path or PROJECT_ROOT / "data" / "flights.csv"
    cameras_path = cameras_path or PROJECT_ROOT / "data" / "cameras.csv"
    flight_rows = read_flights(flights_path)
    camera_configs = read_cameras(cameras_path)

    for index, row in enumerate(rows):
        local_path_raw = str(row.get("local_path", ""))
        local_path = html.escape(local_path_raw)
        timestamp = html.escape(str(row.get("timeline_timestamp_utc", "")))
        server_timestamp = html.escape(str(row.get("image_timestamp_utc", "")))
        source_name = html.escape(str(row.get("source_name", "")))
        page_name = html.escape(str(row.get("page_name", "")))
        source_url = html.escape(str(row.get("source_url", "")))
        sha = html.escape(str(row.get("sha256", ""))[:12])
        frames.append(
            {
                "src": f"../{local_path_raw}",
                "timestamp": str(row.get("timeline_timestamp_utc", "")),
                "serverTimestamp": str(row.get("image_timestamp_utc", "")),
                "pageName": str(row.get("page_name", "")),
                "sourceName": str(row.get("source_name", "")),
                "sourceUrl": str(row.get("source_url", "")),
                "sha": str(row.get("sha256", ""))[:12],
            }
        )
        cards.append(
            f"""
            <button class="thumb" type="button" data-index="{index}" title="{timestamp}" aria-label="Show {source_name} at {timestamp}">
              <img src="../{local_path}" loading="lazy" alt="{source_name}">
              <span>{page_name} / {source_name}</span>
            </button>
            """
        )

    frames_json = json.dumps(frames)
    flights_json = json.dumps(flight_rows)
    cameras_json = json.dumps(camera_configs)

    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Webcam Timeline</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #e9eef5;
      --panel: #ffffff;
      --panel-strong: #f7f9fc;
      --text: #172033;
      --muted: #617089;
      --line: #d6dfeb;
      --stage: #0d1320;
      --stage-panel: #151e2f;
      --accent: #2563eb;
      --accent-soft: rgba(37, 99, 235, 0.16);
      --shadow: 0 16px 40px rgba(24, 36, 58, 0.14);
      font-family: "Segoe UI", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    body.theme-dark {{
      color-scheme: dark;
      --bg: #0b1018;
      --panel: #121a26;
      --panel-strong: #172233;
      --text: #ecf2fb;
      --muted: #9aa8bd;
      --line: #263347;
      --stage: #05070c;
      --stage-panel: #0f1724;
      --accent: #60a5fa;
      --accent-soft: rgba(96, 165, 250, 0.18);
      --shadow: 0 18px 46px rgba(0, 0, 0, 0.34);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 18px;
      background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.12), transparent 28rem),
        var(--bg);
    }}
    header {{
      max-width: 1180px;
      margin: 0 auto 18px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      letter-spacing: 0;
    }}
    .sub {{
      margin: 0;
      color: var(--muted);
    }}
    .theme-toggle {{
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel);
      color: var(--text);
      cursor: pointer;
      padding: 0 14px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
    }}
    .player {{
      max-width: 1180px;
      margin: 0 auto;
      display: grid;
      gap: 12px;
    }}
    .stage {{
      background: var(--stage);
      border: 1px solid var(--line);
      border-radius: 10px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}
    .stage-images {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 1px;
      background: var(--line);
    }}
    .stage.side-by-side .stage-images {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .stage-view {{
      position: relative;
      background: var(--stage);
    }}
    .stage-view[hidden] {{
      display: none;
    }}
    .stage img {{
      width: 100%;
      display: block;
      aspect-ratio: 16 / 9;
      object-fit: contain;
      background: var(--stage);
    }}
    .plane-overlay {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
    }}
    .analysis-status {{
      position: absolute;
      right: 10px;
      bottom: 10px;
      border-radius: 999px;
      background: rgba(5, 7, 12, 0.72);
      color: #eef3f8;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 700;
    }}
    .analysis-status:empty {{
      display: none;
    }}
    .stage-label {{
      position: absolute;
      left: 10px;
      top: 10px;
      border-radius: 999px;
      background: rgba(5, 7, 12, 0.72);
      color: #eef3f8;
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 700;
    }}
    .stage-empty {{
      display: none;
      place-items: center;
      min-height: min(42vw, 360px);
      aspect-ratio: 16 / 9;
      color: #aeb9cc;
      background: repeating-linear-gradient(
        135deg,
        rgba(255, 255, 255, 0.04),
        rgba(255, 255, 255, 0.04) 10px,
        rgba(255, 255, 255, 0.08) 10px,
        rgba(255, 255, 255, 0.08) 20px
      );
      font-weight: 700;
    }}
    .stage-view.is-blank img {{
      display: none;
    }}
    .stage-view.is-blank .stage-empty {{
      display: grid;
    }}
    .details {{
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 16px;
      padding: 14px 16px;
      color: #eef3f8;
      background: var(--stage-panel);
      font-size: 14px;
    }}
    .details strong {{
      display: block;
      margin-bottom: 3px;
      font-size: 16px;
    }}
    .details code {{
      color: #bac6d8;
    }}
    .details a {{
      color: #9dc4ff;
    }}
    .controls {{
      position: sticky;
      top: 10px;
      z-index: 5;
      display: grid;
      grid-template-columns: auto auto auto 1fr repeat(5, auto);
      gap: 10px;
      align-items: center;
      background: color-mix(in srgb, var(--panel) 92%, transparent);
      backdrop-filter: blur(14px);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.1);
    }}
    button, select {{
      font: inherit;
    }}
    .control-button {{
      height: 38px;
      min-width: 58px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-strong);
      color: var(--text);
      cursor: pointer;
      font-weight: 700;
    }}
    .control-button:hover {{
      border-color: var(--accent);
      background: var(--accent-soft);
    }}
    .scrubber {{
      width: 100%;
      accent-color: var(--accent);
    }}
    .counter {{
      color: var(--muted);
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .speed {{
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 0 8px;
    }}
    .date-filter, .page-filter {{
      height: 38px;
      min-width: 150px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 0 8px;
    }}
    .analysis-toggle {{
      height: 38px;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--text);
      padding: 0 10px;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 700;
    }}
    .analysis-toggle input {{
      accent-color: var(--accent);
    }}
    .flight-panel {{
      display: grid;
      gap: 10px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
    }}
    .flight-header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
    }}
    .flight-header strong {{
      color: var(--text);
      font-size: 15px;
    }}
    .flight-list {{
      display: grid;
      gap: 8px;
    }}
    .flight-empty {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .flight-card {{
      display: grid;
      grid-template-columns: 150px 1fr auto;
      gap: 10px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel-strong);
      padding: 10px;
      font-size: 13px;
    }}
    .flight-card strong {{
      color: var(--text);
      font-size: 14px;
    }}
    .flight-card span {{
      color: var(--muted);
    }}
    .flight-card.is-target {{
      border-color: #f59e0b;
      box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.22);
      background: color-mix(in srgb, var(--panel-strong) 82%, #f59e0b);
    }}
    .timeline-shell {{
      max-width: 1180px;
      margin: 4px auto 0;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--panel);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
      overflow: hidden;
    }}
    .timeline-header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 13px;
    }}
    .thumbs {{
      display: flex;
      gap: 10px;
      overflow-x: auto;
      overflow-y: hidden;
      padding: 12px;
      scroll-behavior: smooth;
      scrollbar-width: thin;
    }}
    .thumb {{
      position: relative;
      flex: 0 0 132px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 7px;
      overflow: hidden;
      padding: 0;
      text-align: left;
      cursor: pointer;
      color: inherit;
    }}
    .thumb.is-active {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-soft);
    }}
    .thumb img {{
      width: 100%;
      display: block;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      background: var(--line);
    }}
    .thumb span {{
      display: block;
      padding: 6px 8px 7px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }}
    a {{
      color: var(--accent);
      text-decoration: none;
    }}
    code {{
      color: var(--muted);
      font-size: 12px;
    }}
    @media (max-width: 720px) {{
      body {{
        padding: 12px;
      }}
      header {{
        align-items: start;
        flex-direction: column;
      }}
      .controls {{
        grid-template-columns: repeat(3, auto);
      }}
      .scrubber, .counter, .speed, .date-filter, .page-filter, .analysis-toggle {{
        grid-column: 1 / -1;
      }}
      .details {{
        display: grid;
      }}
      .flight-card {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Webcam Timeline</h1>
      <p class="sub">{len(rows)} unique image snapshots</p>
    </div>
    <button class="theme-toggle" id="themeToggle" type="button">Dark mode</button>
  </header>
  <main class="player">
    <section class="stage" aria-label="Timeline player">
      <div class="stage-images">
        <div class="stage-view" id="stageViewPrimary">
          <img id="stageImage" alt="">
          <canvas class="plane-overlay" id="planeOverlayPrimary"></canvas>
          <span class="stage-empty" id="stageEmptyPrimary">No image yet</span>
          <span class="stage-label" id="stageLabelPrimary"></span>
          <span class="analysis-status" id="analysisStatusPrimary"></span>
        </div>
        <div class="stage-view" id="stageViewSecondary" hidden>
          <img id="stageImageSecondary" alt="">
          <canvas class="plane-overlay" id="planeOverlaySecondary"></canvas>
          <span class="stage-empty" id="stageEmptySecondary">No image yet</span>
          <span class="stage-label" id="stageLabelSecondary"></span>
          <span class="analysis-status" id="analysisStatusSecondary"></span>
        </div>
      </div>
      <div class="details">
        <div>
          <strong id="stageTime"></strong>
          <span id="stageSource"></span>
          <span id="stageServerTime"></span>
        </div>
        <div>
          <a id="stageLink" href="#" target="_blank" rel="noreferrer">source</a>
          <code id="stageSha"></code>
        </div>
      </div>
    </section>
    <section class="controls" aria-label="Playback controls">
      <button class="control-button" id="prevButton" type="button" title="Previous frame">Prev</button>
      <button class="control-button" id="playButton" type="button" title="Play or pause">Play</button>
      <button class="control-button" id="nextButton" type="button" title="Next frame">Next</button>
      <input class="scrubber" id="scrubber" type="range" min="0" max="{max(len(rows) - 1, 0)}" value="0" step="1" aria-label="Timeline position">
      <select class="speed" id="speed" aria-label="Playback speed">
        <option value="1500">Slow</option>
        <option value="800" selected>Normal</option>
        <option value="350">Fast</option>
        <option value="150">Very fast</option>
      </select>
      <select class="date-filter" id="dateFilter" aria-label="Filter by date">
        <option value="all">All dates</option>
      </select>
      <select class="page-filter" id="pageFilter" aria-label="Filter by webcam">
        <option value="all">All webcams</option>
      </select>
      <label class="analysis-toggle" title="Highlight likely aircraft by comparing nearby frames">
        <input id="aircraftOverlayToggle" type="checkbox">
        Flight match
      </label>
      <label class="analysis-toggle" title="Find visible aircraft shapes in the current image">
        <input id="visibleAircraftToggle" type="checkbox">
        Visible aircraft
      </label>
      <span class="counter" id="counter"></span>
    </section>
    <section class="flight-panel" aria-label="Flight information">
      <div class="flight-header">
        <strong>Flight info</strong>
        <span id="flightWindowLabel"></span>
      </div>
      <div class="flight-list" id="flightList"></div>
    </section>
  </main>
  <section class="timeline-shell" aria-label="Timeline thumbnails">
    <div class="timeline-header">
      <strong>Timeline</strong>
      <span>Oldest to newest</span>
    </div>
    <div class="thumbs" id="thumbs">
      {''.join(cards)}
    </div>
  </section>
  <script>
    const frames = {frames_json};
    const flights = {flights_json};
    const cameraConfigs = {cameras_json};
    const FLIGHT_WINDOW_MINUTES = 10;
    const TFJS_URL = "https://cdn.jsdelivr.net/npm/@tensorflow/tfjs@4.22.0/dist/tf.min.js";
    const COCO_SSD_URL = "https://cdn.jsdelivr.net/npm/@tensorflow-models/coco-ssd@2.2.3/dist/coco-ssd.min.js";
    const TARGET_ICAO = "{TARGET_ICAO}";
    const stage = document.querySelector(".stage");
    const themeToggle = document.getElementById("themeToggle");
    const stageViewPrimary = document.getElementById("stageViewPrimary");
    const stageViewSecondary = document.getElementById("stageViewSecondary");
    const stageImage = document.getElementById("stageImage");
    const stageImageSecondary = document.getElementById("stageImageSecondary");
    const planeOverlayPrimary = document.getElementById("planeOverlayPrimary");
    const planeOverlaySecondary = document.getElementById("planeOverlaySecondary");
    const analysisStatusPrimary = document.getElementById("analysisStatusPrimary");
    const analysisStatusSecondary = document.getElementById("analysisStatusSecondary");
    const stageEmptyPrimary = document.getElementById("stageEmptyPrimary");
    const stageEmptySecondary = document.getElementById("stageEmptySecondary");
    const stageLabelPrimary = document.getElementById("stageLabelPrimary");
    const stageLabelSecondary = document.getElementById("stageLabelSecondary");
    const stageTime = document.getElementById("stageTime");
    const stageSource = document.getElementById("stageSource");
    const stageServerTime = document.getElementById("stageServerTime");
    const stageLink = document.getElementById("stageLink");
    const stageSha = document.getElementById("stageSha");
    const scrubber = document.getElementById("scrubber");
    const counter = document.getElementById("counter");
    const playButton = document.getElementById("playButton");
    const prevButton = document.getElementById("prevButton");
    const nextButton = document.getElementById("nextButton");
    const speed = document.getElementById("speed");
    const dateFilter = document.getElementById("dateFilter");
    const pageFilter = document.getElementById("pageFilter");
    const aircraftOverlayToggle = document.getElementById("aircraftOverlayToggle");
    const visibleAircraftToggle = document.getElementById("visibleAircraftToggle");
    const flightList = document.getElementById("flightList");
    const flightWindowLabel = document.getElementById("flightWindowLabel");
    const thumbs = Array.from(document.querySelectorAll(".thumb"));
    let index = 0;
    let visiblePosition = 0;
    let visibleIndexes = frames.map((_, frameIndex) => frameIndex);
    let visibleItems = visibleIndexes.map((frameIndex) => ({{
      timestamp: frames[frameIndex]?.timestamp || "",
      anchorIndex: frameIndex,
      indexes: [frameIndex],
      slots: [frameIndex],
    }}));
    let timer = null;
    let aircraftModelPromise = null;

    function localDateKey(timestamp) {{
      const date = new Date(timestamp);
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${{year}}-${{month}}-${{day}}`;
    }}

    function minuteKey(timestamp) {{
      return String(timestamp || "").slice(0, 16);
    }}

    function minuteTimestamp(key) {{
      return `${{key}}:00+00:00`;
    }}

    function webcamPages() {{
      return Array.from(new Set(frames.map((frame) => frame.pageName))).filter(Boolean).sort();
    }}

    function latestFrameForPageAt(pageName, timestamp, items) {{
      let latest = null;
      items.forEach((item) => {{
        if (item.frame.pageName !== pageName) return;
        if (item.frame.timestamp > timestamp) return;
        if (latest === null || item.frame.timestamp > frames[latest].timestamp) {{
          latest = item.frameIndex;
        }}
      }});
      return latest;
    }}

    function previousFrameFor(frame) {{
      if (!frame) return null;
      let previous = null;
      frames.forEach((candidate) => {{
        if (candidate.pageName !== frame.pageName) return;
        if (candidate.timestamp >= frame.timestamp) return;
        if (!previous || candidate.timestamp > previous.timestamp) previous = candidate;
      }});
      return previous;
    }}

    function clearOverlay(canvas, status) {{
      const context = canvas.getContext("2d");
      context.clearRect(0, 0, canvas.width || 1, canvas.height || 1);
      status.textContent = "";
    }}

    function loadAnalysisImage(src) {{
      return new Promise((resolve, reject) => {{
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = reject;
        image.src = src;
      }});
    }}

    function loadScript(src) {{
      return new Promise((resolve, reject) => {{
        const existing = document.querySelector(`script[src="${{src}}"]`);
        if (existing) {{
          existing.addEventListener("load", resolve, {{ once: true }});
          existing.addEventListener("error", reject, {{ once: true }});
          if (existing.dataset.loaded === "true") resolve();
          return;
        }}

        const script = document.createElement("script");
        script.src = src;
        script.async = true;
        script.onload = () => {{
          script.dataset.loaded = "true";
          resolve();
        }};
        script.onerror = reject;
        document.head.appendChild(script);
      }});
    }}

    function loadAircraftModel() {{
      if (!aircraftModelPromise) {{
        aircraftModelPromise = Promise.resolve()
          .then(() => loadScript(TFJS_URL))
          .then(() => loadScript(COCO_SSD_URL))
          .then(() => window.cocoSsd.load());
      }}
      return aircraftModelPromise;
    }}

    async function findModelAircraftCandidates(image) {{
      const model = await loadAircraftModel();
      const predictions = await model.detect(image);

      return predictions
        .filter((prediction) => prediction.class === "airplane" && prediction.score >= 0.32)
        .map((prediction) => {{
          const [x, y, width, height] = prediction.bbox;
          return {{
            x,
            y,
            width,
            height,
            score: prediction.score,
            label: `AI airplane ${{Math.round(prediction.score * 100)}}%`,
            stroke: "#38bdf8",
            fill: "rgba(56, 189, 248, 0.16)",
            labelText: "#e0f2fe",
          }};
        }})
        .sort((a, b) => b.score - a.score)
        .slice(0, 8);
    }}

    function drawCandidateBoxes(canvas, imageElement, candidates) {{
      const naturalWidth = imageElement.naturalWidth || imageElement.width || 1;
      const naturalHeight = imageElement.naturalHeight || imageElement.height || 1;
      const displayRect = imageElement.getBoundingClientRect();
      const parentRect = imageElement.parentElement.getBoundingClientRect();
      const imageAspect = naturalWidth / naturalHeight;
      const boxAspect = displayRect.width / displayRect.height;
      let drawnWidth = displayRect.width;
      let drawnHeight = displayRect.height;
      let offsetX = displayRect.left - parentRect.left;
      let offsetY = displayRect.top - parentRect.top;

      if (boxAspect > imageAspect) {{
        drawnWidth = displayRect.height * imageAspect;
        offsetX += (displayRect.width - drawnWidth) / 2;
      }} else {{
        drawnHeight = displayRect.width / imageAspect;
        offsetY += (displayRect.height - drawnHeight) / 2;
      }}

      canvas.width = Math.max(1, Math.round(parentRect.width));
      canvas.height = Math.max(1, Math.round(parentRect.height));
      const context = canvas.getContext("2d");
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.lineWidth = Math.max(2, canvas.width / 420);
      context.font = `${{Math.max(12, Math.round(canvas.width / 90))}}px Segoe UI, Arial, sans-serif`;
      context.textBaseline = "top";

      candidates.forEach((candidate) => {{
        const stroke = candidate.stroke || "#fbbf24";
        const fill = candidate.fill || "rgba(251, 191, 36, 0.16)";
        const labelFill = candidate.labelFill || "rgba(5, 7, 12, 0.82)";
        const labelText = candidate.labelText || "#fef3c7";
        const x = offsetX + candidate.x * drawnWidth / naturalWidth;
        const y = offsetY + candidate.y * drawnHeight / naturalHeight;
        const width = candidate.width * drawnWidth / naturalWidth;
        const height = candidate.height * drawnHeight / naturalHeight;
        const label = candidate.label || "Aircraft";
        const labelWidth = Math.min(context.measureText(label).width + 12, canvas.width - 8);
        const labelY = Math.max(4, y - 24);
        context.strokeStyle = stroke;
        context.fillStyle = fill;
        context.fillRect(x, y, width, height);
        context.strokeRect(x, y, width, height);
        context.fillStyle = labelFill;
        context.fillRect(x, labelY, labelWidth, 20);
        context.fillStyle = labelText;
        context.fillText(label, x + 6, labelY + 3, labelWidth - 12);
      }});
    }}

    function findMovingCandidates(currentImage, previousImage) {{
      const sourceWidth = currentImage.naturalWidth || currentImage.width;
      const sourceHeight = currentImage.naturalHeight || currentImage.height;
      const scale = Math.min(1, 360 / sourceWidth);
      const width = Math.max(1, Math.round(sourceWidth * scale));
      const height = Math.max(1, Math.round(sourceHeight * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d", {{ willReadFrequently: true }});

      context.drawImage(currentImage, 0, 0, width, height);
      const current = context.getImageData(0, 0, width, height).data;
      context.clearRect(0, 0, width, height);
      context.drawImage(previousImage, 0, 0, width, height);
      const previous = context.getImageData(0, 0, width, height).data;

      const startY = Math.round(height * 0.06);
      const endY = Math.round(height * 0.84);
      const changed = new Uint8Array(width * height);

      for (let y = startY; y < endY; y += 1) {{
        for (let x = 0; x < width; x += 1) {{
          const offset = (y * width + x) * 4;
          const currentLuma = current[offset] * 0.299 + current[offset + 1] * 0.587 + current[offset + 2] * 0.114;
          const previousLuma = previous[offset] * 0.299 + previous[offset + 1] * 0.587 + previous[offset + 2] * 0.114;
          const diff = Math.abs(currentLuma - previousLuma);
          const localContrast = Math.abs(current[offset] - current[offset + 1]) + Math.abs(current[offset + 1] - current[offset + 2]);

          if (diff > 34 || (diff > 24 && localContrast > 35)) {{
            changed[y * width + x] = 1;
          }}
        }}
      }}

      const visited = new Uint8Array(width * height);
      const candidates = [];
      const queue = [];
      const directions = [[1, 0], [-1, 0], [0, 1], [0, -1]];

      for (let y = startY; y < endY; y += 1) {{
        for (let x = 0; x < width; x += 1) {{
          const index = y * width + x;
          if (!changed[index] || visited[index]) continue;

          let minX = x;
          let maxX = x;
          let minY = y;
          let maxY = y;
          let count = 0;
          queue.length = 0;
          queue.push([x, y]);
          visited[index] = 1;

          while (queue.length) {{
            const [nextX, nextY] = queue.pop();
            count += 1;
            minX = Math.min(minX, nextX);
            maxX = Math.max(maxX, nextX);
            minY = Math.min(minY, nextY);
            maxY = Math.max(maxY, nextY);

            directions.forEach(([dx, dy]) => {{
              const checkX = nextX + dx;
              const checkY = nextY + dy;
              if (checkX < 0 || checkX >= width || checkY < startY || checkY >= endY) return;
              const checkIndex = checkY * width + checkX;
              if (!changed[checkIndex] || visited[checkIndex]) return;
              visited[checkIndex] = 1;
              queue.push([checkX, checkY]);
            }});
          }}

          const boxWidth = maxX - minX + 1;
          const boxHeight = maxY - minY + 1;
          const area = boxWidth * boxHeight;
          const density = count / area;

          if (count < 3 || count > 650) continue;
          if (boxWidth > width * 0.24 || boxHeight > height * 0.24) continue;
          if (density < 0.08) continue;

          const pad = Math.max(6, Math.round(Math.max(boxWidth, boxHeight) * 1.4));
          candidates.push({{
            x: Math.max(0, (minX - pad) / scale),
            y: Math.max(0, (minY - pad) / scale),
            width: Math.min(sourceWidth, (boxWidth + pad * 2) / scale),
            height: Math.min(sourceHeight, (boxHeight + pad * 2) / scale),
            score: count,
          }});
        }}
      }}

      return candidates
        .sort((a, b) => b.score - a.score)
        .slice(0, 6);
    }}

    function findShapeAircraftCandidates(image) {{
      const sourceWidth = image.naturalWidth || image.width;
      const sourceHeight = image.naturalHeight || image.height;
      const scale = Math.min(1, 420 / sourceWidth);
      const width = Math.max(1, Math.round(sourceWidth * scale));
      const height = Math.max(1, Math.round(sourceHeight * scale));
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext("2d", {{ willReadFrequently: true }});
      context.drawImage(image, 0, 0, width, height);
      const pixels = context.getImageData(0, 0, width, height).data;
      const startY = Math.round(height * 0.16);
      const endY = Math.round(height * 0.90);
      const edge = new Uint8Array(width * height);

      for (let y = startY + 1; y < endY - 1; y += 1) {{
        for (let x = 1; x < width - 1; x += 1) {{
          const offset = (y * width + x) * 4;
          const left = (y * width + x - 1) * 4;
          const right = (y * width + x + 1) * 4;
          const up = ((y - 1) * width + x) * 4;
          const down = ((y + 1) * width + x) * 4;
          const luma = pixels[offset] * 0.299 + pixels[offset + 1] * 0.587 + pixels[offset + 2] * 0.114;
          const gx = Math.abs(
            (pixels[right] * 0.299 + pixels[right + 1] * 0.587 + pixels[right + 2] * 0.114) -
            (pixels[left] * 0.299 + pixels[left + 1] * 0.587 + pixels[left + 2] * 0.114)
          );
          const gy = Math.abs(
            (pixels[down] * 0.299 + pixels[down + 1] * 0.587 + pixels[down + 2] * 0.114) -
            (pixels[up] * 0.299 + pixels[up + 1] * 0.587 + pixels[up + 2] * 0.114)
          );
          const colorContrast = Math.abs(pixels[offset] - pixels[offset + 1]) + Math.abs(pixels[offset + 1] - pixels[offset + 2]);

          if (gx + gy > 46 || (gx + gy > 30 && (luma < 80 || luma > 165 || colorContrast > 38))) {{
            edge[y * width + x] = 1;
          }}
        }}
      }}

      const dilated = new Uint8Array(width * height);
      for (let y = startY; y < endY; y += 1) {{
        for (let x = 0; x < width; x += 1) {{
          if (!edge[y * width + x]) continue;
          for (let dy = -2; dy <= 2; dy += 1) {{
            for (let dx = -2; dx <= 2; dx += 1) {{
              const nx = x + dx;
              const ny = y + dy;
              if (nx < 0 || nx >= width || ny < startY || ny >= endY) continue;
              dilated[ny * width + nx] = 1;
            }}
          }}
        }}
      }}

      const visited = new Uint8Array(width * height);
      const queue = [];
      const directions = [[1, 0], [-1, 0], [0, 1], [0, -1]];
      const candidates = [];

      for (let y = startY; y < endY; y += 1) {{
        for (let x = 0; x < width; x += 1) {{
          const index = y * width + x;
          if (!dilated[index] || visited[index]) continue;

          let minX = x;
          let maxX = x;
          let minY = y;
          let maxY = y;
          let count = 0;
          queue.length = 0;
          queue.push([x, y]);
          visited[index] = 1;

          while (queue.length) {{
            const [nextX, nextY] = queue.pop();
            count += 1;
            minX = Math.min(minX, nextX);
            maxX = Math.max(maxX, nextX);
            minY = Math.min(minY, nextY);
            maxY = Math.max(maxY, nextY);

            directions.forEach(([dx, dy]) => {{
              const checkX = nextX + dx;
              const checkY = nextY + dy;
              if (checkX < 0 || checkX >= width || checkY < startY || checkY >= endY) return;
              const checkIndex = checkY * width + checkX;
              if (!dilated[checkIndex] || visited[checkIndex]) return;
              visited[checkIndex] = 1;
              queue.push([checkX, checkY]);
            }});
          }}

          const boxWidth = maxX - minX + 1;
          const boxHeight = maxY - minY + 1;
          const area = boxWidth * boxHeight;
          const aspect = Math.max(boxWidth, boxHeight) / Math.max(1, Math.min(boxWidth, boxHeight));
          const density = count / area;

          if (count < 18 || count > 5200) continue;
          if (boxWidth < 10 || boxHeight < 6) continue;
          if (boxWidth > width * 0.46 || boxHeight > height * 0.42) continue;
          if (aspect < 1.05 || aspect > 8.0) continue;
          if (density < 0.10) continue;

          candidates.push({{
            x: Math.max(0, (minX - 5) / scale),
            y: Math.max(0, (minY - 5) / scale),
            width: Math.min(sourceWidth, (boxWidth + 10) / scale),
            height: Math.min(sourceHeight, (boxHeight + 10) / scale),
            score: count * aspect,
            label: "Visible aircraft",
            stroke: "#22d3ee",
            fill: "rgba(34, 211, 238, 0.14)",
            labelText: "#cffafe",
          }});
        }}
      }}

      return candidates
        .sort((a, b) => b.score - a.score)
        .slice(0, 8);
    }}

    async function findVisibleAircraftCandidates(image) {{
      try {{
        const modelCandidates = await findModelAircraftCandidates(image);
        if (modelCandidates.length) {{
          return {{ candidates: modelCandidates, source: "AI" }};
        }}
      }} catch (_error) {{
      }}

      return {{
        candidates: findShapeAircraftCandidates(image),
        source: "shape",
      }};
    }}

    function matchingFlightsForTimestamp(timestamp) {{
      const frameTime = parseTimestamp(timestamp);
      if (!frameTime) return [];

      return flights
        .map((row) => {{
          const time = flightTime(row);
          const diffMinutes = time ? Math.abs(time.getTime() - frameTime.getTime()) / 60000 : Infinity;
          return {{ row, time, diffMinutes }};
        }})
        .filter((item) => item.time && item.diffMinutes <= FLIGHT_WINDOW_MINUTES)
        .sort((a, b) => a.diffMinutes - b.diffMinutes);
    }}

    function numericValue(value) {{
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }}

    function radians(degrees) {{
      return degrees * Math.PI / 180;
    }}

    function degrees(radiansValue) {{
      return radiansValue * 180 / Math.PI;
    }}

    function bearingDeg(lat1, lon1, lat2, lon2) {{
      const phi1 = radians(lat1);
      const phi2 = radians(lat2);
      const deltaLon = radians(lon2 - lon1);
      const y = Math.sin(deltaLon) * Math.cos(phi2);
      const x = Math.cos(phi1) * Math.sin(phi2) -
        Math.sin(phi1) * Math.cos(phi2) * Math.cos(deltaLon);
      return (degrees(Math.atan2(y, x)) + 360) % 360;
    }}

    function distanceMeters(lat1, lon1, lat2, lon2) {{
      const radiusMeters = 6371000;
      const phi1 = radians(lat1);
      const phi2 = radians(lat2);
      const deltaPhi = radians(lat2 - lat1);
      const deltaLon = radians(lon2 - lon1);
      const a = Math.sin(deltaPhi / 2) ** 2 +
        Math.cos(phi1) * Math.cos(phi2) * Math.sin(deltaLon / 2) ** 2;
      return radiusMeters * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    }}

    function angleDeltaDeg(angle, reference) {{
      return ((((angle - reference) % 360) + 540) % 360) - 180;
    }}

    function cameraForFrame(frame) {{
      if (!frame) return null;
      return cameraConfigs[frame.pageName] || cameraConfigs[frame.sourceName] || null;
    }}

    function projectedFlightPoint(row, frame) {{
      const camera = cameraForFrame(frame);
      if (!camera) return null;

      const cameraLat = numericValue(camera.latitude);
      const cameraLon = numericValue(camera.longitude);
      const aircraftLat = numericValue(row.latitude);
      const aircraftLon = numericValue(row.longitude);
      const heading = numericValue(camera.heading_deg);
      const horizontalFov = numericValue(camera.horizontal_fov_deg) || 70;
      const verticalFov = numericValue(camera.vertical_fov_deg) || 40;
      const pitch = numericValue(camera.pitch_deg) || 0;

      if ([cameraLat, cameraLon, aircraftLat, aircraftLon, heading].some((value) => value === null)) {{
        return null;
      }}

      const bearing = bearingDeg(cameraLat, cameraLon, aircraftLat, aircraftLon);
      const relativeBearing = angleDeltaDeg(bearing, heading);
      const xNorm = 0.5 + relativeBearing / horizontalFov;

      let yNorm = 0.5;
      const altitudeFt = String(row.altitude_ft || "").toLowerCase() === "ground"
        ? 0
        : numericValue(row.altitude_ft);
      const cameraElevationFt = numericValue(camera.elevation_ft) || 0;
      if (altitudeFt !== null) {{
        const rangeMeters = Math.max(1, distanceMeters(cameraLat, cameraLon, aircraftLat, aircraftLon));
        const heightMeters = (altitudeFt - cameraElevationFt) * 0.3048;
        const elevationDeg = degrees(Math.atan2(heightMeters, rangeMeters));
        yNorm = 0.5 - (elevationDeg - pitch) / verticalFov;
      }}

      return {{
        xNorm,
        yNorm,
        bearing,
        relativeBearing,
        inView: xNorm >= -0.15 && xNorm <= 1.15 && yNorm >= -0.25 && yNorm <= 1.25,
      }};
    }}

    function projectedCandidateForFlight(flightMatch, frame, image) {{
      const projection = projectedFlightPoint(flightMatch.row, frame);
      if (!projection || !projection.inView) return null;

      const imageWidth = image.naturalWidth || image.width || 1;
      const imageHeight = image.naturalHeight || image.height || 1;
      const profile = aircraftProfile(flightMatch.row);
      const width = profile.group === "helicopter" ? 58 : 76;
      const height = profile.group === "helicopter" ? 46 : 34;

      return {{
        x: Math.max(0, Math.min(imageWidth - width, projection.xNorm * imageWidth - width / 2)),
        y: Math.max(0, Math.min(imageHeight - height, projection.yNorm * imageHeight - height / 2)),
        width,
        height,
        score: 0,
        matchScore: 0,
        label: `Expected ${{[flightLabel(flightMatch.row), flightMatch.row.aircraft_type || profile.group].filter(Boolean).join(" ")}}`,
      }};
    }}

    function aircraftProfile(row) {{
      const text = [
        row.aircraft_type,
        row.registration,
        row.callsign,
        row.notes,
      ].filter(Boolean).join(" ").toUpperCase();

      if (/(A109|H135|H145|EC|R44|R66|B06|HELI|ROTOR)/.test(text)) {{
        return {{ group: "helicopter", aspect: 1.35, minAspect: 0.9, maxAspect: 2.4, minArea: 16, maxArea: 760 }};
      }}

      if (/(A3|B7|E1|CRJ|EMB|JET|AIRBUS|BOEING)/.test(text)) {{
        return {{ group: "jet", aspect: 3.1, minAspect: 1.8, maxAspect: 7.5, minArea: 18, maxArea: 980 }};
      }}

      if (/(PC12|P28|PA28|C17|C15|C172|DA40|SR22|TBM|BE20|BN2|PIPER|CESSNA)/.test(text)) {{
        return {{ group: "fixed-wing", aspect: 2.35, minAspect: 1.65, maxAspect: 6.5, minArea: 8, maxArea: 720 }};
      }}

      return {{ group: "aircraft", aspect: 2.2, minAspect: 1.1, maxAspect: 7.0, minArea: 8, maxArea: 850 }};
    }}

    function scoreCandidateForFlight(candidate, flightMatch, frame, image) {{
      const row = flightMatch.row;
      const profile = aircraftProfile(row);
      const aspect = Math.max(candidate.width, candidate.height) / Math.max(1, Math.min(candidate.width, candidate.height));
      const area = candidate.width * candidate.height;
      const expectedArea = Math.max(profile.minArea, Math.min(profile.maxArea, area));
      const aspectScore = Math.max(0, 1 - Math.abs(aspect - profile.aspect) / Math.max(profile.aspect, 1));
      const areaScore = area >= profile.minArea && area <= profile.maxArea
        ? 1
        : Math.max(0, 1 - Math.abs(area - expectedArea) / Math.max(expectedArea, 1));
      const typeShapeScore = aspect >= profile.minAspect && aspect <= profile.maxAspect ? 1 : 0;

      if (!typeShapeScore && profile.group !== "aircraft") {{
        return -Infinity;
      }}

      const timeScore = Math.max(0, 1 - flightMatch.diffMinutes / FLIGHT_WINDOW_MINUTES);
      const motionScore = Math.min(1, candidate.score / 120);
      const projection = projectedFlightPoint(row, frame);

      if (!projection) {{
        return motionScore * 0.34 + aspectScore * 0.24 + areaScore * 0.16 + timeScore * 0.10 + typeShapeScore * 0.16;
      }}

      const imageWidth = image.naturalWidth || image.width || 1;
      const imageHeight = image.naturalHeight || image.height || 1;
      const candidateX = (candidate.x + candidate.width / 2) / imageWidth;
      const candidateY = (candidate.y + candidate.height / 2) / imageHeight;
      const dx = candidateX - projection.xNorm;
      const dy = candidateY - projection.yNorm;
      const projectionDistance = Math.sqrt(dx * dx + dy * dy);

      if (projection.inView && projectionDistance > 0.24) {{
        return -Infinity;
      }}

      const positionScore = Math.max(0, 1 - projectionDistance * 3.0);
      const viewScore = projection.inView ? 1 : 0.25;

      return motionScore * 0.26 +
        aspectScore * 0.14 +
        areaScore * 0.12 +
        timeScore * 0.12 +
        positionScore * 0.28 +
        typeShapeScore * 0.12 +
        viewScore * 0.04;
    }}

    function flightAwareCandidates(candidates, flightMatches, frame, image) {{
      const selected = [];
      const used = new Set();

      flightMatches.slice(0, 4).forEach((flightMatch) => {{
        let best = null;
        let bestIndex = -1;
        let bestScore = -Infinity;

        candidates.forEach((candidate, candidateIndex) => {{
          if (used.has(candidateIndex)) return;
          const score = scoreCandidateForFlight(candidate, flightMatch, frame, image);
          if (score > bestScore) {{
            best = candidate;
            bestIndex = candidateIndex;
            bestScore = score;
          }}
        }});

        if (best && bestScore >= 0.28) {{
          used.add(bestIndex);
          selected.push({{
            ...best,
            label: [
              flightLabel(flightMatch.row),
              flightMatch.row.aircraft_type || aircraftProfile(flightMatch.row).group,
            ].filter(Boolean).join(" "),
            matchScore: bestScore,
          }});
        }} else {{
          const projected = projectedCandidateForFlight(flightMatch, frame, image);
          if (projected) selected.push(projected);
        }}
      }});

      return selected.sort((a, b) => b.matchScore - a.matchScore);
    }}

    async function updateAircraftOverlay(elements, frame) {{
      if ((!aircraftOverlayToggle.checked && !visibleAircraftToggle.checked) || !frame) {{
        clearOverlay(elements.overlay, elements.status);
        return;
      }}

      const previous = aircraftOverlayToggle.checked ? previousFrameFor(frame) : null;
      const token = `${{frame.sha}}:${{previous?.sha || "shape-only"}}:${{aircraftOverlayToggle.checked}}:${{visibleAircraftToggle.checked}}`;
      elements.overlay.dataset.analysisToken = token;
      elements.status.textContent = "Analyzing";

      try {{
        const currentImage = await loadAnalysisImage(frame.src);

        if (elements.overlay.dataset.analysisToken !== token) return;

        const selected = [];
        const statusParts = [];

        if (visibleAircraftToggle.checked) {{
          const visibleResult = await findVisibleAircraftCandidates(currentImage);
          const visibleCandidates = visibleResult.candidates;
          selected.push(...visibleCandidates);
          statusParts.push(`${{visibleCandidates.length}} visible (${{visibleResult.source}})`);
        }}

        if (aircraftOverlayToggle.checked) {{
          const flightMatches = matchingFlightsForTimestamp(frame.timestamp);

          if (!flightMatches.length) {{
            statusParts.push("no matching flight");
          }} else if (!previous) {{
            statusParts.push("no earlier frame");
          }} else {{
            const previousImage = await loadAnalysisImage(previous.src);
            if (elements.overlay.dataset.analysisToken !== token) return;
            const motionCandidates = findMovingCandidates(currentImage, previousImage);
            const flightCandidates = flightAwareCandidates(motionCandidates, flightMatches, frame, currentImage);
            selected.push(...flightCandidates);
            const target = flightMatches[0]?.row;
            const targetLabel = target
              ? [flightLabel(target), target.aircraft_type || aircraftProfile(target).group].filter(Boolean).join(" ")
              : "flight";
            statusParts.push(flightCandidates.length ? `${{flightCandidates.length}} flight` : `no match for ${{targetLabel}}`);
          }}
        }}

        drawCandidateBoxes(elements.overlay, elements.image, selected);
        elements.status.textContent = statusParts.join(" | ");
      }} catch (_error) {{
        clearOverlay(elements.overlay, elements.status);
        elements.status.textContent = "Analysis failed";
      }}
    }}

    function parseTimestamp(timestamp) {{
      if (!timestamp) return null;
      const value = timestamp.endsWith("Z") || timestamp.includes("+") ? timestamp : `${{timestamp}}Z`;
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? null : date;
    }}

    function flightTime(row) {{
      return parseTimestamp(row.event_time_utc || row.timestamp_utc || row.time_utc || row.time || "");
    }}

    function flightLabel(row) {{
      return row.callsign || row.registration || row.flight || row.hex || "Unknown aircraft";
    }}

    function flightRoute(row) {{
      const origin = row.origin || row.from || "";
      const destination = row.destination || row.to || "";

      if (origin && destination) return `${{origin}} to ${{destination}}`;
      if (origin) return `from ${{origin}}`;
      if (destination) return `to ${{destination}}`;

      return row.direction || row.notes || "No route details";
    }}

    function formatFlightNumber(value, suffix, decimals = 0) {{
      if (value === null || value === undefined || value === "") return "";
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return "";
      return `${{numeric.toFixed(decimals)}} ${{suffix}}`;
    }}

    function flightDetailChips(row) {{
      const chips = [];
      const altitude = String(row.altitude_ft || "").toLowerCase() === "ground"
        ? "ground"
        : formatFlightNumber(row.altitude_ft, "ft");
      const speed = formatFlightNumber(row.groundspeed_kt, "kt", 1);
      const track = formatFlightNumber(row.track_deg || row.direction, "deg", 0);
      const verticalRate = formatFlightNumber(row.vertical_rate_fpm, "fpm", 0);
      const distance = formatFlightNumber(row.distance_nm, "nm", 2);

      if (altitude) chips.push(`alt ${{altitude}}`);
      if (speed) chips.push(`speed ${{speed}}`);
      if (track) chips.push(`track ${{track}}`);
      if (verticalRate) chips.push(`vertical ${{verticalRate}}`);
      if (distance) chips.push(`${{distance}} from EGPG`);
      if (row.squawk) chips.push(`squawk ${{row.squawk}}`);
      if (row.provider) chips.push(row.provider);

      return chips;
    }}

    function updateFlightsForFrame(frame) {{
      flightList.replaceChildren();
      const frameTime = parseTimestamp(frame.timestamp);
      flightWindowLabel.textContent = `within ${{FLIGHT_WINDOW_MINUTES}} min`;

      if (!frameTime) {{
        const empty = document.createElement("p");
        empty.className = "flight-empty";
        empty.textContent = "No valid timeline timestamp for flight matching.";
        flightList.appendChild(empty);
        return;
      }}

      const matches = flights
        .map((row) => {{
          const time = flightTime(row);
          const diffMinutes = time ? Math.abs(time.getTime() - frameTime.getTime()) / 60000 : Infinity;
          return {{ row, time, diffMinutes }};
        }})
        .filter((item) => item.time && item.diffMinutes <= FLIGHT_WINDOW_MINUTES)
        .sort((a, b) => a.diffMinutes - b.diffMinutes);

      if (!matches.length) {{
        const empty = document.createElement("p");
        empty.className = "flight-empty";
        empty.textContent = flights.length
          ? "No flight rows match this frame."
          : "No flight rows loaded. Add data/flights.csv to show aircraft near this timestamp.";
        flightList.appendChild(empty);
        return;
      }}

      matches.slice(0, 8).forEach((item) => {{
        const row = item.row;
        const card = document.createElement("article");
        card.className = "flight-card";
        if (String(row.hex || "").toUpperCase() === TARGET_ICAO) {{
          card.classList.add("is-target");
        }}

        const timeCell = document.createElement("strong");
        timeCell.textContent = item.time.toISOString().replace(".000Z", "Z");

        const detailCell = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = flightLabel(row);
        const meta = document.createElement("span");
        const aircraft = row.aircraft_type || row.type || row.registration || "";
        const icao = row.hex ? `ICAO ${{String(row.hex).toUpperCase()}}` : "";
        const route = flightRoute(row);
        meta.textContent = [icao, aircraft, route].filter(Boolean).join(" - ");
        const chips = document.createElement("span");
        chips.textContent = flightDetailChips(row).join(" | ");
        detailCell.append(title, document.createElement("br"), meta);
        if (chips.textContent) {{
          detailCell.append(document.createElement("br"), chips);
        }}

        const delta = document.createElement("span");
        delta.textContent = `${{item.diffMinutes.toFixed(1)}} min`;

        if (row.source_url) {{
          const link = document.createElement("a");
          link.href = row.source_url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = delta.textContent;
          card.append(timeCell, detailCell, link);
        }} else {{
          card.append(timeCell, detailCell, delta);
        }}

        flightList.appendChild(card);
      }});
    }}

    function populateDateFilter() {{
      const dates = Array.from(new Set(frames.map((frame) => localDateKey(frame.timestamp))));
      dates.forEach((date) => {{
        const option = document.createElement("option");
        option.value = date;
        option.textContent = date;
        dateFilter.appendChild(option);
      }});
    }}

    function populatePageFilter() {{
      const pages = Array.from(new Set(frames.map((frame) => frame.pageName))).filter(Boolean);
      pages.forEach((page) => {{
        const option = document.createElement("option");
        option.value = page;
        option.textContent = page;
        pageFilter.appendChild(option);
      }});
    }}

    function rebuildVisibleIndexes() {{
      const selectedDate = dateFilter.value;
      const selectedPage = pageFilter.value;
      const filtered = frames
        .map((frame, frameIndex) => ({{ frame, frameIndex }}))
        .filter((item) => selectedDate === "all" || localDateKey(item.frame.timestamp) === selectedDate)
        .filter((item) => selectedPage === "all" || item.frame.pageName === selectedPage);

      if (selectedPage === "all") {{
        const pages = webcamPages();
        const groups = Array.from(new Set(filtered.map((item) => minuteKey(item.frame.timestamp))))
          .sort()
          .map((key) => {{
            const timestamp = minuteTimestamp(key);
            const slots = pages.map((pageName) => latestFrameForPageAt(pageName, timestamp, filtered));
            const indexes = slots.filter((frameIndex) => frameIndex !== null);
            return {{ timestamp, indexes, slots, pages }};
          }});
        visibleItems = groups
          .map((item) => {{
            item.anchorIndex = item.indexes[0] ?? -1;
            return item;
          }})
          .sort((a, b) => a.timestamp.localeCompare(b.timestamp));
      }} else {{
        visibleItems = filtered.map((item) => ({{
          timestamp: item.frame.timestamp,
          anchorIndex: item.frameIndex,
          indexes: [item.frameIndex],
          slots: [item.frameIndex],
        }}));
      }}

      visibleIndexes = Array.from(new Set(visibleItems.flatMap((item) => item.indexes)));

      thumbs.forEach((thumb, thumbIndex) => {{
        thumb.hidden = !visibleIndexes.includes(thumbIndex);
      }});

      scrubber.max = String(Math.max(visibleItems.length - 1, 0));
      scrubber.disabled = visibleItems.length === 0;
      prevButton.disabled = visibleItems.length === 0;
      nextButton.disabled = visibleItems.length === 0;
      playButton.disabled = visibleItems.length === 0;
    }}

    function setTheme(theme) {{
      const isDark = theme === "dark";
      document.body.classList.toggle("theme-dark", isDark);
      themeToggle.textContent = isDark ? "Light mode" : "Dark mode";
      window.localStorage.setItem("webcamTimelineTheme", theme);
    }}

    function renderStageSlot(slot, elements, fallbackLabel) {{
      const frame = slot === null || slot === undefined || slot < 0 ? null : frames[slot];
      elements.view.classList.toggle("is-blank", !frame);
      elements.label.textContent = frame ? frame.pageName : fallbackLabel;
      elements.empty.textContent = `No ${{fallbackLabel}} image yet`;

      if (frame) {{
        elements.image.src = frame.src;
        elements.image.alt = frame.sourceName;
        updateAircraftOverlay(elements, frame);
      }} else {{
        elements.image.removeAttribute("src");
        elements.image.alt = "";
        clearOverlay(elements.overlay, elements.status);
      }}

      return frame;
    }}

    function setVisiblePosition(nextPosition) {{
      if (!visibleItems.length) {{
        counter.textContent = "0 / 0";
        return;
      }}

      visiblePosition = Math.max(0, Math.min(visibleItems.length - 1, nextPosition));
      const item = visibleItems[visiblePosition];
      index = item.anchorIndex;
      const isSynced = pageFilter.value === "all";
      const pages = item.pages || [];
      const primaryFrame = renderStageSlot(item.slots[0], {{
        view: stageViewPrimary,
        image: stageImage,
        overlay: planeOverlayPrimary,
        status: analysisStatusPrimary,
        empty: stageEmptyPrimary,
        label: stageLabelPrimary,
      }}, pages[0] || "Webcam 1");
      const secondaryFrame = isSynced ? renderStageSlot(item.slots[1], {{
        view: stageViewSecondary,
        image: stageImageSecondary,
        overlay: planeOverlaySecondary,
        status: analysisStatusSecondary,
        empty: stageEmptySecondary,
        label: stageLabelSecondary,
      }}, pages[1] || "Webcam 2") : null;
      if (!isSynced) {{
        clearOverlay(planeOverlaySecondary, analysisStatusSecondary);
      }}
      const firstFrame = primaryFrame || secondaryFrame;

      stage.classList.toggle("side-by-side", isSynced);
      stageViewSecondary.hidden = !isSynced;

      stageTime.textContent = isSynced ? item.timestamp : firstFrame.timestamp;
      stageSource.textContent = isSynced
        ? (pages.length ? pages.join(" + ") : "Synced webcams")
        : `${{firstFrame.pageName}} / ${{firstFrame.sourceName}}`;
      stageServerTime.textContent = isSynced
        ? `synced minute: ${{minuteKey(item.timestamp)}}`
        : `server: ${{firstFrame.serverTimestamp}}`;
      stageLink.href = firstFrame?.sourceUrl || "#";
      stageSha.textContent = firstFrame?.sha || "";
      updateFlightsForFrame({{ timestamp: isSynced ? item.timestamp : firstFrame.timestamp }});
      scrubber.value = String(visiblePosition);
      counter.textContent = `${{visiblePosition + 1}} / ${{visibleItems.length}}`;
      thumbs.forEach((thumb, thumbIndex) => {{
        thumb.classList.toggle("is-active", item.indexes.includes(thumbIndex));
      }});
    }}

    function setIndex(nextIndex) {{
      const nextVisiblePosition = visibleItems.findIndex((item) => item.indexes.includes(nextIndex));
      setVisiblePosition(nextVisiblePosition === -1 ? 0 : nextVisiblePosition);
    }}

    function stop() {{
      if (timer) {{
        window.clearInterval(timer);
        timer = null;
      }}
      playButton.textContent = "Play";
    }}

    function play() {{
      stop();
      playButton.textContent = "Pause";
      timer = window.setInterval(() => {{
        if (visiblePosition >= visibleItems.length - 1) {{
          stop();
          return;
        }}
        setVisiblePosition(visiblePosition + 1);
      }}, Number(speed.value));
    }}

    playButton.addEventListener("click", () => {{
      if (timer) {{
        stop();
      }} else {{
        play();
      }}
    }});
    prevButton.addEventListener("click", () => {{
      stop();
      setVisiblePosition(visiblePosition - 1);
    }});
    nextButton.addEventListener("click", () => {{
      stop();
      setVisiblePosition(visiblePosition + 1);
    }});
    scrubber.addEventListener("input", () => {{
      stop();
      setVisiblePosition(Number(scrubber.value));
    }});
    speed.addEventListener("change", () => {{
      if (timer) play();
    }});
    thumbs.forEach((thumb) => {{
      thumb.addEventListener("click", (event) => {{
        if (event.target.closest("a")) return;
        stop();
        setIndex(Number(thumb.dataset.index));
      }});
    }});
    dateFilter.addEventListener("change", () => {{
      stop();
      rebuildVisibleIndexes();
      setVisiblePosition(0);
    }});
    pageFilter.addEventListener("change", () => {{
      stop();
      rebuildVisibleIndexes();
      setVisiblePosition(0);
    }});
    aircraftOverlayToggle.addEventListener("change", () => {{
      window.localStorage.setItem("webcamTimelineAircraftOverlay", aircraftOverlayToggle.checked ? "on" : "off");
      setVisiblePosition(visiblePosition);
    }});
    visibleAircraftToggle.addEventListener("change", () => {{
      window.localStorage.setItem("webcamTimelineVisibleAircraftOverlay", visibleAircraftToggle.checked ? "on" : "off");
      setVisiblePosition(visiblePosition);
    }});
    themeToggle.addEventListener("click", () => {{
      const nextTheme = document.body.classList.contains("theme-dark") ? "light" : "dark";
      setTheme(nextTheme);
    }});
    window.addEventListener("resize", () => {{
      if (aircraftOverlayToggle.checked || visibleAircraftToggle.checked) setVisiblePosition(visiblePosition);
    }});

    setTheme(window.localStorage.getItem("webcamTimelineTheme") || "light");
    aircraftOverlayToggle.checked = window.localStorage.getItem("webcamTimelineAircraftOverlay") === "on";
    visibleAircraftToggle.checked = window.localStorage.getItem("webcamTimelineVisibleAircraftOverlay") === "on";
    populateDateFilter();
    populatePageFilter();
    rebuildVisibleIndexes();
    setVisiblePosition(0);
  </script>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch webcam images and build a timeline.")
    parser.add_argument(
        "--page-url",
        action="append",
        help="Webcam history page URL. Can be supplied multiple times. Defaults to both EGPG webcams.",
    )
    parser.add_argument("--output-dir", default="data/images", help="Directory for downloaded images.")
    parser.add_argument("--csv", default="data/timeline.csv", help="Timeline metadata CSV path.")
    parser.add_argument("--flights-csv", default="data/flights.csv", help="Flight metadata CSV path.")
    parser.add_argument("--cameras-csv", default="data/cameras.csv", help="Optional camera position and direction CSV path.")
    parser.add_argument("--no-flights", action="store_true", help="Do not fetch live flight data.")
    parser.add_argument("--flight-radius-nm", type=float, default=DEFAULT_FLIGHT_RADIUS_NM, help="Aircraft search radius around EGPG in nautical miles.")
    parser.add_argument("--flight-max-altitude-ft", type=float, default=DEFAULT_FLIGHT_MAX_ALTITUDE_FT, help="Maximum aircraft altitude to keep for EGPG-local matching.")
    parser.add_argument("--once", action="store_true", help="Fetch once and exit.")
    parser.add_argument("--watch", action="store_true", help="Keep fetching on an interval.")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds.")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = PROJECT_ROOT / args.output_dir
    csv_path = PROJECT_ROOT / args.csv
    flights_csv_path = PROJECT_ROOT / args.flights_csv
    cameras_csv_path = PROJECT_ROOT / args.cameras_csv
    page_urls = args.page_url or DEFAULT_PAGE_URLS

    if not args.once and not args.watch:
        args.once = True

    while True:
        new_count, skipped_count = save_images(
            page_urls=page_urls,
            output_dir=output_dir,
            csv_path=csv_path,
        )
        flight_new_count = 0
        flight_provider = "disabled"

        if not args.no_flights:
            flight_new_count, flight_provider = update_flights_csv(
                csv_path=flights_csv_path,
                lat=EGPG_LAT,
                lon=EGPG_LON,
                radius_nm=args.flight_radius_nm,
                max_altitude_ft=args.flight_max_altitude_ft,
            )

        write_html(PROJECT_ROOT / "data" / "timeline.html", read_existing(csv_path), flights_csv_path, cameras_csv_path)

        print(
            f"{datetime.now().isoformat(timespec='seconds')} "
            f"pages={len(page_urls)} new={new_count} skipped={skipped_count} "
            f"flight_provider={flight_provider} flight_new={flight_new_count} timeline={csv_path}"
        )

        if args.once:
            return 0

        time.sleep(max(args.interval, 1))


if __name__ == "__main__":
    raise SystemExit(main())
