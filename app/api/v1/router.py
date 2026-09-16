from fastapi import APIRouter

from app.api.v1.endpoints import admin, agent_trust, health, suppliers, webhooks

api_router = APIRouter(prefix="/v1")
api_router.include_router(admin.router, prefix="/admin")
api_router.include_router(agent_trust.router)
api_router.include_router(health.router)
api_router.include_router(suppliers.router, prefix="/suppliers", tags=["Suppliers"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["Webhooks"])
