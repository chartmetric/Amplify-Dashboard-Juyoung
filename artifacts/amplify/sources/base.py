from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class FeatureContext:
    title: str
    description: str
    raw_details: str = ""
    source_type: str = ""
    metadata: dict = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        lines = [
            f"Title: {self.title}",
            f"Description: {self.description}",
        ]
        if self.raw_details:
            lines.append(f"Raw Details: {self.raw_details}")
        if self.source_type:
            lines.append(f"Source: {self.source_type}")
        if self.metadata:
            lines.append(f"Metadata: {self.metadata}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "raw_details": self.raw_details,
            "source_type": self.source_type,
            "metadata": self.metadata,
        }


class SourceAdapter(ABC):
    @abstractmethod
    def list_recent_features(self) -> list[dict]:
        pass

    @abstractmethod
    def get_feature_context(self, feature_id: str, **kwargs) -> FeatureContext:
        pass
