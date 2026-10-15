"""The prototype's synthetic personas (frontend-prototype-instructions §personas, GOV-08).

All names, emails, and subjects are synthetic. Team Members use the firm domain; Jordan is
a Client Collaborator on a client domain and activates only through an invitation (T06).
``OPS`` is the Platform Operator, recognised by subject from configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

FIRM_DOMAIN: Final = "relaxai-consulting.test"
CLIENT_DOMAIN: Final = "atlas-client.test"
TENANT_ID: Final = "00000000-0000-4000-8000-00000000aaaa"
OTHER_TENANT_ID: Final = "00000000-0000-4000-8000-00000000bbbb"


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    display_name: str
    email: str
    subject: str
    team_member: bool = True
    firm_manager: bool = False
    platform_operator: bool = False


AVERY: Final = Persona("avery", "Avery Chen", f"avery.chen@{FIRM_DOMAIN}", "sub-avery-0001")
MORGAN: Final = Persona("morgan", "Morgan Lee", f"morgan.lee@{FIRM_DOMAIN}", "sub-morgan-0002")
RILEY: Final = Persona("riley", "Riley Park", f"riley.park@{FIRM_DOMAIN}", "sub-riley-0003")
JORDAN: Final = Persona(
    "jordan", "Jordan Kim", f"jordan.kim@{CLIENT_DOMAIN}", "sub-jordan-0004", team_member=False
)
SAM: Final = Persona(
    "sam", "Sam Rivera", f"sam.rivera@{FIRM_DOMAIN}", "sub-sam-0005", firm_manager=True
)
TAYLOR: Final = Persona(
    "taylor", "Taylor Morgan", f"taylor.morgan@{FIRM_DOMAIN}", "sub-taylor-0006",
    firm_manager=True,
)  # fmt: skip
OPS: Final = Persona(
    "ops", "Operations Desk", f"ops@{FIRM_DOMAIN}", "sub-ops-0007", platform_operator=True
)

TEAM_MEMBERS: Final = (AVERY, MORGAN, RILEY, SAM, TAYLOR)
ALL: Final = (AVERY, MORGAN, RILEY, JORDAN, SAM, TAYLOR, OPS)
