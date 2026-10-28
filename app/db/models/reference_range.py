import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SpeciesReferenceRange(Base):
    """A parameter's normal range for one species, as a lab printed it. The first range
    a report supplies is kept: it fills later reports that print none, and is never
    overwritten by a user (see app/api/v1/reference_ranges.py)."""

    __tablename__ = "species_reference_ranges"
    __table_args__ = (UniqueConstraint("species", "parameter_key", name="uq_species_parameter"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "dog", "cat" - see reference_ranges.normalize_species.
    species: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    # Canonical analyte when known ("Albumin" for "ALB"), else the label's lookup key,
    # so the same test printed two ways shares one range.
    parameter_key: Mapped[str] = mapped_column(String(120), nullable=False)
    parameter_name: Mapped[str] = mapped_column(String(120), nullable=False)
    reference_range: Mapped[str] = mapped_column(String(120), nullable=False)
    unit: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contributed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
