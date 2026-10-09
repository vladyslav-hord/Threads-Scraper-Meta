from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class PostMedia:
    type: str
    url: str
    local_path: str = ""

@dataclass
class Post:
    id: str | None = None
    username: str | None = None
    text: str = ""
    timestamp: str | None = None
    permalink: str | None = None
    media: list[dict[str, Any]] = field(default_factory=list)
