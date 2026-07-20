"""
UI / route capability flags from the RBAC matrix (functional spec §4).

Used by templates and `maintenance.context_processors` so navigation and actions
only appear when the signed-in role is allowed.
"""

from __future__ import annotations

from typing import Any, Dict

from django.contrib.auth.models import AnonymousUser

from accounts.models import User

_ALL_KEYS = (
    "super_full",
    "view_issues",
    "report_issue",
    "validate_issue",
    "view_work_orders",
    "create_work_order",
    "assign_technician",
    "issue_parts_to_wo",
    "request_part_on_wo",
    "approve_part_request",
    "raise_part_shortage_report",
    "decide_part_shortage_report",
    "request_external_repair",
    "approve_external_repair_request",
    "close_or_review_wo",
    "execute_work_order",
    "view_stock",
    "stock_in",
    "consume_consumables",
    "issue_consumables",
    "view_consumables",
    "create_purchase_request",
    "view_procurement_requests",
    "view_purchase_orders",
    "procurement_officer_update",
    "procurement_receive",
    "pm_schedule_manage",
    "tool_page",
    "tool_assign",
    "tool_return",
    "emergency_wo",
    "repair_create",
    "repair_list",
    "repair_officer",
    "repair_manager_accept",
    "reports",
    "kpi_dashboard",
    "audit_log",
    "quick_log",
    "manage_system_users",
    "issue_archive",
    "machine_manage",
)


def get_mms_capabilities(user: Any) -> Dict[str, bool]:
    """
    Return stable boolean keys for templates (prefix with `perm_` in context).

    "Super Admin" / Django superuser: full bypass where the matrix grants Admin ✅.
    """
    false = {k: False for k in _ALL_KEYS}

    if not user or isinstance(user, AnonymousUser) or not user.is_authenticated:
        return false

    r = getattr(user, "role", "") or ""
    su = bool(user.is_superuser)
    full = bool(getattr(user, "is_super_admin_role", lambda: False)())

    def role_in(*roles: str) -> bool:
        return full or r in roles

    # --- Issues ---
    view_issues = role_in(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.MANAGER)
    report_issue = role_in(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.MANAGER)
    validate_issue = role_in(User.Role.SUPERVISOR, User.Role.MANAGER)
    issue_archive = role_in(User.Role.MANAGER, User.Role.SUPER_ADMIN)

    # --- Work orders (matrix: queue = technician + manager + supervisor (read-only)) ---
    view_work_orders = role_in(User.Role.TECHNICIAN, User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.PROCUREMENT)
    create_work_order = role_in(User.Role.MANAGER)
    assign_technician = role_in(User.Role.MANAGER)
    issue_parts_to_wo = role_in(User.Role.MANAGER)
    # Phase 2.1: hybrid approval — technician requests (PENDING), manager approves.
    # Own-WO enforcement is done in the view, not the capability flag.
    request_part_on_wo = role_in(User.Role.TECHNICIAN)
    # Phase 2.1: only MANAGER approves (single approval authority, parts = money).
    approve_part_request = role_in(User.Role.MANAGER)
    # --- Sprint 1: part shortage flow ---
    # Tech (or manager) can press the "📦 Raise Shortage Request" button.
    raise_part_shortage_report = role_in(User.Role.TECHNICIAN, User.Role.MANAGER)
    # Manager (or super admin) can approve or reject a PartShortageReport.
    decide_part_shortage_report = role_in(User.Role.MANAGER, User.Role.SUPER_ADMIN)
    # Phase 2.2: technician requests external repair on own WO; manager reviews/approves.
    # Own-WO enforcement is done in the view, not the capability flag.
    request_external_repair = role_in(User.Role.TECHNICIAN)
    approve_external_repair_request = role_in(User.Role.MANAGER)
    close_or_review_wo = role_in(User.Role.MANAGER)
    execute_work_order = role_in(User.Role.TECHNICIAN) or full

    # --- Inventory ---
    view_stock = role_in(User.Role.MANAGER, User.Role.PROCUREMENT)
    stock_in = role_in(User.Role.MANAGER, User.Role.PROCUREMENT)
    # Split consumables: self-consume vs issue-to-others vs view
    consume_consumables = role_in(User.Role.OPERATOR, User.Role.TECHNICIAN)
    issue_consumables = role_in(User.Role.SUPERVISOR, User.Role.MANAGER)
    view_consumables = role_in(User.Role.OPERATOR, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.MANAGER)

    # --- Procurement (web) ---
    create_purchase_request = role_in(User.Role.MANAGER)
    view_procurement_requests = role_in(
        User.Role.MANAGER,
        User.Role.PROCUREMENT,
        User.Role.SUPERVISOR,
    )
    view_purchase_orders = role_in(
        User.Role.MANAGER,
        User.Role.PROCUREMENT,
        User.Role.SUPER_ADMIN,
    )
    procurement_officer_update = role_in(User.Role.PROCUREMENT)
    procurement_receive = role_in(User.Role.MANAGER, User.Role.PROCUREMENT)

    # --- PM schedules (manager creates) ---
    pm_schedule_manage = role_in(User.Role.MANAGER)

    # --- Tools: manager/supervisor assign to anyone; technician/operator self-assign ---
    tool_page = role_in(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.OPERATOR)
    tool_assign = role_in(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.OPERATOR)
    tool_return = role_in(User.Role.MANAGER, User.Role.SUPERVISOR, User.Role.TECHNICIAN, User.Role.OPERATOR)

    # --- Emergency / repairs ---
    emergency_wo = role_in(User.Role.MANAGER)
    repair_create = role_in(User.Role.MANAGER)
    repair_list = role_in(User.Role.MANAGER, User.Role.PROCUREMENT, User.Role.SUPERVISOR)
    repair_officer = role_in(User.Role.PROCUREMENT)
    repair_manager_accept = role_in(User.Role.MANAGER)

    # --- Reporting (matrix: all except operator) ---
    reports = role_in(
        User.Role.SUPERVISOR,
        User.Role.TECHNICIAN,
        User.Role.MANAGER,
        User.Role.PROCUREMENT,
    )
    kpi_dashboard = reports
    # Matrix: Audit logs — Admin column (super admin role or Django superuser).
    audit_log = (getattr(user, "role", None) == User.Role.SUPER_ADMIN) or su

    quick_log = role_in(
        User.Role.OPERATOR,
        User.Role.SUPERVISOR,
        User.Role.TECHNICIAN,
        User.Role.MANAGER,
    )

    # MMS UI: create/list users (same gate as Django admin User model).
    manage_system_users = full
    machine_manage = role_in(User.Role.MANAGER)

    return {
        "super_full": full or su,
        "view_issues": view_issues,
        "report_issue": report_issue,
        "validate_issue": validate_issue,
        "view_work_orders": view_work_orders,
        "create_work_order": create_work_order,
        "assign_technician": assign_technician,
        "issue_parts_to_wo": issue_parts_to_wo,
        "request_part_on_wo": request_part_on_wo,
        "approve_part_request": approve_part_request,
        "raise_part_shortage_report": raise_part_shortage_report,
        "decide_part_shortage_report": decide_part_shortage_report,
        "request_external_repair": request_external_repair,
        "approve_external_repair_request": approve_external_repair_request,
        "close_or_review_wo": close_or_review_wo,
        "execute_work_order": execute_work_order,
        "view_stock": view_stock,
        "stock_in": stock_in,
        "consume_consumables": consume_consumables,
        "issue_consumables": issue_consumables,
        "view_consumables": view_consumables,
        "create_purchase_request": create_purchase_request,
        "view_procurement_requests": view_procurement_requests,
        "view_purchase_orders": view_purchase_orders,
        "procurement_officer_update": procurement_officer_update,
        "procurement_receive": procurement_receive,
        "pm_schedule_manage": pm_schedule_manage,
        "tool_page": tool_page,
        "tool_assign": tool_assign,
        "tool_return": tool_return,
        "emergency_wo": emergency_wo,
        "repair_create": repair_create,
        "repair_list": repair_list,
        "repair_officer": repair_officer,
        "repair_manager_accept": repair_manager_accept,
        "reports": reports,
        "kpi_dashboard": kpi_dashboard,
        "audit_log": audit_log,
        "quick_log": quick_log,
        "manage_system_users": manage_system_users,
        "issue_archive": issue_archive,
        "machine_manage": machine_manage,
    }

