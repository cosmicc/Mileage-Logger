from dataclasses import dataclass
from math import ceil

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mileage_logger.models import OwnTracksLocation


@dataclass(frozen=True)
class OwnTracksEntriesPage:
    entries: list[OwnTracksLocation]
    page: int
    page_size: int
    total: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def first_item(self) -> int:
        if self.total == 0:
            return 0
        return ((self.page - 1) * self.page_size) + 1

    @property
    def last_item(self) -> int:
        return min(self.page * self.page_size, self.total)


def paginated_owntracks_entries(
    db: Session,
    *,
    page: int = 1,
    page_size: int = 20,
) -> OwnTracksEntriesPage:
    page_size = max(page_size, 1)
    total = db.scalar(select(func.count(OwnTracksLocation.id))) or 0
    total_pages = max(1, ceil(total / page_size))
    current_page = min(max(page, 1), total_pages)
    offset = (current_page - 1) * page_size
    newest_entries = list(
        db.scalars(
            select(OwnTracksLocation)
            .order_by(OwnTracksLocation.id.desc())
            .offset(offset)
            .limit(page_size)
        )
    )
    entries = list(reversed(newest_entries))
    return OwnTracksEntriesPage(
        entries=entries,
        page=current_page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
    )


def recent_owntracks_entries(db: Session, limit: int = 20) -> list[OwnTracksLocation]:
    return paginated_owntracks_entries(db, page=1, page_size=limit).entries
