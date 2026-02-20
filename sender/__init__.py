"""
OpusBrief - Sender Module

Responsible for:
- Sending briefings to various platforms (Discord, WeChat, etc.)
- Message formatting and length handling
- Connection testing and error handling
"""

from .discord import DiscordSender, DiscordSenderError

__all__ = [
    "DiscordSender",
    "DiscordSenderError",
]
