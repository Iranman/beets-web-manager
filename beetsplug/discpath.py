import re

from beets.plugins import BeetsPlugin


_BAD_PATH_CHARS = re.compile(r'[\\/:*?"<>|]+')


def _to_int(value) -> int:
    try:
        text = str(value or "").strip()
        return int(text) if text else 0
    except Exception:
        return 0


def _safe_segment(value: str) -> str:
    cleaned = _BAD_PATH_CHARS.sub("_", str(value or "").strip())
    return cleaned or "Disc"


class DiscPathPlugin(BeetsPlugin):
    """Template field for optional multi-disc album subfolders."""

    def __init__(self):
        super().__init__()
        self.template_fields["disc_subfolder"] = self.disc_subfolder

    def disc_subfolder(self, item) -> str:
        disc = _to_int(getattr(item, "disc", 0)) or 1
        disctotal = _to_int(getattr(item, "disctotal", 0))
        if disctotal <= 1 and disc <= 1:
            return ""

        media = str(getattr(item, "media", "") or "").strip()
        label = media if media.lower() in {"cd", "vinyl", "cassette"} else "Disc"
        return f"{_safe_segment(label)} {disc:02d}/"
