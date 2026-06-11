import json
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from mileage_logger.models import Base, Site
from mileage_logger.services.owntracks import parse_owntracks_location, process_owntracks_payload


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_parse_owntracks_location_payload() -> None:
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": 1_718_000_000,
        "tid": "IP",
        "acc": 12,
        "batt": 88,
        "topic": "owntracks/me/android",
    }

    message = parse_owntracks_location(json.dumps(payload).encode("utf-8"))

    assert message.identity.user == "me"
    assert message.identity.device == "android"
    assert message.tracker_id == "IP"
    assert message.accuracy_m == 12
    assert str(message.latitude) == "42.3314"


def test_process_owntracks_waypoint_creates_site() -> None:
    db = _session()
    payload = {
        "_type": "waypoint",
        "desc": "Client Warehouse",
        "lat": 42.3314,
        "lon": -83.0458,
        "rad": "75",
        "tst": 1_718_000_000,
        "rid": "abc123",
    }

    result = process_owntracks_payload(db, json.dumps(payload).encode("utf-8"))
    site = db.scalar(select(Site).where(Site.name == "Client Warehouse"))

    assert result.location is None
    assert site is not None
    assert site.radius_m == 75
    assert site.latitude == Decimal("42.3314")


def test_process_owntracks_location_with_region_creates_approximate_site() -> None:
    db = _session()
    payload = {
        "_type": "location",
        "lat": 42.3314,
        "lon": -83.0458,
        "tst": 1_718_000_000,
        "inregions": ["Client Office"],
    }

    result = process_owntracks_payload(db, json.dumps(payload).encode("utf-8"))
    site = db.scalar(select(Site).where(Site.name == "Client Office"))

    assert result.location is not None
    assert site is not None
    assert site.radius_m == 150
