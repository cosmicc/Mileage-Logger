from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from mileage_logger.config import get_settings
from mileage_logger.models import MonthlyReport, Trip
from mileage_logger.services.gas_prices import get_or_create_monthly_price
from mileage_logger.services.mileage import included_monthly_miles


@dataclass(frozen=True)
class TripReportRow:
    trip_date: date
    from_location: str
    to_location: str
    start_miles: Decimal
    stop_miles: Decimal
    trip_miles: Decimal


def calculate_reimbursement_gallons(total_miles: Decimal, vehicle_mpg: Decimal) -> Decimal:
    if vehicle_mpg <= 0:
        raise ValueError("vehicle_mpg must be greater than zero")
    return (total_miles / vehicle_mpg).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def calculate_reimbursement(
    total_miles: Decimal,
    monthly_gas_price: Decimal,
    vehicle_mpg: Decimal,
) -> Decimal:
    if vehicle_mpg <= 0:
        raise ValueError("vehicle_mpg must be greater than zero")
    return ((total_miles / vehicle_mpg) * monthly_gas_price).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def included_trips_for_month(db: Session, year: int, month: int) -> list[Trip]:
    start, end = _month_bounds(year, month)
    stmt = (
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
        .where(Trip.trip_date >= start)
        .where(Trip.trip_date < end)
        .where(Trip.include_in_report.is_(True))
        .order_by(Trip.trip_date.asc(), Trip.started_at.asc())
    )
    return list(db.scalars(stmt))


def _format_coordinates(latitude: Decimal, longitude: Decimal) -> str:
    return f"{latitude:.5f}, {longitude:.5f}"


def _origin_location(trip: Trip) -> str:
    if trip.origin_site is not None:
        return trip.origin_site.name
    return f"Unknown ({_format_coordinates(trip.start_latitude, trip.start_longitude)})"


def _destination_location(trip: Trip) -> str:
    if trip.destination_site is not None:
        return trip.destination_site.name
    return f"Unknown ({_format_coordinates(trip.end_latitude, trip.end_longitude)})"


def trip_report_rows(trips: list[Trip]) -> list[TripReportRow]:
    rows: list[TripReportRow] = []
    running_miles = Decimal("0.00")
    for trip in trips:
        trip_miles = Decimal(trip.miles).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        start_miles = running_miles
        stop_miles = (running_miles + trip_miles).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
        rows.append(
            TripReportRow(
                trip_date=trip.trip_date,
                from_location=_origin_location(trip),
                to_location=_destination_location(trip),
                start_miles=start_miles,
                stop_miles=stop_miles,
                trip_miles=trip_miles,
            )
        )
        running_miles = stop_miles
    return rows


def generate_monthly_pdf(db: Session, year: int, month: int) -> MonthlyReport:
    settings = get_settings()
    gas_price = get_or_create_monthly_price(db, year, month)
    trips = included_trips_for_month(db, year, month)
    total_miles = included_monthly_miles(db, year, month)
    reimbursement_gallons = calculate_reimbursement_gallons(total_miles, settings.vehicle_mpg)
    reimbursement_total = calculate_reimbursement(
        total_miles,
        gas_price.average_price_per_gallon,
        settings.vehicle_mpg,
    )
    report_rows = trip_report_rows(trips)

    output_dir = Path(settings.report_output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / f"mileage-{year}-{month:02d}.pdf"

    styles = getSampleStyleSheet()
    table_cell = ParagraphStyle(
        "TableCell",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7,
        leading=8.5,
    )
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=landscape(LETTER),
        leftMargin=0.45 * inch,
        rightMargin=0.45 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
    )
    story = [
        Paragraph(f"Mileage Log - {year}-{month:02d}", styles["Title"]),
        Paragraph("Included work-site trips for reimbursement", styles["Normal"]),
        Spacer(1, 16),
    ]

    trip_rows = [["Date", "From", "To", "Start Mi", "Stop Mi", "Trip Mi"]]
    for row in report_rows:
        trip_rows.append(
            [
                row.trip_date.isoformat(),
                Paragraph(row.from_location, table_cell),
                Paragraph(row.to_location, table_cell),
                f"{row.start_miles:.2f}",
                f"{row.stop_miles:.2f}",
                f"{row.trip_miles:.2f}",
            ]
        )

    if len(trip_rows) == 1:
        trip_rows.append(["", "No included trips", "", "0.00", "0.00", "0.00"])

    table = Table(trip_rows, repeatRows=1, colWidths=[70, 210, 210, 70, 70, 70])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#101828")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d0d5dd")),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 18))

    summary_data = [
        ["Michigan Avg Monthly Gas Price", f"${gas_price.average_price_per_gallon:.3f}"],
        ["Vehicle MPG", f"{settings.vehicle_mpg:.1f}"],
        ["Total trip miles for month", f"{total_miles:.2f}"],
        ["Reimbursement gallons", f"{reimbursement_gallons:.3f}"],
        ["Total reimbursement", f"${reimbursement_total:.2f}"],
    ]
    summary = Table(summary_data, hAlign="RIGHT", colWidths=[220, 120])
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f7")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d5dd")),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(summary)
    doc.build(story)

    stmt = (
        select(MonthlyReport).where(MonthlyReport.year == year).where(MonthlyReport.month == month)
    )
    report = db.scalar(stmt)
    if report is None:
        report = MonthlyReport(
            year=year,
            month=month,
            total_miles=total_miles,
            gas_price_id=gas_price.id,
            reimbursement_total=reimbursement_total,
            pdf_path=str(pdf_path),
        )
        db.add(report)
    else:
        report.total_miles = total_miles
        report.gas_price_id = gas_price.id
        report.reimbursement_total = reimbursement_total
        report.pdf_path = str(pdf_path)
    db.commit()
    db.refresh(report)
    return report
