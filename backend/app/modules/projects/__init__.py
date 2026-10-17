"""Projects: Project creation, the ``ProjectAccess`` authorization module, membership and
invitations, lifecycle and deletion coordination (T05-T07).

This is the module's public interface; other modules import only from here. Every
repository in the system takes the ``ProjectScope``/``ScopeSet`` minted here.
"""

from app.modules.projects import jobs as _jobs  # registers the module's job handlers
from app.modules.projects.authorization import (
    AccessSummary,
    ProjectAccess,
    ProjectAccessAdapter,
    ProjectMembershipSummary,
    ProjectScope,
    ScopeSet,
    scope_key,
)
from app.modules.projects.domain import (
    ACTIVE,
    AccessPurpose,
    ActivityKind,
    Capability,
    InvitationState,
    Landing,
    LandingKind,
    Lifecycle,
    ProjectRole,
    Visibility,
)
from app.modules.projects.invitations import (
    AcceptanceResult,
    AttemptLimiter,
    InvitationContext,
    Invitations,
    InvitationView,
)
from app.modules.projects.jobs import INVITATION_EXPIRE
from app.modules.projects.lifecycle import (
    ARCHIVAL_EXECUTE,
    PROJECT_DELETE,
    DeletionReportView,
    DeletionRequestResult,
    LifecycleHook,
    ProjectLifecycle,
    ProjectPurger,
    PurgeReport,
    StoreReportView,
    SuspendReason,
    VerifiedNoopPurger,
)
from app.modules.projects.membership import (
    Membership,
    MembershipView,
    PeoplePurpose,
    PersonRef,
    RoleChangePreview,
    people_capability,
)
from app.modules.projects.service import (
    ArchivalScheduleView,
    DeletionView,
    ProjectActivity,
    ProjectPatch,
    Projects,
    ProjectView,
)

del _jobs

__all__ = [
    "ACTIVE",
    "ARCHIVAL_EXECUTE",
    "INVITATION_EXPIRE",
    "PROJECT_DELETE",
    "AcceptanceResult",
    "AccessPurpose",
    "AccessSummary",
    "ActivityKind",
    "ArchivalScheduleView",
    "AttemptLimiter",
    "Capability",
    "DeletionReportView",
    "DeletionRequestResult",
    "DeletionView",
    "InvitationContext",
    "InvitationState",
    "InvitationView",
    "Invitations",
    "Landing",
    "LandingKind",
    "Lifecycle",
    "LifecycleHook",
    "Membership",
    "MembershipView",
    "PeoplePurpose",
    "PersonRef",
    "ProjectAccess",
    "ProjectAccessAdapter",
    "ProjectActivity",
    "ProjectLifecycle",
    "ProjectMembershipSummary",
    "ProjectPatch",
    "ProjectPurger",
    "ProjectRole",
    "ProjectScope",
    "ProjectView",
    "Projects",
    "PurgeReport",
    "RoleChangePreview",
    "ScopeSet",
    "StoreReportView",
    "SuspendReason",
    "VerifiedNoopPurger",
    "Visibility",
    "people_capability",
    "scope_key",
]
