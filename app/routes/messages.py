import logging
from fastapi import APIRouter, Query

from app.database import get_db
from app.models.schemas import MessageResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get("/", response_model=list[MessageResponse])
async def list_messages(
    phone_number: str | None = Query(None, description="Filter by phone number"),
    channel: str | None = Query(None, description="Filter by channel (whatsapp/sms)"),
    direction: str | None = Query(None, description="Filter by direction (incoming/outgoing)"),
    limit: int = Query(50, ge=1, le=500, description="Number of messages to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """Get message history with optional filters."""
    db = await get_db()
    try:
        conditions: list[str] = []
        params: list[str | int] = []

        if phone_number:
            conditions.append("(sender = ? OR receiver = ?)")
            params.extend([phone_number, phone_number])
        if channel:
            conditions.append("channel = ?")
            params.append(channel)
        if direction:
            conditions.append("direction = ?")
            params.append(direction)

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        query = f"""
            SELECT id, sender, receiver, content, channel, direction, timestamp
            FROM messages
            {where_clause}
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        return [
            MessageResponse(
                id=row[0],
                sender=row[1],
                receiver=row[2],
                content=row[3],
                channel=row[4],
                direction=row[5],
                timestamp=str(row[6]),
            )
            for row in rows
        ]
    finally:
        await db.close()
