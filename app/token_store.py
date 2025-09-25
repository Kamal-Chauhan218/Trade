from typing import Dict, Optional
import asyncio
import time


class TokenStore:
    _tokens: Dict[str, Dict] = {}  # e.g. {"access_token": {"value": "...", "expiry": 123456}, ...}
    _lock = asyncio.Lock()

    @classmethod
    async def set_token(cls, name: str, value: str, expiry: Optional[int] = None):
        """
        Store a token with optional expiry timestamp (unix seconds).
        """
        async with cls._lock:
            cls._tokens[name] = {
                "value": value,
                "expiry": expiry or (time.time() + 3600)  # default 1h expiry
            }

    @classmethod
    async def get_token(cls, name: str) -> Optional[str]:
        """
        Get a token by name. Returns None if missing or expired.
        """
        async with cls._lock:
            token = cls._tokens.get(name)
            if not token:
                return None
            if token["expiry"] and token["expiry"] < time.time():
                return None  # expired
            return token["value"]

    @classmethod
    async def all_tokens(cls) -> Dict[str, Dict]:
        """
        Return all stored tokens (for debugging).
        """
        async with cls._lock:
            return cls._tokens.copy()