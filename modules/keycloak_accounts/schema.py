"""The impersonation audit log.

One row per "impersonate this account" click -- who, as whom, when. Separate
from any audit trail the impersonated application keeps of what happens
afterwards: this table is the record of the impersonation *event itself*,
independent of whether the staff member went on to change anything.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, Table

from app.schema import metadata, timestamps

impersonation_log = Table(
    "impersonation_log", metadata,
    Column("id", Integer, primary_key=True),
    Column("impersonator_email", String(255), nullable=False, index=True),
    Column("impersonator_name", String(255)),
    Column("target_keycloak_id", String(64), nullable=False, index=True),
    Column("target_username", String(255)),
    Column("target_email", String(255)),
    *timestamps(),
)

TABLES = (impersonation_log,)
