from datetime import datetime, timedelta, timezone
import logging

import asana
import config
from sources.base import SourceAdapter, FeatureContext

logger = logging.getLogger("amplify.asana")

PROJECTS = {
    "devin": {
        "project_gid": "1213445772342530",
        "sections": {
            "merged_to_prod": "1213443514830854",
            "archive": "1213485992807619",
        },
        "fetch_mode": "sections",
    },
    "pe": {
        "project_gid": "1206107750513843",
        "fetch_mode": "completed",
    },
}


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

    def _parse_task(self, t: dict, source_label: str) -> dict:
        urgency_score = None
        for cf in (t.get("custom_fields") or []):
            if cf.get("name") == "Urgency Score":
                urgency_score = cf.get("display_value") or cf.get("number_value")
                break

        return {
            "id": t.get("gid", ""),
            "title": t.get("name", ""),
            "description": t.get("notes", ""),
            "date": t.get("modified_at") or t.get("created_at", ""),
            "urgency_score": urgency_score,
            "section": source_label,
        }

    def _get_tasks_for_section(self, section_gid: str, section_name: str) -> list[dict]:
        client = self._get_client()
        tasks_api = asana.TasksApi(client)
        opts = {
            "opt_fields": "name,completed,created_at,modified_at,notes,custom_fields",
        }
        results = []
        try:
            for task in tasks_api.get_tasks_for_section(section_gid, opts):
                t = task.to_dict() if hasattr(task, "to_dict") else task
                results.append(self._parse_task(t, section_name))
        except Exception as e:
            logger.error(f"Error fetching tasks from section {section_name}: {e}")
        return results

    def _get_completed_tasks_for_project(self, project_gid: str, project_label: str) -> list[dict]:
        client = self._get_client()
        tasks_api = asana.TasksApi(client)
        since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        opts = {
            "opt_fields": "name,completed,completed_at,created_at,modified_at,notes,custom_fields",
            "completed_since": since,
        }
        results = []
        try:
            for task in tasks_api.get_tasks_for_project(project_gid, opts):
                t = task.to_dict() if hasattr(task, "to_dict") else task
                if t.get("completed"):
                    results.append(self._parse_task(t, project_label))
        except Exception as e:
            logger.error(f"Error fetching completed tasks from project {project_label}: {e}")
        return results

    def list_recent_features(self) -> list[dict]:
        all_results = []

        for name, proj in PROJECTS.items():
            if proj["fetch_mode"] == "sections":
                for section_key, section_gid in proj["sections"].items():
                    label = f"{name}/{section_key}"
                    tasks = self._get_tasks_for_section(section_gid, label)
                    logger.info(f"  {label}: {len(tasks)} tasks")
                    all_results.extend(tasks)
            elif proj["fetch_mode"] == "completed":
                label = f"{name}/completed"
                tasks = self._get_completed_tasks_for_project(proj["project_gid"], label)
                logger.info(f"  {label}: {len(tasks)} tasks")
                all_results.extend(tasks)

        logger.info(f"Total tasks fetched across all projects: {len(all_results)}")
        return all_results

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
