from fastapi import APIRouter

from app.routing.api.v1 import router as api_v1_router

router = APIRouter(prefix="/api")

router.include_router(api_v1_router)
