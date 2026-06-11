import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.config import get_settings
from mileage_logger.models import Site

logger = logging.getLogger(__name__)

GOOGLE_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
GOOGLE_FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.formattedAddress,"
    "places.shortFormattedAddress,"
    "places.location,"
    "places.businessStatus,"
    "places.types"
)
NON_BUSINESS_TYPES = {
    "administrative_area_level_1",
    "administrative_area_level_2",
    "country",
    "geocode",
    "intersection",
    "locality",
    "neighborhood",
    "political",
    "postal_code",
    "route",
    "street_address",
    "sublocality",
}


@dataclass(frozen=True)
class PlaceMatch:
    place_id: str
    display_name: str
    latitude: Decimal
    longitude: Decimal
    formatted_address: str
    business_status: str
    types: tuple[str, ...]


def _text_value(value: object) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        if text:
            return str(text).strip()
    return ""


def _place_match_from_payload(place: dict) -> PlaceMatch | None:
    display_name = _text_value(place.get("displayName"))
    location = place.get("location")
    if not display_name or not isinstance(location, dict):
        return None

    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if latitude is None or longitude is None:
        return None

    return PlaceMatch(
        place_id=str(place.get("id") or ""),
        display_name=display_name,
        latitude=Decimal(str(latitude)),
        longitude=Decimal(str(longitude)),
        formatted_address=str(
            place.get("shortFormattedAddress") or place.get("formattedAddress") or ""
        ).strip(),
        business_status=str(place.get("businessStatus") or ""),
        types=tuple(str(place_type) for place_type in place.get("types", [])),
    )


def best_business_place(places: list[dict]) -> PlaceMatch | None:
    matches = [
        match
        for place in places
        if (match := _place_match_from_payload(place)) is not None
        and not set(match.types).intersection(NON_BUSINESS_TYPES)
    ]
    if not matches:
        return None

    operational = [match for match in matches if match.business_status == "OPERATIONAL"]
    return (operational or matches)[0]


def lookup_google_place(latitude: Decimal, longitude: Decimal) -> PlaceMatch | None:
    settings = get_settings()
    if not settings.google_places_api_key or not settings.google_places_auto_create_sites:
        return None

    radius_m = max(1, min(settings.google_places_radius_m, 50_000))
    payload = {
        "maxResultCount": 5,
        "rankPreference": "DISTANCE",
        "locationRestriction": {
            "circle": {
                "center": {
                    "latitude": float(latitude),
                    "longitude": float(longitude),
                },
                "radius": float(radius_m),
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": GOOGLE_FIELD_MASK,
    }

    logger.info(
        "Looking up Google Places match for unknown stop lat=%s lon=%s", latitude, longitude
    )
    try:
        response = httpx.post(GOOGLE_NEARBY_SEARCH_URL, json=payload, headers=headers, timeout=15)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Google Places lookup failed")
        return None

    data = response.json()
    places = data.get("places", [])
    if not isinstance(places, list):
        return None
    return best_business_place(places)


def _distance_meters_between(
    latitude_a: Decimal,
    longitude_a: Decimal,
    latitude_b: Decimal,
    longitude_b: Decimal,
) -> Decimal:
    from mileage_logger.services.mileage import METERS_PER_MILE, haversine_miles

    return haversine_miles(latitude_a, longitude_a, latitude_b, longitude_b) * METERS_PER_MILE


def _unique_site_name(db: Session, place: PlaceMatch, latitude: Decimal, longitude: Decimal) -> str:
    existing = list(db.scalars(select(Site).where(Site.name == place.display_name)))
    for site in existing:
        distance_m = _distance_meters_between(site.latitude, site.longitude, latitude, longitude)
        if distance_m <= Decimal(get_settings().owntracks_default_site_radius_m):
            return site.name

    if not existing:
        return place.display_name

    if place.formatted_address:
        candidate = f"{place.display_name} - {place.formatted_address}"
        if db.scalar(select(Site).where(Site.name == candidate)) is None:
            return candidate

    candidate = f"{place.display_name} ({latitude:.5f}, {longitude:.5f})"
    if db.scalar(select(Site).where(Site.name == candidate)) is None:
        return candidate

    suffix = 2
    while True:
        suffixed = f"{candidate} #{suffix}"
        if db.scalar(select(Site).where(Site.name == suffixed)) is None:
            return suffixed
        suffix += 1


def create_site_from_google_place(
    db: Session,
    latitude: Decimal,
    longitude: Decimal,
) -> Site | None:
    place = lookup_google_place(latitude, longitude)
    if place is None:
        return None

    settings = get_settings()
    existing = list(db.scalars(select(Site).where(Site.name == place.display_name)))
    for site in existing:
        distance_m = _distance_meters_between(site.latitude, site.longitude, latitude, longitude)
        if distance_m <= Decimal(settings.owntracks_default_site_radius_m):
            return site

    name = _unique_site_name(db, place, latitude, longitude)
    site = Site(
        name=name,
        latitude=place.latitude,
        longitude=place.longitude,
        radius_m=settings.owntracks_default_site_radius_m,
        active=True,
    )
    db.add(site)
    db.flush()
    logger.info("Created site from Google Places: %s", name)
    return site
