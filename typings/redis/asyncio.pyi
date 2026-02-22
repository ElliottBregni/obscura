from typing import Any

from redis import Redis

def from_url(url: str, **kwargs: Any) -> Redis: ...
