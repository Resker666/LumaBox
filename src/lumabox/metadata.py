"""Best-effort, bounded display metadata for an identified work."""
import unicodedata


def normalize_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(" " if char.isspace() else char for char in value
        if char.isspace() or not unicodedata.category(char).startswith("C"))
    return " ".join(cleaned.split())[:300].strip() or None
