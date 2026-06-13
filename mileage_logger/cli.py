import argparse
import logging

from mileage_logger.database import SessionLocal
from mileage_logger.logging_config import configure_logging
from mileage_logger.services.gas_prices import GasPriceUnavailable, refresh_current_monthly_price

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mileage-logger")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("gas-snapshot", help="Fetch and store the current Michigan gas price")

    args = parser.parse_args()
    configure_logging("gas-snapshot" if args.command == "gas-snapshot" else "cli")

    with SessionLocal() as db:
        if args.command == "gas-snapshot":
            try:
                monthly = refresh_current_monthly_price(db)
            except GasPriceUnavailable as exc:
                logger.warning("Gas snapshot unavailable: %s", exc)
                raise SystemExit(f"Gas snapshot unavailable: {exc}") from exc
            except Exception:
                logger.exception("Gas snapshot failed")
                raise
            logger.info(
                "Refreshed monthly gas price state=%s year=%s month=%s average=%s",
                monthly.state,
                monthly.year,
                monthly.month,
                monthly.average_price_per_gallon,
            )
            print(
                f"Saved {monthly.state} monthly average "
                f"{monthly.average_price_per_gallon} for {monthly.year}-{monthly.month:02d}"
            )


if __name__ == "__main__":
    main()
