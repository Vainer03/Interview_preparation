from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.db import engine, Base
from app.routers import users

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup
    Base.metadata.create_all(bind=engine)
    yield
    # Cleanup on shutdown (optional)
    await engine.dispose()

app = FastAPI(
    title="User API",
    lifespan=lifespan
)

app.include_router(users.router)