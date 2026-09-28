from fastapi import APIRouter

router = APIRouter(tags=["hello"])


@router.get("/")
def hello() -> dict[str, str]:
    return {"message": "Hello World"}
