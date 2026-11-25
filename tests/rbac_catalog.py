"""Catálogo RBAC de teste — espelha o seed da migration V1 da API Java.

Os fakes de /api/auth/me usam este mapa para devolver `permissionKeys`,
exatamente como a API Java real faz desde a inclusão das permissões efetivas
na resposta de perfil.
"""

ALL_PERMISSIONS = [
    "overview.view", "plantations.manage", "plantations.read.all", "monitoring.view",
    "analytics.view", "models.manage", "reports.view", "users.manage", "rbac.manage",
    "security.manage", "logs.read", "settings.manage", "design.manage", "integrations.manage",
]

ROLE_PERMISSIONS = {
    "PRODUCER": [
        "overview.view", "plantations.manage", "monitoring.view", "analytics.view", "reports.view",
    ],
    "ANALYST": [
        "overview.view", "plantations.read.all", "monitoring.view", "analytics.view",
        "models.manage", "reports.view", "integrations.manage",
    ],
    "ADMIN": list(ALL_PERMISSIONS),
}


def permissions_for(role_key: str) -> list[str]:
    return list(ROLE_PERMISSIONS.get(role_key.upper(), []))
