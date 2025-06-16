from contextlib import asynccontextmanager

import uvicorn

from fastapi import FastAPI, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from starlette import status

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.responses.base import BaseResponse
from app.routing import router as api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    DatabaseSessionManager.create(settings.DB_URL)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="FastAPI Application",
    description="FastAPI Application with Swagger",
    version="1.0.0",
    docs_url="/docs",
)
app.include_router(api_router)

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
def exception_handler(request, err):
    # base_error_message = f"Failed to execute:
    # {request.method}: {request.url}"
    # message = f"{base_error_message}. Detail: {err}"
    # TODO add  log
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=BaseResponse(ok=False, error={"message": str(err)}).dict(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=BaseResponse(ok=False, error={"message": exc.detail}).dict(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    error_details = exc.errors()

    errors = {}

    for err in error_details:
        field = err["loc"][-1]
        errors[field] = err["msg"].replace("Value error, ", "")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=BaseResponse(ok=False, error=errors).dict(),
    )


@app.get("/", tags=["Home"])
def home():
    return {"message": "Hello world"}


if __name__ == "__main__":
    uvicorn.run("main:app", port=8000, loop="asyncio", reload=True)
