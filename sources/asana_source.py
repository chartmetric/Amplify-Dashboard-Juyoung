import asana
from sources.base import SourceAdapter, FeatureContext
import config

class AsanaSource(SourceAdapter):
    def __init__(self, project_gid: str):
        self.client = asana.Client.access_token(config.ASANA_ACCESS_TOKEN)
        self.client.headers = {"asana-enable": "new_goals,new_user_task_lists"}
        self.project_gid = project_gid

    def list_recent_features(self) -> list[dict]:
        tasks = self.client.tasks.get_tasks_for_project(
            self.project_gid,
            opt_fields=["name", "completed", "created_at", "modified_at"],
            completed_since="now"  # only incomplete tasks
        )
        results = []
        for task in tasks:
            results.append({
                "id": task["gid"],
                "title": task["name"],
                "date": task.get("modified_at", ""),
            })
        return results[:20]  # most recent 20

    def get_feature_context(self, feature_id: str) -> FeatureContext:
        # Get the task details
        task = self.client.tasks.get_task(
            feature_id,
            opt_fields=["name", "notes", "custom_fields", "permalink_url"]
        )

        # Get comments/stories for extra context
        stories = self.client.stories.get_stories_for_task(
            feature_id,
            opt_fields=["text", "type", "created_by.name"]
        )
        comments = []
        for story in stories:
            if story.get("type") == "comment" and story.get("text"):
                author = story.get("created_by", {}).get("name", "Unknown")
                comments.append(f"{author}: {story['text']}")

        raw_details = "\n---\n".join(comments[-10:])  # last 10 comments

        return FeatureContext(
            title=task["name"],
            description=task.get("notes", ""),
            raw_details=raw_details,
            source_type="asana",
            metadata={
                "url": task.get("permalink_url", ""),
                "custom_fields": task.get("custom_fields", []),
            }
        )