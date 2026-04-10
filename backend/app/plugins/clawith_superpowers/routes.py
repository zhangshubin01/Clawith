from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health():
    """Health check endpoint for the Superpowers plugin."""
    return {"status": "ok"}
