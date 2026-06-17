"""Auth domain constants.

Single source of truth for RBAC markers shared across the service layer and
out-of-band tooling (e.g. ``scripts/seed_admin.py``).
"""

from __future__ import annotations

# Role code that confers super-admin. A user holding an active role with this
# code bypasses every interface-permission gate and data-scope restriction —
# the same effect as the wildcard ``*:*:*`` permission, but carried by the role
# identity itself (bootstrap-safe: no dependency on a menu/role_menu link).
SUPERADMIN_ROLE_CODE = "superadmin"
