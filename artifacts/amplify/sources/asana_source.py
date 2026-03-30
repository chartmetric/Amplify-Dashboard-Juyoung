import asana
from sources.base import SourceAdapter, FeatureContext
from config import Config


class AsanaSource(SourceAdapter):
    def __init__(self, project_gid: str = ""):
        self.project_gid = project_gid
        self.client = None

    def connect(self) -> bool:
        token = Config.ASANA_ACCESS_TOKEN
        if not token:
            return False
        self.client = asana.Client.access_token(token)
        self.client.headers = {"asana-enable": "new_goal_memberships,new_user_task_lists"}
        return True

    def fetch_features(self) -> list[FeatureContext]:
        if not self.client or not self.project_gid:
            return []

        features = []
        tasks = self.client.tasks.get_tasks_for_project(
            self.project_gid,
            opt_fields=["name", "notes", "tags.name", "custom_fields"],
        )

        for task in tasks:
            feature = FeatureContext(
                title=task.get("name", ""),
                description=task.get("notes", ""),
                source="asana",
                tags=[t["name"] for t in task.get("tags", [])],
                raw_data=task,
            )
            features.append(feature)

        return features

    def get_source_name(self) -> str:
        return "asana"
