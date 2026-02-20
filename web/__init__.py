"""
OpusBrief Web Module

This module provides the FastAPI-based web administration interface
for managing LLM configurations, webhooks, briefing tasks, and viewing history.
"""

from .server import create_app

__all__ = ["create_app"]
__version__ = "0.1.0"
