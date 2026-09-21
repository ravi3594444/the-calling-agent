"""HTTP surfaces: the dashboard API, the tool API, telephony and the guest page."""

from .auth import router as auth_router
from .dashboard import router as dashboard_router
from .manage import router as manage_router
from .signup import router as signup_router
from .telephony import router as telephony_router
from .tools import router as tools_router

__all__ = [
    "auth_router",
    "dashboard_router",
    "manage_router",
    "signup_router",
    "telephony_router",
    "tools_router",
]
