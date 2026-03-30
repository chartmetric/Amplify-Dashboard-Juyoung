from sources.base import SourceAdapter, FeatureContext


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
