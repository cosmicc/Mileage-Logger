from decimal import Decimal

from mileage_logger.services.places import best_business_place


def test_best_business_place_skips_non_business_results() -> None:
    result = best_business_place(
        [
            {
                "id": "street-address",
                "displayName": {"text": "123 Main St"},
                "location": {"latitude": 42.0, "longitude": -83.0},
                "types": ["street_address"],
            },
            {
                "id": "place-1",
                "displayName": {"text": "Client Warehouse"},
                "location": {"latitude": 42.3314, "longitude": -83.0458},
                "businessStatus": "OPERATIONAL",
                "shortFormattedAddress": "100 Industrial Dr",
                "types": ["point_of_interest", "establishment"],
            },
        ]
    )

    assert result is not None
    assert result.display_name == "Client Warehouse"
    assert result.latitude == Decimal("42.3314")
    assert result.formatted_address == "100 Industrial Dr"
