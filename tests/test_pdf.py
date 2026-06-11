from decimal import Decimal

from mileage_logger.services.pdf import calculate_reimbursement


def test_calculate_reimbursement_uses_requested_formula() -> None:
    assert calculate_reimbursement(Decimal("120.50"), Decimal("4.250")) == Decimal("512.13")
