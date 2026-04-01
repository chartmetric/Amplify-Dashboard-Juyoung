from datetime import datetime, timedelta, timezone
import logging
import re

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

WORKSPACE_GID = "1198197264916217"


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

    CUSTOM_FIELD_MAP = {
        "Engineer": "engineer",
        "Team": "team",
        "Task Type": "task_type",
        "Planning Priority": "planning_priority",
        "Urgency Score": "urgency_score",
        "Slack URL": "slack_url",
        "PR Preview Link": "pr_preview_link",
        "Planner": "planner",
    }

    def _extract_custom_field_value(self, cf: dict):
        if cf.get("number_value") is not None:
            return cf["number_value"]
        if cf.get("enum_value") and cf["enum_value"].get("name"):
            return cf["enum_value"]["name"]
        if cf.get("display_value"):
            return cf["display_value"]
        if cf.get("text_value"):
            return cf["text_value"]
        return None

    def _parse_task(self, t: dict, source_label: str) -> dict:
        parsed_custom = {v: None for v in self.CUSTOM_FIELD_MAP.values()}

        for cf in (t.get("custom_fields") or []):
            cf_name = cf.get("name", "")
            if cf_name in self.CUSTOM_FIELD_MAP:
                val = self._extract_custom_field_value(cf)
                if val is not None:
                    parsed_custom[self.CUSTOM_FIELD_MAP[cf_name]] = val

        assignee_obj = t.get("assignee")
        assignee_name = None
        if isinstance(assignee_obj, dict):
            assignee_name = assignee_obj.get("name")

        return {
            "id": t.get("gid", ""),
            "title": t.get("name", ""),
            "description": t.get("notes", ""),
            "date": t.get("modified_at") or t.get("created_at", ""),
            "section": source_label,
            "assignee": assignee_name,
            **parsed_custom,
        }

    TASK_OPT_FIELDS = "name,completed,created_at,modified_at,notes,assignee,assignee.name,custom_fields,custom_fields.name,custom_fields.display_value,custom_fields.number_value,custom_fields.enum_value,custom_fields.enum_value.name,custom_fields.text_value"

    def _get_tasks_for_section(self, section_gid: str, section_name: str) -> list[dict]:
        client = self._get_client()
        tasks_api = asana.TasksApi(client)
        opts = {
            "opt_fields": self.TASK_OPT_FIELDS,
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
            "opt_fields": self.TASK_OPT_FIELDS + ",completed_at",
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

    def enrich_feature(self, feature: dict) -> dict:
        asana_task_id = feature.get("asana_task_id")
        title = feature.get("title", "")

        task_data = None
        match_method = "none"

        if asana_task_id:
            task_data = self._fetch_task_by_id(asana_task_id)
            if task_data:
                match_method = "url"

        if not task_data and title:
            task_data = self._search_task_by_title(title)
            if task_data:
                match_method = "search"

        if not task_data:
            feature["asana_linked"] = False
            feature["asana_match_method"] = "none"
            return feature

        feature["asana_linked"] = True
        feature["asana_match_method"] = match_method
        feature["source"] = "slack+asana"

        if task_data.get("gid") and not feature.get("asana_task_id"):
            feature["asana_task_id"] = task_data["gid"]

        if task_data.get("description") and not feature.get("description"):
            feature["description"] = task_data["description"]
        elif task_data.get("description"):
            feature["description"] = task_data["description"]

        if task_data.get("asana_url") and not feature.get("asana_url"):
            feature["asana_url"] = task_data["asana_url"]

        for field in ["engineer", "assignee", "team", "task_type", "urgency_score", "planner", "planning_priority"]:
            if task_data.get(field) and not feature.get(field):
                feature[field] = task_data[field]

        if task_data.get("subtasks"):
            feature["subtasks"] = task_data["subtasks"]
        if task_data.get("comments"):
            feature["comments"] = task_data["comments"]
        if task_data.get("project_info"):
            feature["project_info"] = task_data["project_info"]
        if task_data.get("github_pr_urls"):
            feature["github_pr_urls"] = task_data["github_pr_urls"]

        return feature

    def _fetch_task_by_id(self, task_gid: str) -> dict | None:
        try:
            client = self._get_client()
            tasks_api = asana.TasksApi(client)
            task = tasks_api.get_task(task_gid, {
                "opt_fields": "name,notes,custom_fields,custom_fields.name,custom_fields.display_value,custom_fields.number_value,custom_fields.enum_value,custom_fields.enum_value.name,custom_fields.text_value,permalink_url,assignee,assignee.name,memberships,memberships.project,memberships.project.name,memberships.section,memberships.section.name",
            })
            t = task.to_dict() if hasattr(task, "to_dict") else task
            return self._build_task_data(t)
        except Exception as e:
            logger.warning(f"Failed to fetch Asana task {task_gid}: {e}")
            return None

    def _search_task_by_title(self, title: str) -> dict | None:
        try:
            if re.match(r"^<?https?://", title):
                return None

            client = self._get_client()
            tasks_api = asana.TasksApi(client)

            search_text = re.sub(r'<https?://[^>]+>', '', title)
            search_text = re.sub(r'https?://\S+', '', search_text)
            search_text = re.sub(r'[^\w\s]', '', search_text).strip()
            if len(search_text) < 3:
                return None

            if len(search_text) > 80:
                search_text = search_text[:80]

            opts = {
                "text": search_text,
                "opt_fields": "name,notes,custom_fields,custom_fields.name,custom_fields.display_value,custom_fields.number_value,custom_fields.enum_value,custom_fields.enum_value.name,custom_fields.text_value,permalink_url,assignee,assignee.name,memberships,memberships.project,memberships.project.name,memberships.section,memberships.section.name",
                "resource_subtype": "default_task",
            }

            results = list(tasks_api.search_tasks_for_workspace(WORKSPACE_GID, opts))

            if not results:
                return None

            title_lower = title.lower().strip()
            for r in results[:10]:
                t = r.to_dict() if hasattr(r, "to_dict") else r
                task_name = (t.get("name") or "").lower().strip()
                if title_lower in task_name or task_name in title_lower:
                    return self._build_task_data(t)

            t = results[0]
            t = t.to_dict() if hasattr(t, "to_dict") else t
            task_name = (t.get("name") or "").lower().strip()

            title_words = set(re.findall(r'\w+', title_lower))
            task_words = set(re.findall(r'\w+', task_name))
            if title_words and task_words:
                overlap = len(title_words & task_words) / max(len(title_words), 1)
                if overlap >= 0.5:
                    return self._build_task_data(t)

            return None

        except Exception as e:
            logger.warning(f"Asana search failed for '{title[:50]}': {e}")
            return None

    def _build_task_data(self, t: dict) -> dict:
        parsed_custom = {v: None for v in self.CUSTOM_FIELD_MAP.values()}
        for cf in (t.get("custom_fields") or []):
            cf_name = cf.get("name", "")
            if cf_name in self.CUSTOM_FIELD_MAP:
                val = self._extract_custom_field_value(cf)
                if val is not None:
                    parsed_custom[self.CUSTOM_FIELD_MAP[cf_name]] = val

        assignee_obj = t.get("assignee")
        assignee_name = None
        if isinstance(assignee_obj, dict):
            assignee_name = assignee_obj.get("name")

        project_info = []
        for m in (t.get("memberships") or []):
            proj = m.get("project") or {}
            sec = m.get("section") or {}
            project_info.append({
                "project_name": proj.get("name", ""),
                "section_name": sec.get("name", ""),
            })

        subtasks = []
        comments = []
        github_pr_urls = []
        try:
            client = self._get_client()
            tasks_api = asana.TasksApi(client)
            for st in tasks_api.get_subtasks_for_task(t.get("gid", ""), {"opt_fields": "name,completed"}):
                s = st.to_dict() if hasattr(st, "to_dict") else st
                subtasks.append({"name": s.get("name", ""), "completed": s.get("completed", False)})
        except Exception:
            pass

        try:
            stories_api = asana.StoriesApi(self._get_client())
            for story in stories_api.get_stories_for_task(t.get("gid", ""), {"opt_fields": "text,type"}):
                s = story.to_dict() if hasattr(story, "to_dict") else story
                if s.get("type") == "comment" and s.get("text"):
                    comments.append(s["text"])
        except Exception:
            pass

        try:
            attach_api = asana.AttachmentsApi(self._get_client())
            for att in attach_api.get_attachments_for_object(t.get("gid", ""), {"opt_fields": "name,resource_subtype,host,view_url", "parent": t.get("gid", "")}):
                a = att.to_dict() if hasattr(att, "to_dict") else att
                if a.get("host") == "external" and a.get("resource_subtype") == "external":
                    view_url = a.get("view_url", "")
                    if "github.com" in view_url and "/pull/" in view_url:
                        github_pr_urls.append(view_url)
        except Exception:
            pass

        return {
            "gid": t.get("gid", ""),
            "description": t.get("notes", ""),
            "asana_url": t.get("permalink_url", ""),
            "assignee": assignee_name,
            "project_info": project_info,
            "subtasks": subtasks,
            "comments": comments,
            "github_pr_urls": github_pr_urls,
            **parsed_custom,
        }

    def list_unannounced_tasks(self, days: int = 30, announced_task_ids: set = None) -> list[dict]:
        if announced_task_ids is None:
            announced_task_ids = set()

        all_tasks = self.list_recent_features()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        unannounced = []
        for task in all_tasks:
            task_id = task.get("id", "")
            if task_id in announced_task_ids:
                continue

            task_date_str = task.get("date", "")
            if task_date_str:
                try:
                    task_date = datetime.fromisoformat(task_date_str.replace("Z", "+00:00"))
                    if task_date < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            task["source"] = "asana_only"
            task["released"] = False
            task["asana_linked"] = True
            task["asana_url"] = None
            task["slack_url"] = None
            task["github_url"] = None
            task["release_version"] = {"fe": None, "be": None}
            task["release_date"] = task.get("date", "")
            task["source_prefix"] = task.get("section", "").split("/")[0] if task.get("section") else None
            task["total_reactions"] = 0
            task["reactions_breakdown"] = {}
            unannounced.append(task)

        logger.info(f"[asana-only] Found {len(unannounced)} unannounced tasks (out of {len(all_tasks)} total, {len(announced_task_ids)} already announced)")
        return unannounced

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
