"""Stable content identities for deliverables and approvals."""

import hashlib
import json

from pydantic import BaseModel


def content_hash(obj: BaseModel | dict, exclude: set[str] | None = None) -> str:
    data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
    data = {key: value for key, value in data.items() if key not in (exclude or set())}
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
