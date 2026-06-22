from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_session
from src.gateway.service import GatewayService


def get_gateway_service(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
) -> GatewayService:
    return GatewayService(session)
