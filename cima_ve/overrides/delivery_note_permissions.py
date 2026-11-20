from __future__ import annotations

"""Restringe la lectura de Notas de Entrega: administradores y caja ven todas; vendedores ven las suyas."""

ADMIN_ROLES = {"System Manager", "Sales Manager", "Accounts Manager", "Caja CIMA"}


def _is_admin(user: str, roles: list[str] | None = None) -> bool:
    if not user or user == "Guest":
        return False
    if user == "Administrator":
        return True
    if roles is None:
        import frappe

        roles = frappe.get_roles(user)
    return bool(set(roles or []) & ADMIN_ROLES)


def get_permission_query_conditions(user: str | None = None, doctype: str | None = None) -> str:
    import frappe

    user = user or frappe.session.user
    if not user or user == "Guest":
        return "1=0"
    if _is_admin(user):
        return ""
    escaped = frappe.db.escape(user.strip())
    return (
        f"(LOWER(COALESCE(NULLIF(`tabDelivery Note`.`ve_vendedor_usuario`, ''), "
        f"`tabDelivery Note`.`owner`)) = LOWER({escaped}))"
    )


def has_permission(doc, ptype: str | None = None, user: str | None = None, **kwargs) -> bool | None:
    import frappe

    if not doc or isinstance(doc, str):
        return None

    user = user or frappe.session.user
    if not user or user == "Guest":
        return False
    if _is_admin(user):
        return None

    u = (user or "").strip().lower()
    seller = (getattr(doc, "ve_vendedor_usuario", None) or "").strip().lower() or (getattr(doc, "owner", None) or "").strip().lower()
    if seller and seller == u:
        return None

    seller_name = (getattr(doc, "ve_vendedor_nombre", None) or "").strip().lower()
    user_fullname = (
        frappe.utils.get_fullname(user)
        if hasattr(frappe, "utils") and hasattr(frappe.utils, "get_fullname")
        else ""
    ).strip().lower()
    if seller_name and user_fullname:
        if user_fullname == seller_name or seller_name in user_fullname or user_fullname in seller_name:
            return None

    user_prefix = u.split("@")[0].strip()
    if seller_name and user_prefix and len(user_prefix) >= 3 and (user_prefix in seller_name or seller_name in user_prefix):
        return None

    for row in getattr(doc, "sales_team", []) or []:
        sp = (getattr(row, "sales_person", "") or "").strip().lower()
        if sp and (sp == u or (user_fullname and (sp == user_fullname or sp in user_fullname or user_fullname in sp))):
            return None

    return False
