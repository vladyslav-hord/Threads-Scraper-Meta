from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .parser import media_key, normalize_media_entry
def load_saved_posts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read saved posts: {path}") from exc
    if not isinstance(data, list) or not all(isinstance(post, dict) for post in data):
        raise RuntimeError(f"Invalid saved posts file: {path}")

    for post in data:
        media = post.get("media")
        unique_media: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in media if isinstance(media, list) else []:
            if not isinstance(item, dict):
                continue
            normalized = normalize_media_entry(item)
            if normalized is None:
                continue
            key = media_key(normalized["url"])
            if key in seen:
                continue
            seen.add(key)
            unique_media.append(normalized)
        post["media"] = unique_media
    return data



def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
