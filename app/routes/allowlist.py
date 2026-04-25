import logging
from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_admin

from app.database import get_db
from app.models.schemas import AllowlistEntry, AllowlistResponse

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/allowlist",
    tags=["allowlist"],
    dependencies=[Depends(require_admin)],
)


@router.get("/", response_model=list[AllowlistResponse])
async def list_allowlist():
    """Get all allowlisted phone numbers."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, phone_number, label, created_at FROM allowlist ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [
            AllowlistResponse(
                id=row[0],
                phone_number=row[1],
                label=row[2],
                created_at=str(row[3]),
            )
            for row in rows
        ]
    finally:
        await db.close()


@router.post("/", response_model=AllowlistResponse)
async def add_to_allowlist(entry: AllowlistEntry):
    """Add a phone number to the allowlist."""
    db = await get_db()
    try:
        # Normalize phone number - remove spaces, dashes, and + prefix
        phone = entry.phone_number.replace(" ", "").replace("-", "").replace("+", "")

        # Check if already exists
        cursor = await db.execute(
            "SELECT id FROM allowlist WHERE phone_number = ?", (phone,)
        )
        if await cursor.fetchone():
            raise HTTPException(
                status_code=409, detail="Phone number already in allowlist"
            )

        cursor = await db.execute(
            "INSERT INTO allowlist (phone_number, label) VALUES (?, ?) RETURNING id, phone_number, label, created_at",
            (phone, entry.label),
        )
        row = await cursor.fetchone()
        await db.commit()

        if row is None:
            raise HTTPException(status_code=500, detail="Failed to add to allowlist")

        return AllowlistResponse(
            id=row[0],
            phone_number=row[1],
            label=row[2],
            created_at=str(row[3]),
        )
    finally:
        await db.close()


@router.delete("/{phone_number}")
async def remove_from_allowlist(phone_number: str):
    """Remove a phone number from the allowlist."""
    db = await get_db()
    try:
        phone = phone_number.replace(" ", "").replace("-", "").replace("+", "")
        cursor = await db.execute(
            "DELETE FROM allowlist WHERE phone_number = ?", (phone,)
        )
        await db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=404, detail="Phone number not found in allowlist"
            )
        return {"status": "deleted", "phone_number": phone}
    finally:
        await db.close()
