#!/usr/bin/env python3
"""Download iNaturalist observation photos for taxa listed in config.yaml."""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

API_BASE = "https://api.inaturalist.org/v1"
USER_AGENT = "download-inaturalist-images/1.0 (personal research)"
API_DELAY_SEC = 1.0
PHOTO_DELAY_SEC = 0.15
PER_PAGE = 200
PHOTO_SIZES = ("original", "large", "medium", "small", "thumb", "square")
TAXON_URL_RE = re.compile(r"/taxa/(\d+)", re.IGNORECASE)
DEFAULT_CONFIG_NAME = "config.yaml"

CSV_FIELDS = [
    "photo_id",
    "filename",
    "local_path",
    "photo_url",
    "width",
    "height",
    "license",
    "attribution",
    "observation_id",
    "observation_url",
    "taxon_id",
    "taxon_name",
    "common_name",
    "quality_grade",
    "observer",
    "observer_name",
    "observed_on",
    "observed_at",
    "time_zone",
    "latitude",
    "longitude",
    "place_guess",
    "positional_accuracy",
    "public_positional_accuracy",
    "obscured",
    "geoprivacy",
    "captive",
    "description",
]


class RateLimitError(RuntimeError):
    pass


class InatClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._last_api_call = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_api_call
        if elapsed < API_DELAY_SEC:
            time.sleep(API_DELAY_SEC - elapsed)

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        for attempt in range(5):
            self._throttle()
            self._last_api_call = time.monotonic()
            response = self.session.get(url, params=params, timeout=60)
            if response.status_code == 429:
                wait = min(30, 2 ** (attempt + 1))
                print(f"Rate limited, retrying in {wait}s…", file=sys.stderr)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        raise RateLimitError(f"Too many 429s from {url}")

    def search_taxon(self, query: str) -> dict[str, Any] | None:
        data = self.get_json("/taxa", {"q": query, "is_active": "true"})
        results = data.get("results") or []
        if not results:
            return None
        lowered = query.strip().lower()
        for item in results:
            if (item.get("name") or "").lower() == lowered:
                return item
        return results[0]

    def get_taxon(self, taxon_id: int) -> dict[str, Any]:
        data = self.get_json(f"/taxa/{taxon_id}")
        results = data.get("results") or []
        if not results:
            raise ValueError(f"Taxon not found: taxon_id={taxon_id}")
        return results[0]

    def iter_observations(
        self,
        taxon_id: int,
        quality_grade: str | None = None,
    ):
        id_below: int | None = None
        while True:
            params: dict[str, Any] = {
                "taxon_id": taxon_id,
                "photos": "true",
                "per_page": PER_PAGE,
                "order": "desc",
                "order_by": "id",
            }
            if quality_grade:
                params["quality_grade"] = quality_grade
            if id_below is not None:
                params["id_below"] = id_below
            data = self.get_json("/observations", params)
            results = data.get("results") or []
            if not results:
                return
            for obs in results:
                yield obs
            id_below = results[-1]["id"]
            if len(results) < PER_PAGE:
                return


def parse_taxon_input(raw: str) -> tuple[int | None, str | None]:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw), None
    match = TAXON_URL_RE.search(raw)
    if match:
        return int(match.group(1)), None
    return None, raw


def sized_photo_url(url: str, size: str) -> str:
    return re.sub(r"/(square|thumb|small|medium|large|original)\.", f"/{size}.", url, count=1)


def photo_extension(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"


def is_all_rights_reserved(license_code: str | None) -> bool:
    return not license_code


def parse_limit(value: str) -> int | None:
    text = str(value).strip().lower()
    if text in {"0", "all", "none", "unlimited", "inf"}:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "limit must be a positive integer, or 0 / all / unlimited"
        ) from exc
    if number < 0:
        raise argparse.ArgumentTypeError("limit cannot be negative")
    if number == 0:
        return None
    return number


def parse_lat_lng(observation: dict[str, Any]) -> tuple[str, str]:
    geojson = observation.get("geojson") or {}
    coords = geojson.get("coordinates")
    if isinstance(coords, (list, tuple)) and len(coords) >= 2:
        return str(coords[1]), str(coords[0])
    location = observation.get("location") or ""
    if "," in location:
        lat, lng = location.split(",", 1)
        return lat.strip(), lng.strip()
    return "", ""


def format_bool(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return ""


def flatten_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def limit_text(limit: int | None) -> str:
    return "unlimited" if limit is None else str(limit)


def resolve_taxon(client: InatClient, raw: str) -> dict[str, Any]:
    taxon_id, name = parse_taxon_input(raw)
    if taxon_id is not None:
        return client.get_taxon(taxon_id)
    taxon = client.search_taxon(name or raw)
    if taxon is None:
        raise ValueError(f"Taxon not found: {raw}")
    return taxon


def download_file(session: requests.Session, url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    for attempt in range(4):
        try:
            with session.get(url, stream=True, timeout=60) as response:
                if response.status_code == 429:
                    time.sleep(min(20, 2 ** (attempt + 1)))
                    continue
                response.raise_for_status()
                with tmp.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            handle.write(chunk)
            tmp.replace(dest)
            return
        except requests.RequestException:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def load_existing_photo_ids(*csv_paths: Path) -> set[int]:
    ids: set[int] = set()
    for csv_path in csv_paths:
        if not csv_path.exists():
            continue
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    ids.add(int(row["photo_id"]))
                except (KeyError, TypeError, ValueError):
                    continue
    return ids


def append_csv_row(csv_path: Path, row: dict[str, Any]) -> None:
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def build_metadata_row(
    observation: dict[str, Any],
    photo: dict[str, Any],
    *,
    taxon_id: int,
    taxon_name: str,
    photo_url: str,
    local_path: Path,
) -> dict[str, Any]:
    obs_id = observation["id"]
    obs_taxon = observation.get("taxon") or {}
    user = observation.get("user") or {}
    dims = photo.get("original_dimensions") or {}
    latitude, longitude = parse_lat_lng(observation)
    return {
        "photo_id": photo["id"],
        "filename": local_path.name,
        "local_path": str(local_path),
        "photo_url": photo_url,
        "width": dims.get("width") or "",
        "height": dims.get("height") or "",
        "license": photo.get("license_code") or "all-rights-reserved",
        "attribution": flatten_text(photo.get("attribution")),
        "observation_id": obs_id,
        "observation_url": observation.get("uri")
        or f"https://www.inaturalist.org/observations/{obs_id}",
        "taxon_id": obs_taxon.get("id") or taxon_id,
        "taxon_name": flatten_text(obs_taxon.get("name") or taxon_name),
        "common_name": flatten_text(obs_taxon.get("preferred_common_name")),
        "quality_grade": observation.get("quality_grade") or "",
        "observer": flatten_text(user.get("login")),
        "observer_name": flatten_text(user.get("name")),
        "observed_on": observation.get("observed_on") or "",
        "observed_at": observation.get("time_observed_at") or "",
        "time_zone": observation.get("observed_time_zone")
        or observation.get("created_time_zone")
        or "",
        "latitude": latitude,
        "longitude": longitude,
        "place_guess": flatten_text(observation.get("place_guess")),
        "positional_accuracy": observation.get("positional_accuracy") or "",
        "public_positional_accuracy": observation.get("public_positional_accuracy") or "",
        "obscured": format_bool(observation.get("obscured")),
        "geoprivacy": observation.get("geoprivacy") or "",
        "captive": format_bool(observation.get("captive")),
        "description": flatten_text(observation.get("description")),
    }


def collect_photos(observation: dict[str, Any]) -> list[dict[str, Any]]:
    photos = observation.get("photos") or []
    unique: list[dict[str, Any]] = []
    seen: set[int] = set()
    for photo in photos:
        photo_id = photo.get("id")
        if photo_id is None or photo_id in seen:
            continue
        if photo.get("hidden"):
            continue
        seen.add(photo_id)
        unique.append(photo)
    return unique


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.dump(data, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)


def copy_config_file(source: Path | None, dest: Path) -> None:
    if source is None or not source.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() == source.resolve():
        return
    shutil.copy2(source, dest)


def save_result_config(
    dest_dir: Path,
    *,
    source_config: Path | None,
    settings: dict[str, Any],
) -> None:
    copy_config_file(source_config, dest_dir / "config.yaml")
    write_yaml(dest_dir / "run_settings.yaml", settings)


def load_config_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config file: {path}")
    return data


def normalize_species(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    species: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            species.append(text)
    return species


def build_settings_from_config(data: dict[str, Any]) -> dict[str, Any]:
    limit_raw = data.get("photo_limit", 1000)
    if limit_raw is None:
        limit = None
    else:
        limit = parse_limit(str(limit_raw))
    size = str(data.get("size") or "original").strip().lower()
    if size not in PHOTO_SIZES:
        raise SystemExit(f"Invalid size: {size} (options: {', '.join(PHOTO_SIZES)})")
    quality = str(data.get("quality") or "all").strip().lower()
    if quality not in {"all", "research", "needs_id", "casual"}:
        raise SystemExit(f"Invalid quality: {quality}")
    return {
        "photo_limit": limit,
        "size": size,
        "quality": quality,
        "licensed_only": bool(data.get("licensed_only", False)),
        "out_dir": Path(str(data.get("out_dir") or "downloads")),
        "species": normalize_species(data.get("species")),
    }


def download_taxon_photos(
    client: InatClient,
    taxon_query: str,
    out_dir: Path,
    size: str = "original",
    quality: str = "all",
    limit: int | None = None,
    licensed_only: bool = False,
    source_config: Path | None = None,
    extra_settings: dict[str, Any] | None = None,
) -> None:
    taxon = resolve_taxon(client, taxon_query)
    taxon_id = int(taxon["id"])
    taxon_name = taxon.get("name") or str(taxon_id)
    observations_count = taxon.get("observations_count")
    common = taxon.get("preferred_common_name")
    label = f"{taxon_name}" + (f" ({common})" if common else "")
    print(f"\n=== {label} ===")
    print(f"taxon_id: {taxon_id}")
    if observations_count is not None:
        print(f"iNaturalist observations (including descendants): {observations_count}")

    folder_name = re.sub(r"[^\w\-.]+", "_", taxon_name).strip("_") or str(taxon_id)
    dest_dir = out_dir / folder_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    csv_path = dest_dir / "metadata.csv"
    in_metadata = load_existing_photo_ids(csv_path)
    known_ids = in_metadata | load_existing_photo_ids(dest_dir / "photos.csv")

    run_settings = {
        "species": taxon_name,
        "query": taxon_query,
        "taxon_id": taxon_id,
        "photo_limit": 0 if limit is None else limit,
        "photo_limit_unlimited": limit is None,
        "size": size,
        "quality": quality,
        "licensed_only": licensed_only,
        "out_dir": str(out_dir),
        "already_in_metadata": len(in_metadata),
    }
    if extra_settings:
        run_settings.update(extra_settings)
    if source_config is not None:
        run_settings["source_config"] = str(source_config)
    save_result_config(dest_dir, source_config=source_config, settings=run_settings)

    quality_grade = None if quality == "all" else quality
    saved = 0
    skipped = 0
    reserved_skipped = 0
    hit_quota = False

    image_session = requests.Session()
    image_session.headers.update({"User-Agent": USER_AGENT})

    print(f"Output directory: {dest_dir}")
    print(f"Photos already in metadata.csv: {len(in_metadata)}")
    print(f"New photos to download this run: {limit_text(limit)}")
    print("Fetching observations and downloading photos…")

    try:
        for observation in client.iter_observations(taxon_id, quality_grade=quality_grade):
            obs_id = observation["id"]
            for photo in collect_photos(observation):
                if limit is not None and saved >= limit:
                    hit_quota = True
                    print(f"Reached this run's limit of {limit} new photos. Stopping.")
                    return
                photo_id = int(photo["id"])
                license_code = photo.get("license_code")
                if licensed_only and is_all_rights_reserved(license_code):
                    reserved_skipped += 1
                    continue
                square_url = photo.get("url") or ""
                if not square_url:
                    continue
                photo_url = sized_photo_url(square_url, size)
                filename = f"obs{obs_id}_photo{photo_id}{photo_extension(photo_url)}"
                local_path = dest_dir / filename
                already_have = photo_id in known_ids or local_path.exists()
                if already_have:
                    skipped += 1
                    if photo_id not in in_metadata:
                        append_csv_row(
                            csv_path,
                            build_metadata_row(
                                observation,
                                photo,
                                taxon_id=taxon_id,
                                taxon_name=taxon_name,
                                photo_url=photo_url,
                                local_path=local_path,
                            ),
                        )
                        in_metadata.add(photo_id)
                        known_ids.add(photo_id)
                    continue
                try:
                    download_file(image_session, photo_url, local_path)
                except requests.RequestException as exc:
                    print(f"Failed to download photo {photo_id}: {exc}", file=sys.stderr)
                    continue
                append_csv_row(
                    csv_path,
                    build_metadata_row(
                        observation,
                        photo,
                        taxon_id=taxon_id,
                        taxon_name=taxon_name,
                        photo_url=photo_url,
                        local_path=local_path,
                    ),
                )
                in_metadata.add(photo_id)
                known_ids.add(photo_id)
                saved += 1
                if saved % 10 == 0:
                    print(
                        f"Downloaded {saved} new photos "
                        f"(skipped existing {skipped}, skipped all-rights-reserved {reserved_skipped})"
                    )
                time.sleep(PHOTO_DELAY_SEC)
    finally:
        if not hit_quota and (limit is None or saved < limit):
            print("No more undownloaded photos available.")
        print(
            f"Finished {taxon_name}: {saved} new, {skipped} already present, "
            f"{reserved_skipped} skipped (all rights reserved). Metadata: {csv_path}"
        )


def find_default_config() -> Path | None:
    candidates = [
        Path.cwd() / DEFAULT_CONFIG_NAME,
        Path(__file__).resolve().parent / DEFAULT_CONFIG_NAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download iNaturalist observation photos for species listed in config.yaml (no login required)."
    )
    parser.add_argument(
        "taxon",
        nargs="*",
        help="Optional: override species in the config (scientific name, taxon ID, or URL)",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config file (default: config.yaml)")
    parser.add_argument("--url", help="Taxon page or browse_photos URL")
    parser.add_argument("--out", type=Path, default=None, help="Output directory (overrides config)")
    parser.add_argument(
        "--size",
        choices=PHOTO_SIZES,
        default=None,
        help="Photo size (overrides config)",
    )
    parser.add_argument(
        "--quality",
        choices=("all", "research", "needs_id", "casual"),
        default=None,
        help="Observation quality grade (overrides config)",
    )
    parser.add_argument(
        "--limit",
        type=parse_limit,
        default=argparse.SUPPRESS,
        help="New photos to download per species this run (overrides config; 0 / all = unlimited)",
    )
    parser.add_argument(
        "--no-limit",
        action="store_true",
        help="Do not limit the number of photos (same as --limit 0)",
    )
    parser.add_argument(
        "--licensed-only",
        action="store_true",
        help="Download only openly licensed photos (overrides config)",
    )
    return parser


def _configure_stdio() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args()

    config_path = args.config
    if config_path is None:
        config_path = find_default_config()
    elif not config_path.is_file():
        raise SystemExit(f"Config file not found: {config_path}")

    if config_path is not None:
        settings = build_settings_from_config(load_config_file(config_path))
        print(f"Config: {config_path}")
    else:
        settings = build_settings_from_config({})
        print("config.yaml not found; using command-line arguments.")

    cli_species = [part for part in args.taxon if str(part).strip()]
    if args.url:
        cli_species.append(args.url)
    species = cli_species or settings["species"]
    if not species:
        raise SystemExit("Add scientific names to species in config.yaml, or pass a taxon on the command line.")

    out_dir = args.out or settings["out_dir"]
    size = args.size or settings["size"]
    quality = args.quality or settings["quality"]
    if args.no_limit:
        limit = None
    elif hasattr(args, "limit"):
        limit = args.limit
    else:
        limit = settings["photo_limit"]
    licensed_only = True if args.licensed_only else settings["licensed_only"]

    out_dir.mkdir(parents=True, exist_ok=True)
    copy_config_file(config_path, out_dir / "config.yaml")
    write_yaml(
        out_dir / "run_settings.yaml",
        {
            "species": species,
            "photo_limit": 0 if limit is None else limit,
            "photo_limit_unlimited": limit is None,
            "size": size,
            "quality": quality,
            "licensed_only": licensed_only,
            "out_dir": str(out_dir),
            "source_config": str(config_path) if config_path else None,
        },
    )

    print(f"Output directory: {out_dir}")
    print(f"Species: {len(species)}; new photos per species this run: {limit_text(limit)}")

    client = InatClient()
    for query in species:
        try:
            download_taxon_photos(
                client,
                taxon_query=query,
                out_dir=out_dir,
                size=size,
                quality=quality,
                limit=limit,
                licensed_only=licensed_only,
                source_config=config_path,
            )
        except Exception as exc:
            print(f"Skipping {query}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
