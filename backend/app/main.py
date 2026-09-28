from fastapi import FastAPI

from app.routers import hello

app = FastAPI(title="backend")
app.include_router(hello.router)
