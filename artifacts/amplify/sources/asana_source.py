import asana
import config
from sources.base import SourceAdapter, FeatureContext


class AsanaSource(SourceAdapter):
    def __init__(self, project_gid: str):
        self.project_gid = project_gid
        self._client = None

    def _get_client(self):
        if self._client is None:
            token = config.ASANA_ACCESS_TOKEN
            if not token:
                raise RuntimeError("ASANA_ACCESS_TOKEN not set")
            configuration = asana.Configuration()
            configuration.access_token = token
            self._client = asana.ApiClient(configuration)
        return self._client

    def list_recent_features(self) -> list[dict]:
        client = self._get_client()
        tasks_api = asana.TasksApi(client)
        opts = {
            "opt_fields": "name,completed,created_at,modified_at,notes,custom_fields",
            "limit": 20,
        }
        results = []
        for task in tasks_api.get_tasks_for_project(self.project_gid, opts):
            t = task.to_dict() if hasattr(task, "to_dict") else task

            urgency_score = None
            for cf in (t.get("custom_fields") or []):
                if cf.get("name") == "Urgency Score":
                    urgency_score = cf.get("display_value") or cf.get("number_value")
                    break

            results.append({
                "id": t.get("gid", ""),
                "title": t.get("name", ""),
                "description": t.get("notes", ""),
                "date": t.get("modified_at") or t.get("created_at", ""),
                "urgency_score": urgency_score,
            })
        return results[:20]

    def get_feature_context(self, feature_id: str, **kwargs) -> FeatureContext:
        client = self._get_client()
        tasks_api = asana.TasksApi(client)
        stories_api = asana.StoriesApi(client)

        task = tasks_api.get_task(feature_id, {
            "opt_fields": "name,notes,custom_fields,permalink_url",
        })
        t = task.to_dict() if hasattr(task, "to_dict") else task

        comments = []
        try:
            for story in stories_api.get_stories_for_task(feature_id, {
                "opt_fields": "text,type",
            }):
                s = story.to_dict() if hasattr(story, "to_dict") else story
                if s.get("type") == "comment" and s.get("text"):
                    comments.append(s["text"])
        except Exception:
            pass

        custom_fields = {}
        for cf in (t.get("custom_fields") or []):
            name = cf.get("name", "")
            value = cf.get("display_value") or cf.get("text_value") or cf.get("number_value", "")
            if name and value:
                custom_fields[name] = value

        return FeatureContext(
            title=t.get("name", ""),
            description=t.get("notes", ""),
            raw_details="\n---\n".join(comments) if comments else "",
            source_type="asana",
            metadata={
                "permalink_url": t.get("permalink_url", ""),
                "custom_fields": custom_fields,
            },
        )
