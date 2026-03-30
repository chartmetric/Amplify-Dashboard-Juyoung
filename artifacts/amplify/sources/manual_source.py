from sources.base import SourceAdapter, FeatureContext


class ManualSource(SourceAdapter):
    def __init__(self):
        self._features: list[FeatureContext] = []

    def connect(self) -> bool:
        return True

    def add_feature(self, title: str, description: str, priority: str = None, tags: list[str] = None):
        feature = FeatureContext(
            title=title,
            description=description,
            source="manual",
            priority=priority,
            tags=tags or [],
        )
        self._features.append(feature)

    def fetch_features(self) -> list[FeatureContext]:
        return self._features

    def get_source_name(self) -> str:
        return "manual"
