"""Explicit Azure DevOps scope; no organization, tenant, login or policy defaults."""
from dataclasses import dataclass
import re
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from .errors import BoardError


OPERATIONS = frozenset({"connect", "request", "item", "wiki", "link", "linked-report", "pull-request"})


@dataclass(frozen=True)
class AzurePolicy:
    organization: str
    project: str
    project_id: str
    authorize: object
    link_comment: str = "Workspace association"
    wiki_update_comment: str = "Reviewed page update"
    wiki_create_comment: str = "Reviewed page creation"

    def __post_init__(self):
        parsed = urlsplit(self.organization)
        if (parsed.scheme != "https" or parsed.netloc != "dev.azure.com"
                or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]*", parsed.path)
                or parsed.query or parsed.fragment):
            raise ValueError("An explicit canonical Azure DevOps organization is required.")
        if not isinstance(self.project, str) or not self.project.strip() or len(self.project) > 256 or any(ord(c) < 32 for c in self.project):
            raise ValueError("An explicit project is required.")
        object.__setattr__(self, "project_id", str(UUID(self.project_id)))
        if not callable(self.authorize):
            raise ValueError("An explicit provider authorization policy is required.")

    @property
    def web_base(self):
        return self.organization + "/" + quote(self.project, safe="")

    def require(self, operation, resource):
        if operation not in OPERATIONS or self.authorize(operation, resource) is not True:
            raise BoardError("The configured provider policy denied this operation.")

    def route(self, area, path):
        parsed = urlsplit(path)
        if (area not in ("wit", "wiki", "git") or parsed.scheme or parsed.netloc
                or parsed.query or parsed.fragment or path.startswith("/")
                or "\\" in unquote(path) or any(p in (".", "..") for p in unquote(path).split("/"))
                or any(ord(c) < 32 for c in path)):
            raise BoardError("Invalid provider route.")
        return self.web_base + "/_apis/" + area + "/" + path
