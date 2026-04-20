from sources.base import SourceAdapter, FeatureContext
from sources.slack_source import is_low_quality_title

LOW_QUALITY_TITLE_MESSAGE = "This title looks too short or generic — please add more detail."


def validate_manual_title(title: str) -> str | None:
    """Return an error message if the title fails the low-quality gate, else None."""
    if is_low_quality_title(title or ""):
        return LOW_QUALITY_TITLE_MESSAGE
    return None


class ManualSource(SourceAdapter):
    def list_recent_features(self) -> list[dict]:
        return []

    def get_feature_context(self, feature_id: str = "", **kwargs) -> FeatureContext:
        title = kwargs.get("title", "")
        description = kwargs.get("description", "")
        return FeatureContext(
            title=title,
            description=description,
            source_type="manual",
        )
