from sqlalchemy import select
from sqlalchemy.orm import Session

from mileage_logger.models import OwnTracksLocation


def recent_owntracks_entries(db: Session, limit: int = 20) -> list[OwnTracksLocation]:
    return list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.received_at.desc(), OwnTracksLocation.id.desc())
            .limit(limit)
        )
    )
