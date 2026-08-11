from fastapi import APIRouter

from app.api.v1 import admin, auth, chat, coach_insights, exports, goals, inspiration, integrations, memory, metrics, onboarding, palmares, rides, training, users
from app.api.v1.dropbox import router as dropbox_router
from app.api.v1.wahoo import router as wahoo_router
from app.api.v1.billing import router as billing_router
from app.api.v1.waitlist import router as waitlist_router

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(rides.router)
api_router.include_router(metrics.router)
api_router.include_router(onboarding.router)
api_router.include_router(goals.router)
api_router.include_router(training.router)
api_router.include_router(exports.router)
api_router.include_router(integrations.router)
api_router.include_router(chat.router)
api_router.include_router(coach_insights.router)
api_router.include_router(memory.router)
api_router.include_router(inspiration.router)
api_router.include_router(palmares.router)
api_router.include_router(dropbox_router)
api_router.include_router(wahoo_router)
api_router.include_router(billing_router)
api_router.include_router(waitlist_router)
api_router.include_router(admin.router)
