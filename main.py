from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE_PATTERN = re.compile(r"\s+")
DEFAULT_HIP_URL = "https://hip.hosting/ru"
DEFAULT_ORDER_URL = "https://my.hip.hosting/hiplets/new"
DEFAULT_STATE_PATH = "data/state.json"
DEFAULT_CHECK_INTERVAL_SECONDS = 300
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20
PRICE_SECTION_HEADING = "Тарифы VPS/VDS серверов"
PRICE_SECTION_END_MARKER = " Standard Memory "
STATUS_AVAILABLE = "AVAILABLE"
STATUS_SOLD_OUT = "SOLD OUT"
STATUS_PLANNED = "PLANNED"
DEFAULT_HEADERS = {
    "User-Agent": "hip-availability-watcher/1.0 (+https://hip.hosting/ru)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


@dataclass(slots=True)
class Config:
    hip_url: str
    order_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    state_path: Path
    check_interval_seconds: int
    request_timeout_seconds: int
    watched_region_slugs: set[str]
    run_once: bool


@dataclass(slots=True)
class SizeSummary:
    slug: str
    range_name: str
    monthly_price: float
    units: int


@dataclass(slots=True)
class RegionAvailability:
    slug: str
    country: str
    city: str | None
    available_sizes: list[SizeSummary]
    status: str

    @property
    def display_name(self) -> str:
        if self.city:
            return f"{self.country} ({self.city})"
        return self.country

    @property
    def sold_out(self) -> bool:
        return self.status == STATUS_SOLD_OUT


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    run_worker(config)


def load_config() -> Config:
    telegram_bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = get_required_env("TELEGRAM_CHAT_ID")
    state_path = Path(os.getenv("STATE_PATH", DEFAULT_STATE_PATH))
    watched_region_slugs = parse_csv_set(os.getenv("WATCHED_REGION_SLUGS", ""))

    return Config(
        hip_url=os.getenv("HIP_URL", DEFAULT_HIP_URL),
        order_url=os.getenv("ORDER_URL", DEFAULT_ORDER_URL),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        state_path=state_path,
        check_interval_seconds=parse_positive_int(
            "CHECK_INTERVAL_SECONDS",
            os.getenv("CHECK_INTERVAL_SECONDS", str(DEFAULT_CHECK_INTERVAL_SECONDS)),
        ),
        request_timeout_seconds=parse_positive_int(
            "REQUEST_TIMEOUT_SECONDS",
            os.getenv("REQUEST_TIMEOUT_SECONDS", str(DEFAULT_REQUEST_TIMEOUT_SECONDS)),
        ),
        watched_region_slugs=watched_region_slugs,
        run_once=parse_bool(os.getenv("RUN_ONCE", "false")),
    )


def run_worker(config: Config) -> None:
    previous_state = load_state(config.state_path)

    if previous_state is None:
        logging.info("State file not found, first successful check will initialize it")
    else:
        logging.info(
            "Loaded state for %s regions", len(previous_state.get("regions", {}))
        )

    while True:
        try:
            regions = fetch_region_availability(config)
            current_state = build_state(regions)

            if previous_state is None:
                save_state(config.state_path, current_state)
                previous_state = current_state
                logging.info("Initial state saved for %s regions", len(regions))
            else:
                reopened_regions = find_reopened_regions(
                    previous_state=previous_state,
                    current_regions=regions,
                    watched_region_slugs=config.watched_region_slugs,
                )

                if reopened_regions:
                    logging.info("Found %s reopened regions", len(reopened_regions))
                    for region in reopened_regions:
                        send_telegram_notification(config, region)
                else:
                    logging.info("No reopened regions detected")

                save_state(config.state_path, current_state)
                previous_state = current_state
        except Exception:
            logging.exception("Polling iteration failed")

        if config.run_once:
            return

        time.sleep(config.check_interval_seconds)


def fetch_region_availability(config: Config) -> list[RegionAvailability]:
    request = Request(config.hip_url, headers=DEFAULT_HEADERS)
    try:
        with urlopen(request, timeout=config.request_timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html_text = response.read().decode(charset)
    except HTTPError as error:
        raise RuntimeError(f"HIP returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Failed to reach HIP: {error.reason}") from error

    next_data = extract_next_data(html_text)
    page_props = next_data.get("props", {}).get("pageProps", {})
    if not isinstance(page_props, dict):
        raise RuntimeError("Unexpected Next.js payload structure")

    return aggregate_regions(page_props, html_text)


def extract_next_data(html_text: str) -> dict[str, Any]:
    match = NEXT_DATA_PATTERN.search(html_text)
    if match is None:
        raise RuntimeError("Could not locate __NEXT_DATA__ payload in HIP HTML")
    return json.loads(match.group(1))


def aggregate_regions(
    page_props: dict[str, Any], html_text: str
) -> list[RegionAvailability]:
    locale_store = (
        page_props.get("_nextI18Next", {}).get("initialI18nStore", {}).get("ru", {})
    )
    countries_by_region_slug = locale_store.get("countries", {}).get(
        "by_region_slug", {}
    )
    cities_by_region_slug = (
        locale_store.get("services", {}).get("locations", {}).get("vps", {})
    )
    statuses_by_region_slug = extract_region_statuses(
        html_text, countries_by_region_slug
    )

    ranges = collect_ranges(page_props)
    seen_region_slugs: set[str] = set()
    available_sizes_by_region: dict[str, list[SizeSummary]] = {}

    for range_definition in ranges:
        range_name = str(
            range_definition.get("name") or range_definition.get("slug") or "Unknown"
        )
        size_definitions = range_definition.get("sizes", [])
        if not isinstance(size_definitions, list):
            continue

        for size_definition in size_definitions:
            if not isinstance(size_definition, dict):
                continue

            size_slug = str(
                size_definition.get("slug") or size_definition.get("id") or "unknown"
            )
            price = parse_price(size_definition.get("pricing", {}))
            availabilities = size_definition.get("availabilities", {})
            if not isinstance(availabilities, dict):
                continue

            for region_slug, raw_units in availabilities.items():
                units = parse_units(raw_units)
                seen_region_slugs.add(region_slug)
                if units <= 0:
                    continue

                available_sizes_by_region.setdefault(region_slug, []).append(
                    SizeSummary(
                        slug=size_slug,
                        range_name=range_name,
                        monthly_price=price,
                        units=units,
                    )
                )

    all_region_slugs = seen_region_slugs | set(statuses_by_region_slug)
    if not all_region_slugs:
        raise RuntimeError("No region availability data found in HIP payload")

    regions: list[RegionAvailability] = []
    for region_slug in sorted(all_region_slugs):
        raw_available_sizes = sorted(
            available_sizes_by_region.get(region_slug, []),
            key=lambda item: (item.range_name.lower(), item.monthly_price, item.slug),
        )
        status = statuses_by_region_slug.get(region_slug, STATUS_AVAILABLE)
        available_sizes = raw_available_sizes if status == STATUS_AVAILABLE else []
        country = str(countries_by_region_slug.get(region_slug) or region_slug)
        city_value = cities_by_region_slug.get(region_slug)
        city = str(city_value) if city_value else None
        regions.append(
            RegionAvailability(
                slug=region_slug,
                country=country,
                city=city,
                available_sizes=available_sizes,
                status=status,
            )
        )

    return regions


def extract_region_statuses(
    html_text: str, countries_by_region_slug: dict[str, Any]
) -> dict[str, str]:
    normalized_text = normalize_html_text(html_text)
    section_text = extract_price_section_text(normalized_text)
    statuses_by_region_slug: dict[str, str] = {}

    for region_slug, country_value in sorted(
        countries_by_region_slug.items(),
        key=lambda item: len(str(item[1])),
        reverse=True,
    ):
        country = str(country_value).strip()
        if not country:
            continue

        match = re.search(
            rf"{re.escape(country)}(?:\s+({re.escape(STATUS_SOLD_OUT)}|{re.escape(STATUS_PLANNED)}))?",
            section_text,
        )
        if match is None:
            continue

        explicit_status = match.group(1)
        if explicit_status == STATUS_SOLD_OUT:
            statuses_by_region_slug[region_slug] = STATUS_SOLD_OUT
        elif explicit_status == STATUS_PLANNED:
            statuses_by_region_slug[region_slug] = STATUS_PLANNED
        else:
            statuses_by_region_slug[region_slug] = STATUS_AVAILABLE

    if not statuses_by_region_slug:
        raise RuntimeError("Could not parse visible region statuses from HIP page")

    return statuses_by_region_slug


def normalize_html_text(html_text: str) -> str:
    text = HTML_TAG_PATTERN.sub(" ", html_text)
    text = html.unescape(text)
    text = WHITESPACE_PATTERN.sub(" ", text)
    return f" {text.strip()} "


def extract_price_section_text(normalized_text: str) -> str:
    start_index = normalized_text.find(PRICE_SECTION_HEADING)
    if start_index == -1:
        raise RuntimeError("Could not find the pricing section in HIP page text")

    tail_text = normalized_text[start_index:]
    end_index = tail_text.find(PRICE_SECTION_END_MARKER)
    if end_index == -1:
        raise RuntimeError("Could not isolate the visible location status section")

    return tail_text[:end_index]


def collect_ranges(page_props: dict[str, Any]) -> list[dict[str, Any]]:
    direct_ranges = page_props.get("ranges")
    if isinstance(direct_ranges, list):
        valid_direct_ranges = [
            item for item in direct_ranges if is_range_definition(item)
        ]
        if valid_direct_ranges:
            return valid_direct_ranges

    ranges: list[dict[str, Any]] = []
    seen_range_ids: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if is_range_definition(node):
                range_id = str(node.get("id") or node.get("slug") or id(node))
                if range_id not in seen_range_ids:
                    seen_range_ids.add(range_id)
                    ranges.append(node)
                return
            for value in node.values():
                walk(value)
            return

        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(page_props)
    return ranges


def is_range_definition(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    sizes = node.get("sizes")
    if not isinstance(sizes, list) or not sizes:
        return False
    return any(isinstance(item, dict) and "availabilities" in item for item in sizes)


def build_state(regions: list[RegionAvailability]) -> dict[str, Any]:
    return {
        "updated_at": current_timestamp(),
        "regions": {
            region.slug: {
                "country": region.country,
                "city": region.city,
                "status": region.status,
                "sold_out": region.sold_out,
                "available_count": len(region.available_sizes),
            }
            for region in regions
        },
    }


def load_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    with state_path.open("r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as file_handle:
        json.dump(state, file_handle, ensure_ascii=False, indent=2)
        file_handle.write("\n")


def find_reopened_regions(
    previous_state: dict[str, Any],
    current_regions: list[RegionAvailability],
    watched_region_slugs: set[str],
) -> list[RegionAvailability]:
    previous_regions = previous_state.get("regions", {})
    reopened_regions: list[RegionAvailability] = []

    for region in current_regions:
        if watched_region_slugs and region.slug not in watched_region_slugs:
            continue

        previous_region = previous_regions.get(region.slug)
        if not isinstance(previous_region, dict):
            continue

        previous_status = str(
            previous_region.get("status")
            or (
                STATUS_SOLD_OUT if previous_region.get("sold_out") else STATUS_AVAILABLE
            )
        )
        if previous_status != STATUS_AVAILABLE and region.status == STATUS_AVAILABLE:
            reopened_regions.append(region)

    return reopened_regions


def send_telegram_notification(config: Config, region: RegionAvailability) -> None:
    message = build_telegram_message(region, config.hip_url, config.order_url)
    payload = urlencode(
        {
            "chat_id": config.telegram_chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.request_timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"Telegram returned HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Failed to reach Telegram: {error.reason}") from error

    if response_payload.get("ok") is not True:
        raise RuntimeError(
            f"Telegram API error: {response_payload.get('description', 'unknown error')}"
        )

    logging.info("Sent Telegram notification for %s", region.display_name)


def build_telegram_message(
    region: RegionAvailability, hip_url: str, order_url: str
) -> str:
    range_lines = summarize_ranges(region.available_sizes)
    escaped_display_name = html.escape(region.display_name)
    escaped_region_slug = html.escape(region.slug)
    message_parts = [
        "<b>HIP: локация снова доступна</b>",
        f"<b>Локация:</b> {escaped_display_name}",
        f"<b>Регион:</b> {escaped_region_slug}",
        f"<b>Доступных конфигураций:</b> {len(region.available_sizes)}",
        "",
        "<b>Что доступно:</b>",
        *range_lines,
        "",
        f"<b>Сайт:</b> {html.escape(hip_url)}",
        f"<b>Заказать:</b> {html.escape(order_url)}",
    ]
    return "\n".join(message_parts)


def summarize_ranges(available_sizes: list[SizeSummary]) -> list[str]:
    grouped: dict[str, list[SizeSummary]] = {}
    for size in available_sizes:
        grouped.setdefault(size.range_name, []).append(size)

    lines: list[str] = []
    for range_name in sorted(grouped):
        group = sorted(
            grouped[range_name], key=lambda item: (item.monthly_price, item.slug)
        )
        min_price = min(item.monthly_price for item in group)
        labels = ", ".join(item.slug for item in group[:6])
        if len(group) > 6:
            labels = f"{labels}, ..."
        lines.append(
            "- <b>{range_name}</b>: {count} шт., от ${price:.2f}/мес. [{labels}]".format(
                range_name=html.escape(range_name),
                count=len(group),
                price=min_price,
                labels=html.escape(labels),
            )
        )
    return lines


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Environment variable {name} is required")
    return value


def parse_positive_int(name: str, raw_value: str) -> int:
    try:
        value = int(raw_value)
    except ValueError as error:
        raise SystemExit(f"Environment variable {name} must be an integer") from error
    if value <= 0:
        raise SystemExit(f"Environment variable {name} must be greater than zero")
    return value


def parse_bool(raw_value: str) -> bool:
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def parse_csv_set(raw_value: str) -> set[str]:
    return {item.strip() for item in raw_value.split(",") if item.strip()}


def parse_price(pricing: Any) -> float:
    if not isinstance(pricing, dict):
        return 0.0
    month_price = pricing.get("month", 0)
    try:
        return float(month_price)
    except (TypeError, ValueError):
        return 0.0


def parse_units(raw_value: Any) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return 0


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
