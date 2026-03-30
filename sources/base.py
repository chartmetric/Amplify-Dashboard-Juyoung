from dataclasses import dataclass, field
from typing import Optional
from abc import ABC, abstractmethod

@dataclass
class FeatureContext:
    title: str
    description: str
    raw_details: str = ""
    source_type: str = ""          # "asana", "slack", "codebase", "manual"
    metadata: dict = field(default_factory=dict)  # links, authors, dates

    def to_prompt_block(self) -> str:
        """Formats this context for Claude's prompt."""
        return f"""Feature: {self.title}
Description: {self.description}
Additional Context: {self.raw_details}
Source: {self.source_type}"""

class SourceAdapter(ABC):
    @abstractmethod
    def list_recent_features(self) -> list[dict]:
        """Returns list of {id, title, date} for the UI dropdown."""
        pass

    @abstractmethod
    def get_feature_context(self, feature_id: str) -> FeatureContext:
        """Pulls full context for a specific feature."""
        pass