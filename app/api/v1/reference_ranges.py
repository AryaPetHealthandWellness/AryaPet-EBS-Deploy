"""Normal ranges per species, learned from the ranges labs print on reports.

The app sends the ranges a report printed (never ones a user typed) when a record is
saved, and reads them back to fill a report that prints none. The first range for a
species and parameter is kept and never overwritten, so the table only ever holds
what a lab actually printed.
"""

import re

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentDbUser
from app.db.models.reference_range import SpeciesReferenceRange
from app.db.session import get_db
from app.schemas.reference_range import ReferenceRangeMatch, ReferenceRangeRead, ReferenceRangeSubmit
from app.services.analyte_gazetteer import canonical_analyte

router = APIRouter(prefix="/reference-ranges", tags=["reference-ranges"])

_SPECIES_ALIASES = {
    "canine": "dog", "dogs": "dog", "puppy": "dog",
    "feline": "cat", "cats": "cat", "kitten": "cat",
}
# A range must carry a number: "3.2-4.3", "< 0.6", "0--199".
_HAS_NUMBER_RE = re.compile(r"\d")


def normalize_species(species: str) -> str:
    key = " ".join(species.lower().split())
    return _SPECIES_ALIASES.get(key, key)


def parameter_key(name: str) -> str:
    canonical = canonical_analyte(name)
    if canonical:
        return canonical
    cleaned = "".join(c if (c.isalnum() or c == "/") else " " for c in name.lower())
    return " ".join(cleaned.split())


@router.get("", response_model=list[ReferenceRangeRead])
async def list_reference_ranges(
    current_user: CurrentDbUser,
    species: str = Query(min_length=1, max_length=60),
    db: AsyncSession = Depends(get_db),
) -> list[SpeciesReferenceRange]:
    result = await db.execute(
        select(SpeciesReferenceRange)
        .where(SpeciesReferenceRange.species == normalize_species(species))
        .order_by(SpeciesReferenceRange.parameter_name)
    )
    return list(result.scalars().all())


@router.post("/match", response_model=dict[str, ReferenceRangeRead])
async def match_reference_ranges(
    payload: ReferenceRangeMatch,
    current_user: CurrentDbUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, SpeciesReferenceRange]:
    """The saved range for each of `names` as the report printed them ("ALB" finds the
    range saved as "Albumin"). Names with no saved range are left out."""
    saved = {
        row.parameter_key: row
        for row in (
            await db.execute(
                select(SpeciesReferenceRange).where(
                    SpeciesReferenceRange.species == normalize_species(payload.species)
                )
            )
        ).scalars()
    }
    return {name: saved[parameter_key(name)] for name in payload.names if parameter_key(name) in saved}


@router.post("", response_model=list[ReferenceRangeRead])
async def submit_reference_ranges(
    payload: ReferenceRangeSubmit,
    current_user: CurrentDbUser,
    db: AsyncSession = Depends(get_db),
) -> list[SpeciesReferenceRange]:
    """Saves the ranges this species doesn't have yet; returns the species' ranges."""
    species = normalize_species(payload.species)
    existing = {
        row.parameter_key
        for row in (
            await db.execute(select(SpeciesReferenceRange).where(SpeciesReferenceRange.species == species))
        ).scalars()
    }
    for parameter in payload.parameters:
        key = parameter_key(parameter.name)
        reference = parameter.reference_range.strip()
        if not key or key in existing or not _HAS_NUMBER_RE.search(reference):
            continue
        existing.add(key)
        db.add(
            SpeciesReferenceRange(
                species=species,
                parameter_key=key,
                parameter_name=parameter.name.strip(),
                reference_range=reference,
                unit=parameter.unit.strip(),
                source=payload.source,
                contributed_by=current_user.id,
            )
        )
    try:
        await db.commit()
    except IntegrityError:
        # Another report saved some of these first - theirs stands.
        await db.rollback()
    return await list_reference_ranges(current_user, species, db)
