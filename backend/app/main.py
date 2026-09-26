from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import initialize_database, router
from app.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="GEC Khagaria Canteen API", version="1.0.0", description="Campus menu, preorder, kitchen queue, and preparation-time API.", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "OPTIONS"], allow_headers=["Authorization", "Content-Type"])
app.include_router(router)


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled API error", exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "An unexpected server error occurred. Please try again."})