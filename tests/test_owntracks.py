import json

from mileage_logger.services.owntracks import parse_owntracks_location


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
