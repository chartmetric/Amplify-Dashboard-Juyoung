from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FeatureContext:
    title: str
    description: str
    source: str
    priority: Optional[str] = None
    status: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "title": self.title,
            "description": self.description,
            "source": self.source,
            "priority": self.priority,
            "status": self.status,
            "tags": self.tags,
        }


class SourceAdapter(ABC):
    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def fetch_features(self) -> list[FeatureContext]:
        pass

    @abstractmethod
    def get_source_name(self) -> str:
        pass
