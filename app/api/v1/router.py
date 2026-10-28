from fastapi import APIRouter

from app.api.v1 import auth, documents, pets, reference_ranges

router = APIRouter(prefix="/v1")
router.include_router(auth.router)
router.include_router(pets.router)
router.include_router(documents.router)
router.include_router(reference_ranges.router)
