import argparse
from datetime import date

from mileage_logger.database import SessionLocal
from mileage_logger.services.gas_prices import fetch_and_save_current_snapshot
from mileage_logger.services.mileage import generate_trips
from mileage_logger.services.pdf import generate_monthly_pdf


def main() -> None:
    parser = argparse.ArgumentParser(prog="mileage-logger")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("gas-snapshot", help="Fetch and store the current Michigan gas price")

    trips_parser = subcommands.add_parser("generate-trips", help="Generate trips for a date range")
    trips_parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    trips_parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")

    report_parser = subcommands.add_parser("report", help="Generate a monthly PDF report")
    report_parser.add_argument("year", type=int)
    report_parser.add_argument("month", type=int)

    args = parser.parse_args()

    with SessionLocal() as db:
        if args.command == "gas-snapshot":
            snapshot = fetch_and_save_current_snapshot(db)
            print(f"Saved {snapshot.state} {snapshot.price_per_gallon} on {snapshot.observed_on}")
        elif args.command == "generate-trips":
            start = date.fromisoformat(args.start)
            end = date.fromisoformat(args.end)
            trips = generate_trips(db, start, end)
            print(f"Generated {len(trips)} trips")
        elif args.command == "report":
            report = generate_monthly_pdf(db, args.year, args.month)
            print(f"Generated {report.pdf_path}")


if __name__ == "__main__":
    main()
