#!/usr/bin/env python3
import argparse
import copy
import csv
import getpass
import hashlib
import io
import json
import os
import platform
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(SCRIPT_DIR, "rules.yaml")
DEFAULT_STATE = os.path.join(SCRIPT_DIR, ".release_state.json")

PACKAGE_FORMAT_VERSION = "1.0.0"

try:
    import yaml
except ImportError:
    yaml = None


def _parse_yaml(path):
    if yaml:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    result = {}
    current_key = None
    current_list = None
    for line in text.splitlines():
        stripped = line.rstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        content = stripped.strip()
        if indent == 0 and content.endswith(":"):
            current_key = content[:-1]
            current_list = None
            result[current_key] = []
        elif indent > 0 and current_key and content.startswith("- "):
            result[current_key].append(content[2:].strip())
    return result


def load_rules(rules_path):
    if not os.path.exists(rules_path):
        print(f"[ERROR] rules file not found: {rules_path}")
        sys.exit(1)
    return _parse_yaml(rules_path)


def load_state(state_path):
    if not os.path.exists(state_path):
        return None
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state, state_path):
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _new_state(version, batch_id):
    return {
        "version": version,
        "draft_version": 0,
        "drafts": [],
        "confirmations": {},
        "migration_processed": {},
        "known_issues_reviewed": False,
        "imported_batches": [],
        "items": [],
        "migration_reminders": [],
        "known_issues": [],
        "approved": False,
        "approved_at_version": None,
        "approved_at_draft_version": None,
        "audit_log": [],
        "current_batch_id": batch_id,
        "pending_bulk_ops": [],
        "takeover_history": [],
        "pending_takeover": None,
        "confirmed_takeover_sessions": {},
        "rules_version": None,
        "rules_snapshot": None,
        "rules_upgrade_history": [],
        "pending_rules_upgrade": None,
        "rules_upgrade_handover_history": [],
        "imported_rules_upgrade_packages": [],
        "pending_rules_upgrade_import": None,
    }


def _compute_state_checksum(state):
    relevant = {
        "version": state.get("version"),
        "draft_version": state.get("draft_version"),
        "approved": state.get("approved"),
        "approved_at_version": state.get("approved_at_version"),
        "approved_at_draft_version": state.get("approved_at_draft_version"),
        "items": state.get("items", []),
        "drafts": state.get("drafts", []),
        "confirmations": state.get("confirmations", {}),
        "audit_log_len": len(state.get("audit_log", [])),
        "pending_bulk_ops_len": len(state.get("pending_bulk_ops", [])),
    }
    serialized = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _get_identity():
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return f"{user}@{platform.node()}"


def _build_handover_summary(state):
    items = state.get("items", [])
    migrations = state.get("migration_reminders", [])
    known_issues = state.get("known_issues", [])
    confirmations = state.get("confirmations", {})
    migration_processed = state.get("migration_processed", {})

    pending_sections = []
    confirmed_sections = []
    for sec in ["overview", "changes", "migration", "known_issues"]:
        if confirmations.get(sec, False):
            confirmed_sections.append(sec)
        else:
            pending_sections.append(sec)

    items_with_missing_owner = [
        it["id"] for it in items if not it.get("owner")
    ]
    items_with_invalid_risk = [
        it["id"] for it in items
        if it.get("risk_level") not in {"low", "medium", "high", "critical"}
    ]
    migrations_pending = [
        m["id"] for m in migrations
        if not migration_processed.get(m["id"])
    ]

    pending_bulk = state.get("pending_bulk_ops", [])
    unresolved_bulk = [
        i for i, p in enumerate(pending_bulk) if not p.get("resolved")
    ]

    suggested_next_steps = []
    if pending_sections:
        suggested_next_steps.append(
            f"Confirm sections: {', '.join(pending_sections)}"
        )
    if items_with_missing_owner:
        suggested_next_steps.append(
            f"Fill owner for items: {', '.join(items_with_missing_owner)}"
        )
    if items_with_invalid_risk:
        suggested_next_steps.append(
            f"Fix risk_level for items: {', '.join(items_with_invalid_risk)}"
        )
    if migrations_pending:
        suggested_next_steps.append(
            f"Process migrations: {', '.join(migrations_pending)}"
        )
    if not state.get("known_issues_reviewed", False) and known_issues:
        suggested_next_steps.append("Review known_issues section")
    if unresolved_bulk:
        suggested_next_steps.append(
            f"Resume bulk amend at indices: {unresolved_bulk}"
        )
    if not state.get("approved", False) and not pending_sections:
        suggested_next_steps.append("Run 'approve' to finalize release")

    return {
        "generated_at": datetime.now().isoformat(),
        "sections_pending": pending_sections,
        "sections_confirmed": confirmed_sections,
        "approval_status": "approved" if state.get("approved", False) else "pending",
        "draft_version": state.get("draft_version", 0),
        "items_total": len(items),
        "items_with_missing_owner": items_with_missing_owner,
        "items_with_invalid_risk": items_with_invalid_risk,
        "migration_total": len(migrations),
        "migrations_pending": migrations_pending,
        "known_issues_total": len(known_issues),
        "known_issues_reviewed": state.get("known_issues_reviewed", False),
        "unresolved_bulk_ops": unresolved_bulk,
        "total_bulk_ops": len(pending_bulk),
        "suggested_next_steps": suggested_next_steps,
    }


def _detect_takeover_conflicts(local_state, package, rules_path, existing_package_info=None):
    conflicts = []
    package_state = package["state"]

    if existing_package_info:
        if existing_package_info.get("package_checksum") != package.get("state_checksum"):
            conflicts.append({
                "type": "package_checksum_changed",
                "severity": "high",
                "message": ("Package content has changed since last reconcile "
                           "(different state_checksum). Re-export from source recommended."),
                "resolution_options": ["skip", "reimport", "override"],
            })
        if existing_package_info.get("exported_at") != package.get("exported_at"):
            conflicts.append({
                "type": "package_re_exported",
                "severity": "high",
                "message": ("Package has been re-exported at a different time. "
                           f"Old: {existing_package_info.get('exported_at')} "
                           f"New: {package.get('exported_at')}"),
                "resolution_options": ["skip", "reimport", "override"],
            })

    if local_state is not None:
        local_dv = local_state.get("draft_version", 0)
        pkg_dv = package_state.get("draft_version", 0)
        if local_dv != pkg_dv:
            conflicts.append({
                "type": "draft_version_mismatch",
                "severity": "medium",
                "message": (f"Draft version mismatch. "
                           f"Local: v{local_dv}, Package: v{pkg_dv}"),
                "resolution_options": ["skip", "override"],
            })

        local_approved = local_state.get("approved", False)
        pkg_approved = package_state.get("approved", False)
        if local_approved != pkg_approved:
            conflicts.append({
                "type": "approval_status_mismatch",
                "severity": "high",
                "message": (f"Approval status mismatch. "
                           f"Local: {local_approved}, Package: {pkg_approved}"),
                "resolution_options": ["skip", "override"],
            })

        local_conf = local_state.get("confirmations", {})
        pkg_conf = package_state.get("confirmations", {})
        for sec in set(list(local_conf.keys()) + list(pkg_conf.keys())):
            if local_conf.get(sec, False) != pkg_conf.get(sec, False):
                conflicts.append({
                    "type": "section_confirmation_diff",
                    "severity": "medium",
                    "message": (f"Section '{sec}' confirmation differs. "
                               f"Local: {local_conf.get(sec, False)}, "
                               f"Package: {pkg_conf.get(sec, False)}"),
                    "resolution_options": ["skip", "override"],
                })

        local_ver = local_state.get("version")
        pkg_ver = package_state.get("version")
        if local_ver != pkg_ver:
            conflicts.append({
                "type": "version_mismatch",
                "severity": "high",
                "message": (f"Release version mismatch. "
                           f"Local: {local_ver}, Package: {pkg_ver}"),
                "resolution_options": ["skip", "override"],
            })

        local_items = {it.get("id"): it for it in local_state.get("items", [])}
        pkg_items = {it.get("id"): it for it in package_state.get("items", [])}
        diverged_items = []
        for iid in set(local_items.keys()) & set(pkg_items.keys()):
            lit = local_items[iid]
            pit = pkg_items[iid]
            if (lit.get("_version", 1) != pit.get("_version", 1) or
                lit.get("_last_modified_at") != pit.get("_last_modified_at")):
                diverged_items.append(iid)
        if diverged_items:
            conflicts.append({
                "type": "item_divergence",
                "severity": "high",
                "message": (f"Some items have diverged between local and package: "
                           f"{', '.join(diverged_items[:5])}"
                           f"{' ...' if len(diverged_items) > 5 else ''}"),
                "resolution_options": ["skip", "override"],
            })

    rules_diff = _compare_rules_diff(rules_path, package.get("rules_snapshot"))
    if rules_diff.get("has_rules_snapshot") and not rules_diff.get("identical", True):
        if rules_diff.get("local_rules_missing"):
            conflicts.append({
                "type": "local_rules_missing",
                "severity": "low",
                "message": ("Local rules file missing. Package contains a rules snapshot."),
                "resolution_options": ["skip", "override"],
            })
        else:
            conflicts.append({
                "type": "rules_differs",
                "severity": "medium",
                "message": ("Local rules.yaml differs from package rules snapshot."),
                "resolution_options": ["skip", "override"],
            })

    return conflicts


def _build_package(state, operator, rules_path, description):
    rules_copy = None
    if rules_path and os.path.exists(rules_path):
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_copy = f.read()

    handover_summary = _build_handover_summary(state)

    package = {
        "package_format_version": PACKAGE_FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "exported_by": operator or _get_identity(),
        "description": description or "",
        "state": copy.deepcopy(state),
        "state_checksum": _compute_state_checksum(state),
        "rules_snapshot": rules_copy,
        "handover_summary": handover_summary,
        "metadata": {
            "version": state.get("version"),
            "draft_version": state.get("draft_version"),
            "approved": state.get("approved"),
            "items_count": len(state.get("items", [])),
            "batches": state.get("imported_batches", []),
            "drafts_count": len(state.get("drafts", [])),
            "audit_log_count": len(state.get("audit_log", [])),
            "pending_bulk_ops_count": len(state.get("pending_bulk_ops", [])),
            "unresolved_bulk_ops": len([p for p in state.get("pending_bulk_ops", []) if not p.get("resolved")]),
            "rules_version": state.get("rules_version"),
            "rules_upgrade_history_count": len(state.get("rules_upgrade_history", [])),
            "rules_upgrade_revoked_count": len([r for r in state.get("rules_upgrade_history", []) if r.get("revoked")]),
            "has_pending_rules_upgrade": state.get("pending_rules_upgrade") is not None,
        }
    }
    return package


def _validate_package(package):
    if not isinstance(package, dict):
        return False, "Package is not a valid JSON object"
    if "package_format_version" not in package:
        return False, "Missing package_format_version"
    if package["package_format_version"] != PACKAGE_FORMAT_VERSION:
        return False, (f"Incompatible package format version: {package['package_format_version']} "
                      f"(expected {PACKAGE_FORMAT_VERSION})")
    if "state" not in package:
        return False, "Package missing 'state' field"
    if "state_checksum" not in package:
        return False, "Package missing 'state_checksum' field"
    expected = _compute_state_checksum(package["state"])
    if expected != package["state_checksum"]:
        return False, f"State checksum mismatch: package may be corrupted"
    return True, "Package valid"


def _compute_deep_diff(target_state, package_state):
    result = {
        "items": {
            "added": [],
            "removed": [],
            "modified": [],
        },
        "migration_reminders": {
            "added": [],
            "removed": [],
            "modified": [],
        },
        "known_issues": {
            "added": [],
            "removed": [],
            "modified": [],
        },
        "metadata": {
            "version_changed": False,
            "version_old": None,
            "version_new": None,
            "draft_version_changed": False,
            "draft_version_old": None,
            "draft_version_new": None,
            "approved_changed": False,
            "approved_old": None,
            "approved_new": None,
            "confirmations_changed": [],
        },
        "field_changes": {},
    }

    t_version = target_state.get("version")
    p_version = package_state.get("version")
    if t_version != p_version:
        result["metadata"]["version_changed"] = True
        result["metadata"]["version_old"] = t_version
        result["metadata"]["version_new"] = p_version

    t_dv = target_state.get("draft_version", 0)
    p_dv = package_state.get("draft_version", 0)
    if t_dv != p_dv:
        result["metadata"]["draft_version_changed"] = True
        result["metadata"]["draft_version_old"] = t_dv
        result["metadata"]["draft_version_new"] = p_dv

    t_approved = target_state.get("approved", False)
    p_approved = package_state.get("approved", False)
    if t_approved != p_approved:
        result["metadata"]["approved_changed"] = True
        result["metadata"]["approved_old"] = t_approved
        result["metadata"]["approved_new"] = p_approved

    t_conf = target_state.get("confirmations", {})
    p_conf = package_state.get("confirmations", {})
    all_conf_sections = set(list(t_conf.keys()) + list(p_conf.keys()))
    for sec in all_conf_sections:
        if t_conf.get(sec) != p_conf.get(sec):
            result["metadata"]["confirmations_changed"].append({
                "section": sec,
                "old": t_conf.get(sec),
                "new": p_conf.get(sec),
            })

    def _diff_list(target_list, package_list, id_field, category_name):
        t_ids = {it.get(id_field) for it in target_list}
        p_ids = {it.get(id_field) for it in package_list}
        t_by_id = {it.get(id_field): it for it in target_list}
        p_by_id = {it.get(id_field): it for it in package_list}

        for pid in p_ids - t_ids:
            result[category_name]["added"].append({"id": pid, "item": p_by_id[pid]})

        for tid in t_ids - p_ids:
            result[category_name]["removed"].append({"id": tid, "item": t_by_id[tid]})

        for common_id in t_ids & p_ids:
            t_item = t_by_id[common_id]
            p_item = p_by_id[common_id]
            diffs = _compute_field_diff(t_item, p_item)
            if diffs:
                result[category_name]["modified"].append({
                    "id": common_id,
                    "diffs": diffs,
                    "old": t_item,
                    "new": p_item,
                })
                result["field_changes"][f"{category_name}:{common_id}"] = diffs

    _diff_list(
        target_state.get("items", []),
        package_state.get("items", []),
        "id", "items"
    )
    _diff_list(
        target_state.get("migration_reminders", []),
        package_state.get("migration_reminders", []),
        "id", "migration_reminders"
    )
    _diff_list(
        target_state.get("known_issues", []),
        package_state.get("known_issues", []),
        "id", "known_issues"
    )

    return result


def _compare_rules_diff(local_rules_path, package_rules_snapshot):
    if not package_rules_snapshot:
        return {"has_rules_snapshot": False, "diff": None}

    local_rules = None
    if local_rules_path and os.path.exists(local_rules_path):
        with open(local_rules_path, "r", encoding="utf-8") as f:
            local_rules = f.read()

    if local_rules is None:
        return {
            "has_rules_snapshot": True,
            "local_rules_missing": True,
            "diff": None,
        }

    local_lines = local_rules.splitlines()
    package_lines = package_rules_snapshot.splitlines()

    added = []
    removed = []
    for i, line in enumerate(package_lines):
        if line.strip() and line not in local_lines:
            added.append((i + 1, line))
    for i, line in enumerate(local_lines):
        if line.strip() and line not in package_lines:
            removed.append((i + 1, line))

    return {
        "has_rules_snapshot": True,
        "local_rules_missing": False,
        "added": added,
        "removed": removed,
        "identical": len(added) == 0 and len(removed) == 0,
    }


def _compute_rules_checksum(rules):
    serialized = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _compare_rules_configs(old_rules, new_rules):
    result = {
        "required_sections": {
            "added": [],
            "removed": [],
            "unchanged": [],
        },
        "valid_risk_levels": {
            "added": [],
            "removed": [],
            "unchanged": [],
        },
        "required_fields_per_item": {
            "added": [],
            "removed": [],
            "unchanged": [],
        },
        "categories": {
            "added": [],
            "removed": [],
            "unchanged": [],
        },
        "has_changes": False,
    }

    old_sections = set(old_rules.get("required_sections", []))
    new_sections = set(new_rules.get("required_sections", []))
    result["required_sections"]["added"] = sorted(new_sections - old_sections)
    result["required_sections"]["removed"] = sorted(old_sections - new_sections)
    result["required_sections"]["unchanged"] = sorted(old_sections & new_sections)

    old_risks = set(old_rules.get("valid_risk_levels", []))
    new_risks = set(new_rules.get("valid_risk_levels", []))
    result["valid_risk_levels"]["added"] = sorted(new_risks - old_risks)
    result["valid_risk_levels"]["removed"] = sorted(old_risks - new_risks)
    result["valid_risk_levels"]["unchanged"] = sorted(old_risks & new_risks)

    old_fields = set(old_rules.get("required_fields_per_item", []))
    new_fields = set(new_rules.get("required_fields_per_item", []))
    result["required_fields_per_item"]["added"] = sorted(new_fields - old_fields)
    result["required_fields_per_item"]["removed"] = sorted(old_fields - new_fields)
    result["required_fields_per_item"]["unchanged"] = sorted(old_fields & new_fields)

    old_cats = set(old_rules.get("categories", []))
    new_cats = set(new_rules.get("categories", []))
    result["categories"]["added"] = sorted(new_cats - old_cats)
    result["categories"]["removed"] = sorted(old_cats - new_cats)
    result["categories"]["unchanged"] = sorted(old_cats & new_cats)

    for key in result:
        if key == "has_changes":
            continue
        if result[key]["added"] or result[key]["removed"]:
            result["has_changes"] = True
            break

    return result


def _assess_rules_upgrade_impact(state, old_rules, new_rules, rules_diff):
    impact = {
        "auto_migratable": [],
        "confirmation_rollback_needed": [],
        "manual_decision_required": [],
        "summary": {
            "total_affected_items": 0,
            "auto_migratable_count": 0,
            "confirmation_rollback_count": 0,
            "manual_decision_count": 0,
        },
    }

    items = state.get("items", [])
    confirmations = state.get("confirmations", {})

    added_sections = rules_diff["required_sections"]["added"]
    removed_sections = rules_diff["required_sections"]["removed"]
    for sec in added_sections:
        if confirmations.get(sec, False):
            impact["confirmation_rollback_needed"].append({
                "type": "section_added",
                "section": sec,
                "message": f"New required section '{sec}' added - needs confirmation",
                "auto_migratable": True,
                "rollback_action": "unconfirm",
            })
        else:
            impact["auto_migratable"].append({
                "type": "section_added_pending",
                "section": sec,
                "message": f"New required section '{sec}' added (already pending)",
                "auto_migratable": True,
            })
    for sec in removed_sections:
        if confirmations.get(sec, False):
            impact["auto_migratable"].append({
                "type": "section_removed_confirmed",
                "section": sec,
                "message": f"Section '{sec}' removed from required - confirmation will be dropped",
                "auto_migratable": True,
            })
        else:
            impact["auto_migratable"].append({
                "type": "section_removed_pending",
                "section": sec,
                "message": f"Section '{sec}' removed from required list",
                "auto_migratable": True,
            })

    removed_risks = set(rules_diff["valid_risk_levels"]["removed"])
    if removed_risks:
        for item in items:
            rl = item.get("risk_level", "")
            if rl in removed_risks:
                impact["manual_decision_required"].append({
                    "type": "invalid_risk_level",
                    "item_id": item.get("id"),
                    "field": "risk_level",
                    "old_value": rl,
                    "valid_values": rules_diff["valid_risk_levels"]["unchanged"] + rules_diff["valid_risk_levels"]["added"],
                    "message": f"Item {item.get('id')}: risk_level '{rl}' is no longer valid",
                    "auto_migratable": False,
                })

    added_required_fields = set(rules_diff["required_fields_per_item"]["added"])
    if added_required_fields:
        for item in items:
            for field in added_required_fields:
                val = item.get(field, "")
                if not val or not str(val).strip():
                    impact["manual_decision_required"].append({
                        "type": "missing_required_field",
                        "item_id": item.get("id"),
                        "field": field,
                        "message": f"Item {item.get('id')}: missing newly required field '{field}'",
                        "auto_migratable": False,
                    })

    removed_cats = set(rules_diff["categories"]["removed"])
    if removed_cats:
        for item in items:
            cat = item.get("category", "")
            if cat in removed_cats:
                impact["manual_decision_required"].append({
                    "type": "invalid_category",
                    "item_id": item.get("id"),
                    "field": "category",
                    "old_value": cat,
                    "valid_values": rules_diff["categories"]["unchanged"] + rules_diff["categories"]["added"],
                    "message": f"Item {item.get('id')}: category '{cat}' is no longer valid",
                    "auto_migratable": False,
                })

    mig_items = {m.get("id") for m in impact["auto_migratable"] if m.get("item_id")}
    conf_items = {m.get("section") for m in impact["confirmation_rollback_needed"]}
    man_items = {m.get("item_id") for m in impact["manual_decision_required"] if m.get("item_id")}

    impact["summary"]["auto_migratable_count"] = len(impact["auto_migratable"])
    impact["summary"]["confirmation_rollback_count"] = len(impact["confirmation_rollback_needed"])
    impact["summary"]["manual_decision_count"] = len(impact["manual_decision_required"])
    impact["summary"]["total_affected_items"] = len(mig_items | conf_items | man_items)

    return impact


def _ensure_rules_snapshot(state, rules_path):
    if state.get("rules_snapshot") is None and rules_path and os.path.exists(rules_path):
        rules = load_rules(rules_path)
        state["rules_snapshot"] = copy.deepcopy(rules)
        state["rules_version"] = _compute_rules_checksum(rules)


def _apply_rules_upgrade_to_state(state, old_rules, new_rules, impact, decisions, operator):
    confirmations = state.get("confirmations", {})

    added_sections = set(m["section"] for m in impact["auto_migratable"]
                         if m["type"] in ("section_added", "section_added_pending"))
    for sec in added_sections:
        confirmations[sec] = False

    for item in impact["confirmation_rollback_needed"]:
        if item["type"] == "section_added":
            sec = item["section"]
            dec_key = f"section_added:{sec}"
            dec = decisions.get(dec_key, "unconfirm")
            if dec == "unconfirm":
                confirmations[sec] = False

    removed_sections = set()
    for m in impact["auto_migratable"]:
        if m["type"] in ("section_removed_confirmed", "section_removed_pending"):
            removed_sections.add(m["section"])
    for sec in removed_sections:
        if sec in confirmations:
            del confirmations[sec]

    for m in impact["manual_decision_required"]:
        iid = m.get("item_id")
        field = m.get("field")
        dec_key = f"{m['type']}:{iid}:{field}"
        dec = decisions.get(dec_key)
        if dec and dec.get("action") == "set" and iid and field:
            target = _find_in_list(state.get("items", []), iid)
            if target:
                target[field] = dec["value"]
                _bump_item_version(target, operator=operator)

    state["rules_snapshot"] = copy.deepcopy(new_rules)
    state["rules_version"] = _compute_rules_checksum(new_rules)

    return state


def _suggest_import_mode(age_info, deep_diff, target_state):
    suggestions = []

    if target_state is None:
        return {
            "recommended_mode": "takeover",
            "reason": "Target state does not exist - takeover will create new state from package",
            "alternative_modes": ["merge"],
            "risk_level": "low",
        }

    risk_level = "low"
    reasons = []

    if age_info["target_newer"]:
        risk_level = "high"
        reasons.append(f"Target state is NEWER (score {age_info['target_score']} vs {age_info['package_score']})")
        reasons.append("  - Target may have work that will be overwritten by takeover")
        reasons.append("  - Use --force with takeover to override, or use merge mode")

    if age_info["target_approved"]:
        risk_level = "high"
        reasons.append("Target state is already APPROVED")
        reasons.append("  - Import will clear approved status")
        reasons.append("  - Use --force to override")

    items_modified = len(deep_diff["items"]["modified"])
    items_removed = len(deep_diff["items"]["removed"])
    items_added = len(deep_diff["items"]["added"])
    conf_changed = len(deep_diff["metadata"]["confirmations_changed"])

    if items_modified > 0:
        risk_level = "medium" if risk_level == "low" else risk_level
        reasons.append(f"Package will MODIFY {items_modified} existing items")

    if items_removed > 0:
        risk_level = "high"
        reasons.append(f"Package will REMOVE {items_removed} items present in target")

    if items_added > 0:
        reasons.append(f"Package will ADD {items_added} new items")

    if conf_changed > 0:
        reasons.append(f"Package will CHANGE {conf_changed} confirmation statuses")

    total_items = len(target_state.get("items", [])) if target_state else 0
    if items_removed > 0 or (total_items > 0 and items_modified > total_items * 0.5):
        recommended_mode = "takeover"
        reasons.append("Recommended mode: takeover (significant structural changes)")
    elif age_info["target_newer"]:
        recommended_mode = "merge"
        reasons.append("Recommended mode: merge (preserve target history while applying package content)")
    else:
        recommended_mode = "takeover"
        reasons.append("Recommended mode: takeover (clean replacement)")

    return {
        "recommended_mode": recommended_mode,
        "alternative_modes": ["merge"] if recommended_mode == "takeover" else ["takeover"],
        "risk_level": risk_level,
        "reasons": reasons,
        "force_required": age_info["target_newer"] or age_info["target_approved"],
    }


def cmd_preflight_check(args, rules):
    package_path = args.package
    state_path = args.state

    if not os.path.exists(package_path):
        print(f"[ERROR] Package file not found: {package_path}")
        sys.exit(1)

    with open(package_path, "r", encoding="utf-8") as f:
        package = json.load(f)

    valid, msg = _validate_package(package)
    if not valid:
        print(f"[REJECTED] Package validation failed: {msg}")
        sys.exit(1)

    package_state = package["state"]
    target_state = load_state(state_path)

    print("\n" + "=" * 70)
    print("PACKAGE PREFLIGHT CHECK")
    print("=" * 70)

    print(f"\n[Package Information]")
    print(f"  Format version:  {package['package_format_version']}")
    print(f"  Exported at:     {package.get('exported_at')}")
    print(f"  Exported by:     {package.get('exported_by')}")
    if package.get('description'):
        print(f"  Description:     {package.get('description')}")
    print(f"  Package state:   v{package_state.get('version')} / draft v{package_state.get('draft_version')}")
    print(f"  Items:           {len(package_state.get('items', []))}")
    print(f"  Approved:        {package_state.get('approved', False)}")

    print(f"\n[Target State]")
    if target_state is None:
        print(f"  Status:          NOT INITIALIZED (will create new state)")
    else:
        print(f"  Version:         v{target_state.get('version')}")
        print(f"  Draft:           v{target_state.get('draft_version', 0)}")
        print(f"  Approved:        {target_state.get('approved', False)}")
        print(f"  Items:           {len(target_state.get('items', []))}")

    print(f"\n[Validation]")
    print(f"  Checksum:        OK")
    print(f"  Format:          OK")

    age_info = _compare_state_age(target_state or package_state, package_state)
    if target_state is not None:
        print(f"\n[State Age Comparison]")
        print(f"  Target score:    {age_info['target_score']}")
        print(f"  Package score:   {age_info['package_score']}")
        print(f"  Target newer:    {age_info['target_newer']}")
        if age_info['target_last_modified']:
            print(f"  Target modified: {age_info['target_last_modified']}")
        if age_info['package_last_modified']:
            print(f"  Package modified:{age_info['package_last_modified']}")

    rules_diff = _compare_rules_diff(args.rules, package.get("rules_snapshot"))
    print(f"\n[Rules Snapshot Diff]")
    if rules_diff.get("has_rules_snapshot"):
        if rules_diff.get("local_rules_missing"):
            print(f"  Status:          Package has rules snapshot, but local rules file missing")
            print(f"  Recommend:       Use --apply-rules-snapshot during import_package to restore rules")
        elif rules_diff.get("identical"):
            print(f"  Status:          IDENTICAL to local rules")
        else:
            print(f"  Status:          DIFFERS from local rules")
            added = rules_diff.get("added", [])
            removed = rules_diff.get("removed", [])
            if added:
                print(f"  Lines in package but not in local: {len(added)}")
                for ln, line in added[:5]:
                    print(f"    + L{ln}: {line.strip()}")
                if len(added) > 5:
                    print(f"    ... and {len(added) - 5} more")
            if removed:
                print(f"  Lines in local but not in package: {len(removed)}")
                for ln, line in removed[:5]:
                    print(f"    - L{ln}: {line.strip()}")
                if len(removed) > 5:
                    print(f"    ... and {len(removed) - 5} more")
            print(f"  Recommend:       Review rules diff. Use --apply-rules-snapshot to use package rules.")
    else:
        print(f"  Status:          No rules snapshot in package (exported with --no-rules)")

    deep_diff = _compute_deep_diff(target_state or package_state, package_state) if target_state is not None else None
    if target_state is not None and deep_diff:
        print(f"\n[Content Diff Summary]")
        items_added = len(deep_diff["items"]["added"])
        items_removed = len(deep_diff["items"]["removed"])
        items_modified = len(deep_diff["items"]["modified"])
        print(f"  Items:           +{items_added} -{items_removed} ~{items_modified}")

        mig_added = len(deep_diff["migration_reminders"]["added"])
        mig_removed = len(deep_diff["migration_reminders"]["removed"])
        mig_modified = len(deep_diff["migration_reminders"]["modified"])
        print(f"  Migrations:      +{mig_added} -{mig_removed} ~{mig_modified}")

        ki_added = len(deep_diff["known_issues"]["added"])
        ki_removed = len(deep_diff["known_issues"]["removed"])
        ki_modified = len(deep_diff["known_issues"]["modified"])
        print(f"  Known issues:    +{ki_added} -{ki_removed} ~{ki_modified}")

        if deep_diff["metadata"]["version_changed"]:
            print(f"  Version:         {deep_diff['metadata']['version_old']} -> {deep_diff['metadata']['version_new']}")
        if deep_diff["metadata"]["draft_version_changed"]:
            print(f"  Draft version:   v{deep_diff['metadata']['draft_version_old']} -> v{deep_diff['metadata']['draft_version_new']}")
        if deep_diff["metadata"]["approved_changed"]:
            print(f"  Approved:        {deep_diff['metadata']['approved_old']} -> {deep_diff['metadata']['approved_new']}")

        print(f"\n[Items to be Modified]")
        if items_modified > 0:
            for item in deep_diff["items"]["modified"][:10]:
                print(f"  - {item['id']}:")
                for d in item["diffs"]:
                    print(f"      {d['field']}: {d['old']!r} -> {d['new']!r}")
            if items_modified > 10:
                print(f"  ... and {items_modified - 10} more modified items")
        else:
            print(f"  (none)")

        print(f"\n[Confirmation Status Changes]")
        conf_changes = deep_diff["metadata"]["confirmations_changed"]
        if conf_changes:
            for c in conf_changes:
                status_old = "CONFIRMED" if c["old"] else "PENDING"
                status_new = "CONFIRMED" if c["new"] else "PENDING"
                arrow = ">>>" if c["new"] else "<<<"
                print(f"  {arrow} {c['section']}: {status_old} -> {status_new}")
        else:
            print(f"  (no changes)")

        print(f"\n[Conflict Risk Analysis]")
        mode_suggestion = _suggest_import_mode(age_info, deep_diff, target_state)
        risk_color = {
            "low": "LOW",
            "medium": "MEDIUM",
            "high": "HIGH",
        }.get(mode_suggestion["risk_level"], mode_suggestion["risk_level"])
        print(f"  Risk level:      {risk_color}")
        for reason in mode_suggestion["reasons"]:
            print(f"  {reason}")
        print(f"\n  Recommended mode: {mode_suggestion['recommended_mode']}")
        print(f"  Alternatives:     {', '.join(mode_suggestion['alternative_modes'])}")
        if mode_suggestion["force_required"]:
            print(f"  Force required:   YES (add --force)")
        else:
            print(f"  Force required:   NO")
    else:
        print(f"\n[Conflict Risk Analysis]")
        print(f"  Risk level:      LOW")
        print(f"  Recommended mode: takeover (clean initialization)")
        print(f"  Alternatives:     merge")
        print(f"  Force required:   NO")

    print(f"\n[Import Commands]")
    if target_state is None:
        print(f"  # Fresh import (create new state):")
        print(f"  release_cli.py --state {state_path} import_package {package_path} --mode takeover --operator <your-name>")
    else:
        print(f"  # Recommended import:")
        mode = mode_suggestion.get("recommended_mode", "takeover")
        force_flag = " --force" if mode_suggestion.get("force_required") else ""
        print(f"  release_cli.py --state {state_path} import_package {package_path} --mode {mode}{force_flag} --operator <your-name>")
        print(f"\n  # Alternative modes:")
        for alt in mode_suggestion.get("alternative_modes", []):
            print(f"  release_cli.py --state {state_path} import_package {package_path} --mode {alt}{force_flag} --operator <your-name>")

    print("\n" + "=" * 70)
    print("PREFLIGHT COMPLETE - No state changes made")
    print("=" * 70)
    print()


def _compare_state_age(target_state, package_state):
    target_dv = target_state.get("draft_version", 0)
    package_dv = package_state.get("draft_version", 0)
    target_audit = len(target_state.get("audit_log", []))
    package_audit = len(package_state.get("audit_log", []))
    target_approved = target_state.get("approved", False)
    package_approved = package_state.get("approved", False)

    target_modified = None
    if target_state.get("audit_log"):
        target_modified = target_state["audit_log"][-1].get("timestamp")
    package_modified = None
    if package_state.get("audit_log"):
        package_modified = package_state["audit_log"][-1].get("timestamp")

    target_score = (1 if target_approved else 0) * 1000 + target_dv * 10 + target_audit
    package_score = (1 if package_approved else 0) * 1000 + package_dv * 10 + package_audit

    return {
        "target_newer": target_score > package_score,
        "target_score": target_score,
        "package_score": package_score,
        "target_draft_version": target_dv,
        "package_draft_version": package_dv,
        "target_audit_len": target_audit,
        "package_audit_len": package_audit,
        "target_approved": target_approved,
        "package_approved": package_approved,
        "target_last_modified": target_modified,
        "package_last_modified": package_modified,
    }


def cmd_export_package(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    operator = args.operator or _get_identity()
    description = args.description or ""
    include_rules = not args.no_rules

    package = _build_package(state, operator, args.rules if include_rules else None, description)

    out_path = args.output
    if not out_path:
        v = state.get("version", "UNKNOWN")
        dv = state.get("draft_version", 0)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(SCRIPT_DIR, f"release_pkg_v{v}_d{dv}_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(package, f, ensure_ascii=False, indent=2)

    _audit(state, "export_package",
           f"operator={operator} output={out_path} "
           f"draft_v={state.get('draft_version')} items={len(state.get('items', []))} "
           f"description={description}")
    save_state(state, state_path)

    print(f"[OK] Package exported -> {out_path}")
    print(f"   Format version: {PACKAGE_FORMAT_VERSION}")
    print(f"   Exported by:    {operator}")
    print(f"   State:          v{state.get('version')} / draft v{state.get('draft_version')}")
    print(f"   Items:          {len(state.get('items', []))}")
    print(f"   Audit entries:  {len(state.get('audit_log', []))}")
    if include_rules and package.get("rules_snapshot"):
        print(f"   Rules snapshot: included")
    else:
        print(f"   Rules snapshot: excluded")

    summary = package.get("handover_summary", {})
    print()
    print(f"== Handover Summary (for receiver) ==")
    print(f"   Approval status:    {summary.get('approval_status')}")
    print(f"   Draft version:      v{summary.get('draft_version')}")
    print(f"   Sections confirmed: {summary.get('sections_confirmed')}")
    print(f"   Sections pending:   {summary.get('sections_pending')}")
    print(f"   Items total:        {summary.get('items_total')}")
    if summary.get('items_with_missing_owner'):
        print(f"   Items missing owner: {summary.get('items_with_missing_owner')}")
    if summary.get('items_with_invalid_risk'):
        print(f"   Items invalid risk:  {summary.get('items_with_invalid_risk')}")
    print(f"   Migrations:         {summary.get('migration_total')} total, "
          f"{len(summary.get('migrations_pending', []))} pending")
    print(f"   Known issues:       {summary.get('known_issues_total')} "
          f"(reviewed={summary.get('known_issues_reviewed')})")
    if summary.get('unresolved_bulk_ops'):
        print(f"   Unresolved bulk:    {summary.get('unresolved_bulk_ops')} "
              f"(indices: {summary.get('unresolved_bulk_ops')})")
    next_steps = summary.get('suggested_next_steps', [])
    if next_steps:
        print(f"   Suggested next steps:")
        for s in next_steps:
            print(f"     - {s}")
    print()
    print(f"   Receiver: run 'takeover_detail' after import_package to review this summary.")
    print(f"   Receiver: run 'takeover_confirm' to officially take over and persist session.")


def cmd_import_package(args, rules):
    state_path = args.state
    package_path = args.package

    if not os.path.exists(package_path):
        print(f"[ERROR] Package file not found: {package_path}")
        sys.exit(1)

    with open(package_path, "r", encoding="utf-8") as f:
        package = json.load(f)

    valid, msg = _validate_package(package)
    if not valid:
        print(f"[REJECTED] Package validation failed: {msg}")
        sys.exit(1)

    package_state = package["state"]
    target_state = load_state(state_path)
    operator = args.operator or _get_identity()
    mode = args.mode or "merge"

    target_state_snapshot = copy.deepcopy(target_state) if target_state else None

    print(f"\n{'=' * 70}")
    print(f"PACKAGE IMPORT - READ-ONLY RECONCILE (NOT YET CONFIRMED)")
    print(f"{'=' * 70}")
    print(f"   Package file:    {package_path}")
    print(f"   Package format:  {package['package_format_version']}")
    print(f"   Exported at:     {package.get('exported_at')}")
    print(f"   Exported by:     {package.get('exported_by')}")
    print(f"   Package state:   v{package_state.get('version')} / draft v{package_state.get('draft_version')}")
    print(f"   Import mode:     {mode}")
    print(f"   Imported by:     {operator}")
    print()

    age_info = None
    if target_state is not None:
        age_info = _compare_state_age(target_state, package_state)
        print(f"[Target State Comparison]")
        print(f"  Target state:      v{target_state.get('version')} / draft v{age_info['target_draft_version']}")
        print(f"  Package state:     v{package_state.get('version')} / draft v{age_info['package_draft_version']}")
        print(f"  Target audit len:  {age_info['target_audit_len']}")
        print(f"  Package audit len: {age_info['package_audit_len']}")
        print(f"  Target approved:   {age_info['target_approved']}")
        print(f"  Package approved:  {age_info['package_approved']}")
        print()

        if target_state.get("approved") and not args.force:
            print("[BLOCKED] Target state is already approved.")
            print("   Use --force to override during confirm step.")

        if age_info["target_newer"] and not args.force:
            print("[BLOCKED] Target state is NEWER than package state.")
            print("   Importing would overwrite newer changes.")
            print("   Use --force during confirm step to override, or consider:")
            print("     --mode merge    Continue from package state, merging history")
            print("     --mode takeover Completely replace target state with package")
            print()

    summary = package.get("handover_summary", {})
    if summary:
        print(f"[Handover Summary from Source]")
        print(f"  Approval status:    {summary.get('approval_status')}")
        print(f"  Draft version:      v{summary.get('draft_version')}")
        print(f"  Sections confirmed: {summary.get('sections_confirmed')}")
        print(f"  Sections pending:   {summary.get('sections_pending')}")
        print(f"  Items total:        {summary.get('items_total')}")
        if summary.get('items_with_missing_owner'):
            print(f"  Items missing owner: {summary.get('items_with_missing_owner')}")
        if summary.get('items_with_invalid_risk'):
            print(f"  Items invalid risk:  {summary.get('items_with_invalid_risk')}")
        print(f"  Migrations:         {summary.get('migration_total')} total, "
              f"{len(summary.get('migrations_pending', []))} pending")
        print(f"  Known issues:       {summary.get('known_issues_total')} "
              f"(reviewed={summary.get('known_issues_reviewed')})")
        if summary.get('unresolved_bulk_ops'):
            print(f"  Unresolved bulk:    indices {summary.get('unresolved_bulk_ops')}")
        next_steps = summary.get('suggested_next_steps', [])
        if next_steps:
            print(f"  Suggested next steps (from source):")
            for s in next_steps:
                print(f"    - {s}")
        print()

    existing_pkg_info = None
    if target_state and target_state.get("pending_takeover"):
        existing_pkg_info = {
            "package_checksum": target_state["pending_takeover"].get("package_checksum"),
            "exported_at": target_state["pending_takeover"].get("exported_at"),
        }
    conflicts = _detect_takeover_conflicts(
        target_state, package, args.rules, existing_pkg_info
    )

    if conflicts:
        print(f"[CONFLICTS DETECTED] ({len(conflicts)})")
        for i, c in enumerate(conflicts, 1):
            sev_tag = {
                "high": "[HIGH]",
                "medium": "[MED]",
                "low": "[LOW]",
            }.get(c.get("severity", "medium"), "[MED]")
            print(f"  {i}. {sev_tag} {c['type']}: {c['message']}")
            opts = c.get("resolution_options", [])
            if opts:
                print(f"     Resolution options: {', '.join(opts)}")
        print()

    pre_approved_blocked = (
        target_state is not None
        and target_state.get("approved")
        and not args.force
    )
    pre_target_newer_blocked = (
        age_info is not None
        and age_info["target_newer"]
        and not args.force
    )

    deep_diff = None
    if mode == "takeover":
        if target_state is not None:
            deep_diff = _compute_deep_diff(target_state_snapshot, package_state)
        else:
            deep_diff = None

        if target_state is not None:
            modified_items = len(deep_diff["items"]["modified"]) if deep_diff else 0
            added_items = len(deep_diff["items"]["added"]) if deep_diff else 0
            removed_items = len(deep_diff["items"]["removed"]) if deep_diff else 0
            print(f"[Takeover Diff Preview]")
            print(f"  Items: +{added_items} -{removed_items} ~{modified_items}")
            if deep_diff and deep_diff["metadata"]["version_changed"]:
                print(f"  Version: {deep_diff['metadata']['version_old']} -> {deep_diff['metadata']['version_new']}")
            if deep_diff and deep_diff["metadata"]["draft_version_changed"]:
                print(f"  Draft: v{deep_diff['metadata']['draft_version_old']} -> v{deep_diff['metadata']['draft_version_new']}")
            if deep_diff and deep_diff["metadata"]["approved_changed"]:
                print(f"  Approved: {deep_diff['metadata']['approved_old']} -> {deep_diff['metadata']['approved_new']}")
            print()
            pending_final_state = package_state
        else:
            print(f"[Takeover Diff Preview]")
            print(f"  Fresh import (no existing target state)")
            print()
            pending_final_state = package_state
    else:
        if target_state is None:
            pending_final_state = package_state
        else:
            merged = copy.deepcopy(package_state)
            merged["audit_log"] = target_state.get("audit_log", []) + package_state.get("audit_log", [])
            if args.keep_target_batches:
                target_batches = set(target_state.get("imported_batches", []))
                package_batches = package_state.get("imported_batches", [])
                merged["imported_batches"] = list(target_batches) + [
                    b for b in package_batches if b not in target_batches
                ]
            pending_final_state = merged
        print(f"[Merge Mode Preview]")
        print(f"  Target history + package history merged.")
        print()

    rules_diff_info = _compare_rules_diff(args.rules, package.get("rules_snapshot"))
    if rules_diff_info.get("has_rules_snapshot"):
        if rules_diff_info.get("local_rules_missing"):
            print(f"[Rules] Package has rules snapshot; local rules file missing.")
        elif rules_diff_info.get("identical"):
            print(f"[Rules] Package rules snapshot IDENTICAL to local rules.")
        else:
            add_cnt = len(rules_diff_info.get("added", []))
            rem_cnt = len(rules_diff_info.get("removed", []))
            print(f"[Rules] Package rules DIFFERS from local: +{add_cnt}/-{rem_cnt} lines.")
        if args.apply_rules_snapshot:
            print(f"        --apply-rules-snapshot set: will restore rules during confirm.")
        print()

    pending_takeover_id = hashlib.sha256(
        f"{datetime.now().isoformat()}{operator}{package_path}".encode()
    ).hexdigest()[:16]

    state_for_pending = copy.deepcopy(target_state) if target_state else _new_state(
        package_state.get("version", "UNKNOWN"),
        package_state.get("current_batch_id", "pending-batch"),
    )
    state_for_pending.setdefault("takeover_history", [])
    state_for_pending.setdefault("confirmed_takeover_sessions", {})
    state_for_pending["pending_takeover"] = {
        "takeover_id": pending_takeover_id,
        "imported_at": datetime.now().isoformat(),
        "imported_by": operator,
        "import_pid": os.getpid(),
        "exported_by": package.get("exported_by"),
        "exported_at": package.get("exported_at"),
        "package_path": os.path.basename(package_path),
        "package_path_full": package_path,
        "package_checksum": package.get("state_checksum"),
        "package_format_version": package.get("package_format_version"),
        "mode": mode,
        "force": args.force,
        "apply_rules_snapshot": args.apply_rules_snapshot,
        "keep_target_batches": args.keep_target_batches,
        "handover_summary": summary,
        "pre_import_state": target_state_snapshot,
        "post_import_preview_state": copy.deepcopy(pending_final_state),
        "diff": deep_diff,
        "conflicts": conflicts,
        "rules_diff": rules_diff_info,
        "age_info": age_info,
        "blocked_reasons": {
            "target_approved": pre_approved_blocked,
            "target_newer": pre_target_newer_blocked,
        },
        "status": "pending_confirmation",
        "confirmed": False,
        "confirmed_at": None,
        "confirmed_by": None,
        "decisions_log": [],
        "resumed_across_restart": False,
    }

    if package.get("rules_snapshot") and args.apply_rules_snapshot:
        rules_dir = os.path.dirname(args.rules)
        if not os.path.exists(rules_dir):
            os.makedirs(rules_dir, exist_ok=True)
        with open(args.rules, "w", encoding="utf-8") as f:
            f.write(package["rules_snapshot"])
        print(f"[INFO] Rules snapshot restored to {args.rules}")
        _audit(state_for_pending, "rules_restored_preview",
               f"from_package={package_path} operator={operator} pending_takeover={pending_takeover_id}")

    _audit(state_for_pending, "import_package_reconciled",
           f"takeover_id={pending_takeover_id} operator={operator} package={package_path} "
           f"exported_by={package.get('exported_by')} mode={mode} "
           f"conflicts={len(conflicts)} status=pending_confirmation")

    save_state(state_for_pending, state_path)

    print()
    print(f"{'=' * 70}")
    if pre_approved_blocked or pre_target_newer_blocked:
        print(f"[STATUS] RECONCILED - BUT BLOCKED (need --force in confirm step)")
    else:
        print(f"[STATUS] RECONCILED - AWAITING CONFIRMATION")
    print(f"{'=' * 70}")
    print(f"  Takeover ID:      {pending_takeover_id}")
    print(f"  State file written with pending_takeover (read-only, not committed).")
    print()
    print(f"  Next steps:")
    print(f"    1. View details:   release_cli.py --state {state_path} takeover_detail")
    print(f"    2. Confirm takeover: release_cli.py --state {state_path} takeover_confirm")
    force_flag = " --force" if (pre_approved_blocked or pre_target_newer_blocked) else ""
    print(f"       (add{force_flag} to override blocks)")
    print(f"    3. Reconcile again: release_cli.py --state {state_path} import_package {package_path} --mode {mode}")
    print(f"    4. Abort / cancel:  release_cli.py --state {state_path} takeover_revoke")
    print(f"{'=' * 70}")
    print()


def cmd_takeover_detail(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("takeover_history", [])
    state.setdefault("confirmed_takeover_sessions", {})

    takeover_id = getattr(args, "takeover_id", None)
    pending = state.get("pending_takeover")
    target_takeover = None
    is_pending = False

    if pending and (takeover_id is None or pending.get("takeover_id") == takeover_id):
        target_takeover = pending
        is_pending = True
    elif takeover_id:
        for tk in state.get("takeover_history", []):
            if tk.get("takeover_id") == takeover_id:
                target_takeover = tk
                break
        if target_takeover is None:
            sess = state.get("confirmed_takeover_sessions", {}).get(takeover_id)
            if sess:
                target_takeover = sess
    elif state.get("takeover_history"):
        target_takeover = state["takeover_history"][-1]

    if target_takeover is None:
        print("[INFO] No takeover record found (no pending_takeover, no takeover_history).")
        print("  Run 'import_package' first.")
        return

    tid = target_takeover.get("takeover_id", "?")
    print(f"\n{'=' * 70}")
    tag = " [PENDING CONFIRMATION]" if is_pending else ""
    if target_takeover.get("confirmed") or target_takeover.get("confirmed_at"):
        tag = " [CONFIRMED / PERSISTED]"
    print(f"TAKEOVER DETAIL: {tid}{tag}")
    print(f"{'=' * 70}")

    print(f"\n[Basic Info]")
    print(f"  Takeover ID:      {tid}")
    print(f"  Imported at:      {target_takeover.get('imported_at')}")
    print(f"  Imported by:      {target_takeover.get('imported_by')}")
    print(f"  Exported by:      {target_takeover.get('exported_by')}")
    print(f"  Exported at:      {target_takeover.get('exported_at')}")
    print(f"  Package:          {target_takeover.get('package_path')}")
    print(f"  Mode:             {target_takeover.get('mode')}")
    print(f"  Force:            {target_takeover.get('force', False)}")
    print(f"  Package checksum: {target_takeover.get('package_checksum', '(legacy)')}")

    if target_takeover.get("confirmed_at"):
        print(f"\n[Confirmation]")
        print(f"  Confirmed at:     {target_takeover.get('confirmed_at')}")
        print(f"  Confirmed by:     {target_takeover.get('confirmed_by')}")

    pre = target_takeover.get("pre_import_state")
    post = target_takeover.get("post_import_state") or target_takeover.get("post_import_preview_state")
    if pre and post:
        print(f"\n[State Transition]")
        print(f"  Before: v{pre.get('version')} / draft v{pre.get('draft_version', 0)} / approved={pre.get('approved', False)}")
        print(f"  After:  v{post.get('version')} / draft v{post.get('draft_version', 0)} / approved={post.get('approved', False)}")

    blocked = target_takeover.get("blocked_reasons", {})
    if blocked.get("target_approved") or blocked.get("target_newer"):
        print(f"\n[Blocks Requiring --force During Confirm]")
        if blocked.get("target_approved"):
            print(f"  - Target state was already APPROVED at reconcile time")
        if blocked.get("target_newer"):
            print(f"  - Target state was NEWER than package state at reconcile time")

    conflicts = target_takeover.get("conflicts", [])
    if conflicts:
        print(f"\n[Conflicts Detected] ({len(conflicts)})")
        for i, c in enumerate(conflicts, 1):
            sev_tag = {
                "high": "[HIGH]",
                "medium": "[MED]",
                "low": "[LOW]",
            }.get(c.get("severity", "medium"), "[MED]")
            print(f"  {i}. {sev_tag} {c['type']}")
            print(f"     {c['message']}")
            opts = c.get("resolution_options", [])
            if opts:
                print(f"     Options: {', '.join(opts)}")

    decisions = target_takeover.get("decisions_log", [])
    if decisions:
        print(f"\n[Conflict Decisions Applied]")
        for d in decisions:
            print(f"  [{d.get('ts')}] {d.get('conflict_type')}: {d.get('decision')}"
                  f" - {d.get('detail', '')}")

    summary = target_takeover.get("handover_summary")
    if summary:
        print(f"\n[Handover Summary (from source)]")
        print(f"  Approval:         {summary.get('approval_status')}")
        print(f"  Draft version:    v{summary.get('draft_version')}")
        print(f"  Sections done:    {summary.get('sections_confirmed')}")
        print(f"  Sections pending: {summary.get('sections_pending')}")
        missing_owners = summary.get('items_with_missing_owner', [])
        invalid_risks = summary.get('items_with_invalid_risk', [])
        print(f"  Items:            {summary.get('items_total')} total"
              f" ({len(missing_owners)} missing owner, {len(invalid_risks)} invalid risk)")
        mig_pending = summary.get('migrations_pending', [])
        print(f"  Migrations:       {summary.get('migration_total')} total"
              f" ({len(mig_pending)} pending)")
        print(f"  Known issues:     {summary.get('known_issues_total')}"
              f" (reviewed={summary.get('known_issues_reviewed')})")
        bulk_pending = summary.get('unresolved_bulk_ops', [])
        if bulk_pending:
            print(f"  Unresolved bulk:  indices {bulk_pending}")
        next_steps = summary.get('suggested_next_steps', [])
        if next_steps:
            print(f"\n  Suggested next steps:")
            for s in next_steps:
                print(f"    - {s}")

    diff = target_takeover.get("diff")
    if diff and target_takeover.get("mode") == "takeover":
        print(f"\n[Takeover Diff Detail]")
        items_mod = diff["items"]["modified"]
        items_add = diff["items"]["added"]
        items_rem = diff["items"]["removed"]
        if items_add:
            print(f"  + ADDED ({len(items_add)} items):")
            for item in items_add[:5]:
                print(f"    - {item['id']}: {item['item'].get('title', '')}")
            if len(items_add) > 5:
                print(f"    ... and {len(items_add) - 5} more")
        if items_rem:
            print(f"  - REMOVED ({len(items_rem)} items):")
            for item in items_rem[:5]:
                print(f"    - {item['id']}: {item['item'].get('title', '')}")
            if len(items_rem) > 5:
                print(f"    ... and {len(items_rem) - 5} more")
        if items_mod:
            print(f"  ~ MODIFIED ({len(items_mod)} items):")
            for item in items_mod[:10]:
                print(f"    - {item['id']}:")
                for d in item["diffs"]:
                    old_repr = str(d["old"])[:30]
                    new_repr = str(d["new"])[:30]
                    print(f"      {d['field']}: {old_repr!r} -> {new_repr!r}")
            if len(items_mod) > 10:
                print(f"    ... and {len(items_mod) - 10} more modified items")

    rules_diff = target_takeover.get("rules_diff")
    if rules_diff and rules_diff.get("has_rules_snapshot"):
        print(f"\n[Rules Status]")
        if rules_diff.get("local_rules_missing"):
            print(f"  Local rules MISSING (package has snapshot)")
        elif rules_diff.get("identical"):
            print(f"  Local rules IDENTICAL to package snapshot")
        else:
            add_cnt = len(rules_diff.get("added", []))
            rem_cnt = len(rules_diff.get("removed", []))
            print(f"  Rules differ: +{add_cnt}/-{rem_cnt} lines from package snapshot")
            added = rules_diff.get("added", [])
            removed = rules_diff.get("removed", [])
            if added[:3]:
                print(f"  In package, not in local:")
                for ln, line in added[:3]:
                    print(f"    + L{ln}: {line.strip()[:60]}")
            if removed[:3]:
                print(f"  In local, not in package:")
                for ln, line in removed[:3]:
                    print(f"    - L{ln}: {line.strip()[:60]}")

    if target_takeover.get("resumed_across_restart"):
        print(f"\n[Cross-Restart]")
        print(f"  This session has been RESUMED after a process restart.")

    print(f"\n{'=' * 70}")
    if is_pending:
        print(f"STATUS: PENDING CONFIRMATION")
        print(f"  Confirm:  release_cli.py --state {state_path} takeover_confirm{force_flag_for(state)}")
        print(f"  Revoke:   release_cli.py --state {state_path} takeover_revoke")
    else:
        print(f"STATUS: CONFIRMED (persisted in takeover_history and confirmed_takeover_sessions)")
    print(f"{'=' * 70}")
    print()


def force_flag_for(state):
    pending = state.get("pending_takeover")
    if not pending:
        return ""
    blocked = pending.get("blocked_reasons", {})
    if blocked.get("target_approved") or blocked.get("target_newer"):
        return " --force"
    return ""


def cmd_takeover_confirm(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("takeover_history", [])
    state.setdefault("confirmed_takeover_sessions", {})

    pending = state.get("pending_takeover")
    if not pending:
        print("[ERROR] No pending takeover found.")
        print("  Run 'import_package' first to reconcile and create pending_takeover.")
        sys.exit(1)

    takeover_id = pending.get("takeover_id", "?")
    operator = getattr(args, "operator", None) or _get_identity()

    package_path = pending.get("package_path_full") or pending.get("package_path")
    package = None
    if package_path and os.path.exists(package_path):
        try:
            with open(package_path, "r", encoding="utf-8") as f:
                package = json.load(f)
        except Exception:
            package = None

    existing_pkg_info = {
        "package_checksum": pending.get("package_checksum"),
        "exported_at": pending.get("exported_at"),
    }
    if package:
        fresh_conflicts = _detect_takeover_conflicts(
            pending.get("pre_import_state"), package, args.rules, existing_pkg_info
        )
        prior_types = {c.get("type") for c in pending.get("conflicts", [])}
        new_conflicts = [c for c in fresh_conflicts if c.get("type") not in prior_types]
    else:
        new_conflicts = []

    if new_conflicts:
        print(f"[WARNING] New conflicts since reconcile ({len(new_conflicts)}):")
        for c in new_conflicts:
            print(f"  - {c['type']}: {c['message']}")
        pending.setdefault("conflicts", []).extend(new_conflicts)

    blocked = pending.get("blocked_reasons", {})
    has_blocks = blocked.get("target_approved") or blocked.get("target_newer")
    if has_blocks and not getattr(args, "force", False):
        print(f"[BLOCKED] Cannot confirm without --force:")
        if blocked.get("target_approved"):
            print(f"  - Target state was already APPROVED at reconcile time")
        if blocked.get("target_newer"):
            print(f"  - Target state was NEWER than package state at reconcile time")
        print(f"  Re-run with --force to confirm anyway, or use takeover_revoke to cancel.")
        if pending.get("conflicts"):
            print(f"  Also, there are {len(pending['conflicts'])} unresolved conflicts; "
                  f"see takeover_detail for list. They will be marked override.")
        sys.exit(1)

    decision_mode = getattr(args, "decision", "override_all")
    per_item_raw = getattr(args, "per_item", None)
    per_item_decision = {}
    if per_item_raw:
        for pair in per_item_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                per_item_decision[k.strip()] = v.strip()

    decisions_log = pending.setdefault("decisions_log", [])
    ts_now = datetime.now().isoformat()

    for conflict in pending.get("conflicts", []):
        ctype = conflict.get("type")
        per_key = f"conflict:{ctype}"
        if per_key in per_item_decision:
            dec = per_item_decision[per_key]
        elif decision_mode == "override_all":
            dec = "override"
        elif decision_mode == "skip_all":
            dec = "skip"
        else:
            dec = "override"

        opts = conflict.get("resolution_options", ["override"])
        if dec not in opts and "override" in opts:
            dec = "override"
        decisions_log.append({
            "ts": ts_now,
            "conflict_type": ctype,
            "decision": dec,
            "operator": operator,
            "detail": f"conflict={ctype} decision={dec} mode={decision_mode}",
        })

    mode = pending.get("mode", "takeover")
    pre_import_state = pending.get("pre_import_state")
    post_import_state = pending.get("post_import_preview_state")

    if post_import_state is None:
        print("[ERROR] pending_takeover missing post_import_preview_state.")
        sys.exit(1)

    final_state = copy.deepcopy(post_import_state)
    final_state.setdefault("takeover_history", [])
    final_state.setdefault("confirmed_takeover_sessions", {})
    final_state.setdefault("audit_log", [])

    if mode == "takeover":
        _audit(final_state, "import_package_takeover",
               f"operator={operator} takeover_id={takeover_id} "
               f"package={pending.get('package_path')} "
               f"exported_by={pending.get('exported_by')} "
               f"exported_at={pending.get('exported_at')} "
               f"force={getattr(args, 'force', False)} "
               f"decisions={len(decisions_log)} conflicts={len(pending.get('conflicts', []))}")
    else:
        _audit(final_state, "import_package_merge",
               f"operator={operator} takeover_id={takeover_id} "
               f"package={pending.get('package_path')} "
               f"exported_by={pending.get('exported_by')} "
               f"exported_at={pending.get('exported_at')} "
               f"decisions={len(decisions_log)} conflicts={len(pending.get('conflicts', []))}")

    deep_diff = pending.get("diff")
    confirmed_at = datetime.now().isoformat()
    takeover_record = {
        "takeover_id": takeover_id,
        "imported_at": pending.get("imported_at"),
        "imported_by": pending.get("imported_by"),
        "import_pid": pending.get("import_pid"),
        "exported_by": pending.get("exported_by"),
        "exported_at": pending.get("exported_at"),
        "package_path": pending.get("package_path"),
        "package_checksum": pending.get("package_checksum"),
        "package_format_version": pending.get("package_format_version"),
        "mode": mode,
        "force": getattr(args, "force", False),
        "pre_import_state": pre_import_state,
        "post_import_state": copy.deepcopy(final_state),
        "diff": deep_diff,
        "handover_summary": pending.get("handover_summary"),
        "conflicts": pending.get("conflicts", []),
        "decisions_log": decisions_log,
        "rules_diff": pending.get("rules_diff"),
        "status": "confirmed",
        "confirmed": True,
        "confirmed_at": confirmed_at,
        "confirmed_by": operator,
        "resumed_across_restart": False,
    }
    final_state["takeover_history"].append(takeover_record)

    final_state["confirmed_takeover_sessions"][takeover_id] = {
        "takeover_id": takeover_id,
        "imported_at": pending.get("imported_at"),
        "imported_by": pending.get("imported_by"),
        "confirmed_at": confirmed_at,
        "confirmed_by": operator,
        "session_pid": os.getpid(),
        "session_created_at": datetime.now().isoformat(),
        "mode": mode,
        "package_path": pending.get("package_path"),
        "package_checksum": pending.get("package_checksum"),
        "decisions_log": decisions_log,
        "handover_summary": pending.get("handover_summary"),
        "revoked": False,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
        "resumed_across_restart": False,
        "resumed_count": 0,
    }

    _audit(final_state, "takeover_confirmed",
           f"takeover_id={takeover_id} operator={operator} mode={mode} "
           f"decisions={len(decisions_log)} conflicts={len(pending.get('conflicts', []))} "
           f"force={getattr(args, 'force', False)}")

    if package and package.get("rules_snapshot") and pending.get("apply_rules_snapshot"):
        rules_dir = os.path.dirname(args.rules)
        if not os.path.exists(rules_dir):
            os.makedirs(rules_dir, exist_ok=True)
        with open(args.rules, "w", encoding="utf-8") as f:
            f.write(package["rules_snapshot"])
        _audit(final_state, "rules_restored",
               f"from_package={pending.get('package_path')} operator={operator} "
               f"takeover_id={takeover_id}")

    final_state["pending_takeover"] = None

    save_state(final_state, state_path)

    print(f"\n{'=' * 70}")
    print(f"TAKEOVER CONFIRMED AND PERSISTED")
    print(f"{'=' * 70}")
    print(f"  Takeover ID:      {takeover_id}")
    print(f"  Confirmed at:     {confirmed_at}")
    print(f"  Confirmed by:     {operator}")
    print(f"  Mode:             {mode}")
    print(f"  Conflicts resolved: {len(pending.get('conflicts', []))}")
    if decisions_log:
        print(f"  Decisions applied:  {len(decisions_log)}")
    print(f"  Final state:      v{final_state.get('version')} / draft v{final_state.get('draft_version', 0)}")
    print(f"  Items:            {len(final_state.get('items', []))}")
    print(f"  Audit log:        {len(final_state.get('audit_log', []))} entries")
    print()
    print(f"  Session is now PERSISTED (cross-restart safe).")
    print(f"  Entry added to takeover_history and confirmed_takeover_sessions.")
    print()
    print(f"  Next steps:")
    print(f"     release_cli.py --state {state_path} status")
    print(f"     release_cli.py --state {state_path} audit_view")
    if final_state.get("pending_bulk_ops"):
        unresolved_indices = [i for i, p in enumerate(final_state["pending_bulk_ops"]) if not p.get("resolved")]
        for idx in unresolved_indices:
            print(f"     release_cli.py --state {state_path} bulk_amend --resume {idx} --decision overwrite")
    summary = pending.get("handover_summary") or {}
    next_steps = summary.get("suggested_next_steps", [])
    if next_steps:
        print(f"  From handover summary:")
        for s in next_steps:
            print(f"    - {s}")
    print(f"{'=' * 70}")
    print()


def cmd_takeover_revoke(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("takeover_history", [])
    state.setdefault("confirmed_takeover_sessions", {})

    takeover_id = getattr(args, "takeover_id", None)
    reason = getattr(args, "reason", "") or "manual revocation"
    operator = getattr(args, "operator", None) or _get_identity()
    force = getattr(args, "force", False)

    pending = state.get("pending_takeover")
    if pending and (takeover_id is None or pending.get("takeover_id") == takeover_id):
        tid = pending.get("takeover_id", "?")
        print(f"\n[Revoking PENDING TAKEOVER] {tid}")
        print(f"  Reason: {reason}")

        _audit(state, "takeover_revoked_pending",
               f"takeover_id={tid} operator={operator} reason={reason}")

        pre_import_state = pending.get("pre_import_state")
        if pre_import_state is None:
            state["pending_takeover"] = None
            if not state.get("items") and not state.get("imported_batches"):
                if os.path.exists(state_path):
                    os.remove(state_path)
                print(f"  Pending takeover removed. State file was fresh import - deleted.")
            else:
                state["pending_takeover"] = None
                save_state(state, state_path)
                print(f"  Pending takeover removed. Original state restored.")
        else:
            restored = copy.deepcopy(pre_import_state)
            restored["takeover_history"] = state.get("takeover_history", [])
            restored["confirmed_takeover_sessions"] = state.get("confirmed_takeover_sessions", {})
            restored["audit_log"] = state.get("audit_log", [])
            restored["pending_takeover"] = None
            _audit(restored, "takeover_revoked_pending_restore",
                   f"takeover_id={tid} operator={operator} reason={reason}")
            save_state(restored, state_path)
            print(f"  Pending takeover removed. Pre-import state restored.")

        print(f"[OK] Pending takeover {tid} revoked successfully.")
        print()
        return

    target_id = takeover_id
    if target_id is None:
        sess_ids = list(state.get("confirmed_takeover_sessions", {}).keys())
        if not sess_ids:
            print("[ERROR] No pending_takeover and no confirmed_takeover_sessions to revoke.")
            sys.exit(1)
        target_id = sess_ids[-1]

    sess = state.get("confirmed_takeover_sessions", {}).get(target_id)
    if not sess:
        print(f"[ERROR] Confirmed session for takeover_id={target_id} not found.")
        sys.exit(1)

    if not force:
        print(f"[WARNING] You are revoking a CONFIRMED (persisted) takeover session.")
        print(f"  Takeover ID: {target_id}")
        print(f"  Confirmed at: {sess.get('confirmed_at')}")
        print(f"  Confirmed by: {sess.get('confirmed_by')}")
        print(f"  This will rollback state to pre-import snapshot (if available).")
        print(f"  Re-run with --force to proceed, or --takeover_id to pick another.")
        sys.exit(1)

    tk_record = None
    for tk in state.get("takeover_history", []):
        if tk.get("takeover_id") == target_id:
            tk_record = tk
            break

    tid = target_id
    print(f"\n[Revoking CONFIRMED TAKEOVER SESSION] {tid}")
    print(f"  Reason: {reason}")
    print(f"  Force:  yes")

    ts_now = datetime.now().isoformat()
    sess["revoked"] = True
    sess["revoked_at"] = ts_now
    sess["revoked_by"] = operator
    sess["revoke_reason"] = reason

    if tk_record:
        tk_record["revoked"] = True
        tk_record["revoked_at"] = ts_now
        tk_record["revoked_by"] = operator
        tk_record["revoke_reason"] = reason

    _audit(state, "takeover_revoked_confirmed",
           f"takeover_id={tid} operator={operator} reason={reason}")

    pre_import = tk_record.get("pre_import_state") if tk_record else None
    if pre_import is not None:
        restored = copy.deepcopy(pre_import)
        restored["takeover_history"] = state.get("takeover_history", [])
        restored["confirmed_takeover_sessions"] = state.get("confirmed_takeover_sessions", {})
        restored["audit_log"] = state.get("audit_log", [])
        restored["pending_takeover"] = None
        _audit(restored, "takeover_revoked_confirmed_rollback",
               f"takeover_id={tid} operator={operator} reason={reason}")
        save_state(restored, state_path)
        print(f"  State rolled back to pre-import snapshot.")
    else:
        save_state(state, state_path)
        print(f"  Session marked revoked. (No pre-import snapshot available for rollback)")

    print(f"[OK] Confirmed takeover {tid} revoked successfully.")
    print()


def _ensure_item_versioning(item, operator="system"):
    item.setdefault("_version", 1)
    item.setdefault("_last_modified_at", datetime.now().isoformat())
    item.setdefault("_last_modified_by", operator)
    return item


def _bump_item_version(item, operator="system"):
    item["_version"] = item.get("_version", 0) + 1
    item["_last_modified_at"] = datetime.now().isoformat()
    item["_last_modified_by"] = operator


def _audit(state, action, detail):
    state.setdefault("audit_log", []).append({
        "timestamp": datetime.now().isoformat(),
        "action": action,
        "detail": detail,
    })


def _render_markdown(state):
    lines = []
    v = state.get("version", "UNKNOWN")
    lines.append(f"# Release Notes v{v}")
    lines.append("")

    lines.append("## Overview")
    lines.append("")
    lines.append(f"Version **{v}** release notes. Please review all sections before approval.")
    lines.append("")

    lines.append("## Changes")
    lines.append("")
    items = state.get("items", [])
    if items:
        by_cat = {}
        for it in items:
            cat = it.get("category", "other")
            by_cat.setdefault(cat, []).append(it)
        for cat, group in sorted(by_cat.items()):
            lines.append(f"### {cat.upper()}")
            lines.append("")
            for it in group:
                risk = it.get("risk_level", "unknown")
                owner = it.get("owner") or "(missing owner)"
                lines.append(f"- **{it.get('id', '?')}** {it.get('title', '')} `risk:{risk}` `owner:{owner}`")
                desc = it.get("description", "")
                if desc:
                    lines.append(f"  > {desc}")
            lines.append("")
    else:
        lines.append("_No change items imported yet._")
        lines.append("")

    lines.append("## Migration")
    lines.append("")
    migs = state.get("migration_reminders", [])
    if migs:
        for m in migs:
            mid = m.get("id", "?")
            processed = state.get("migration_processed", {}).get(mid, False)
            status_tag = "[PROCESSED]" if processed else "[PENDING]"
            lines.append(f"- **{mid}** {m.get('title', '')} {status_tag}")
            lines.append(f"  > {m.get('description', '')}")
            ar = m.get("action_required", "")
            if ar:
                lines.append(f"  - Action: {ar}")
            lines.append("")
    else:
        lines.append("_No migration reminders._")
        lines.append("")

    lines.append("## Known Issues")
    lines.append("")
    kis = state.get("known_issues", [])
    if kis:
        reviewed = state.get("known_issues_reviewed", False)
        tag = "[REVIEWED]" if reviewed else "[NOT REVIEWED]"
        lines.append(f"_Status: {tag}_")
        lines.append("")
        for ki in kis:
            lines.append(f"- **{ki.get('id', '?')}** {ki.get('title', '')}")
            lines.append(f"  > {ki.get('description', '')}")
            wa = ki.get("workaround", "")
            if wa:
                lines.append(f"  - Workaround: {wa}")
            lines.append("")
    else:
        lines.append("_No known issues._")
        lines.append("")

    lines.append("---")
    lines.append(f"_Generated at {datetime.now().isoformat()} | Draft v{state.get('draft_version', 0)}_")
    return "\n".join(lines)


def cmd_import(args, rules):
    state_path = args.state
    state = load_state(state_path)
    manifest_path = args.manifest
    if not os.path.exists(manifest_path):
        print(f"[ERROR] manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        manifest = json.load(f)

    batch_id = manifest.get("batch_id", "")
    version = manifest.get("version", "UNKNOWN")

    if state is None:
        state = _new_state(version, batch_id)

    if state.get("approved"):
        print(
            f"[REJECTED] Current version {state.get('version')} is already approved. "
            "Roll back or reset state before importing a new batch. Original draft preserved."
        )
        _audit(state, "import_rejected", f"approved state blocks new import: batch={batch_id}")
        save_state(state, state_path)
        sys.exit(1)

    if batch_id and batch_id in state.get("imported_batches", []):
        print(f"[REJECTED] batch '{batch_id}' already imported, duplicate import blocked. Original draft preserved.")
        _audit(state, "import_rejected", f"duplicate batch: {batch_id}")
        save_state(state, state_path)
        sys.exit(1)

    required_fields = rules.get("required_fields_per_item", [])
    valid_risks = rules.get("valid_risk_levels", [])
    new_items = manifest.get("items", [])

    field_errors = []
    risk_errors = []
    owner_missing = []
    for item in new_items:
        for rf in required_fields:
            if rf not in item or not str(item.get(rf, "")).strip():
                field_errors.append(f"  item {item.get('id', '?')}: missing required field '{rf}'")
                if rf == "owner":
                    owner_missing.append(item.get("id", "?"))
        rl = item.get("risk_level", "")
        if rl and valid_risks and rl not in valid_risks:
            risk_errors.append(f"  item {item.get('id', '?')}: invalid risk_level '{rl}' (valid: {valid_risks})")

    warnings = []
    if field_errors:
        warnings.append("Missing required fields (items imported but cannot approve until fixed):")
        warnings.extend(field_errors)
    if risk_errors:
        warnings.append("Invalid risk levels (items imported but cannot approve until fixed):")
        warnings.extend(risk_errors)
    if warnings:
        print("[WARNING] Validation issues detected:")
        for w in warnings:
            print(w)

    if version != state.get("version") and state.get("version") is not None:
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None

    state["version"] = version
    for item in new_items:
        _ensure_item_versioning(item, operator=f"import:{batch_id}")
    state["items"].extend(new_items)
    for m in manifest.get("migration_reminders", []):
        _ensure_item_versioning(m, operator=f"import:{batch_id}")
    state["migration_reminders"].extend(manifest.get("migration_reminders", []))
    for ki in manifest.get("known_issues", []):
        _ensure_item_versioning(ki, operator=f"import:{batch_id}")
    state["known_issues"].extend(manifest.get("known_issues", []))

    for m in manifest.get("migration_reminders", []):
        mid = m.get("id", "")
        if mid and mid not in state.get("migration_processed", {}):
            state.setdefault("migration_processed", {})[mid] = False

    if batch_id:
        state.setdefault("imported_batches", []).append(batch_id)
    state["current_batch_id"] = batch_id

    for sec in rules.get("required_sections", []):
        state.setdefault("confirmations", {}).setdefault(sec, False)

    if state.get("rules_snapshot") is None:
        state["rules_snapshot"] = copy.deepcopy(rules)
        state["rules_version"] = _compute_rules_checksum(rules)

    _audit(state, "import", f"batch={batch_id}, version={version}, items={len(new_items)}")
    save_state(state, state_path)
    print(f"[OK] Imported {len(new_items)} items from batch '{batch_id}' (version {version})")


def cmd_draft(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    state["draft_version"] += 1
    dv = state["draft_version"]
    md = _render_markdown(state)

    draft_snapshot = {
        "version": dv,
        "markdown": md,
        "created_at": datetime.now().isoformat(),
        "items_snapshot": copy.deepcopy(state["items"]),
        "migration_reminders_snapshot": copy.deepcopy(state.get("migration_reminders", [])),
        "known_issues_snapshot": copy.deepcopy(state.get("known_issues", [])),
        "confirmations_snapshot": copy.deepcopy(state.get("confirmations", {})),
        "migration_processed_snapshot": copy.deepcopy(state.get("migration_processed", {})),
        "known_issues_reviewed_snapshot": state.get("known_issues_reviewed", False),
    }
    state["drafts"].append(draft_snapshot)

    _audit(state, "draft_generated", f"draft_v={dv} items={len(state['items'])} migs={len(state.get('migration_reminders', []))} kis={len(state.get('known_issues', []))}")
    save_state(state, state_path)

    out_path = args.output or os.path.join(SCRIPT_DIR, f"release_notes_v{state['version']}_draft{dv}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[OK] Draft v{dv} generated -> {out_path}")


def cmd_confirm(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    section = args.section
    required = rules.get("required_sections", [])

    if section == "migration":
        migs = state.get("migration_reminders", [])
        for m in migs:
            mid = m.get("id", "")
            if mid:
                state.setdefault("migration_processed", {})[mid] = True
        state.setdefault("confirmations", {})["migration"] = True
        _audit(state, "confirm", f"section=migration, all {len(migs)} reminders marked processed")
    elif section == "known_issues":
        state["known_issues_reviewed"] = True
        state.setdefault("confirmations", {})["known_issues"] = True
        _audit(state, "confirm", "section=known_issues, reviewed")
    elif section in required:
        state.setdefault("confirmations", {})[section] = True
        _audit(state, "confirm", f"section={section}")
    else:
        print(f"[ERROR] Unknown section '{section}'. Valid: {required + ['migration', 'known_issues']}")
        sys.exit(1)

    save_state(state, state_path)
    print(f"[OK] Section '{section}' confirmed.")


def cmd_reject(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    section = args.section
    reason = args.reason or "no reason given"

    if section == "migration":
        migs = state.get("migration_reminders", [])
        for m in migs:
            mid = m.get("id", "")
            if mid:
                state.setdefault("migration_processed", {})[mid] = False
        state.setdefault("confirmations", {})["migration"] = False
    elif section == "known_issues":
        state["known_issues_reviewed"] = False
        state.setdefault("confirmations", {})["known_issues"] = False
    else:
        state.setdefault("confirmations", {})[section] = False

    _audit(state, "reject", f"section={section}, reason={reason}")
    save_state(state, state_path)
    print(f"[OK] Section '{section}' rejected. Reason: {reason}")


def cmd_approve(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    if state.get("approved"):
        print("[ERROR] Already approved.")
        sys.exit(1)

    errors = []

    required_fields = rules.get("required_fields_per_item", [])
    valid_risks = rules.get("valid_risk_levels", [])

    owner_missing = []
    risk_invalid = []
    for item in state.get("items", []):
        owner = item.get("owner", "")
        if not owner or not owner.strip():
            owner_missing.append(item.get("id", "?"))
        rl = item.get("risk_level", "")
        if rl and valid_risks and rl not in valid_risks:
            risk_invalid.append(item.get("id", "?"))

    if owner_missing:
        errors.append(f"Missing owner on items: {owner_missing}")
    if risk_invalid:
        errors.append(f"Invalid risk_level on items: {risk_invalid} (valid: {valid_risks})")

    required_sections = rules.get("required_sections", [])
    unconfirmed = [s for s in required_sections if not state.get("confirmations", {}).get(s, False)]
    if unconfirmed:
        errors.append(f"Unconfirmed required sections: {unconfirmed}")

    migs = state.get("migration_reminders", [])
    unprocessed = [m.get("id", "?") for m in migs if not state.get("migration_processed", {}).get(m.get("id", ""), False)]
    if unprocessed:
        errors.append(f"Unprocessed migration reminders: {unprocessed}")

    if not state.get("known_issues_reviewed", False) and state.get("known_issues"):
        errors.append("Known issues not reviewed.")

    if errors:
        print("[REJECTED] Cannot approve. Issues:")
        for e in errors:
            print(f"  - {e}")
        _audit(state, "approve_rejected", "; ".join(errors))
        save_state(state, state_path)
        sys.exit(1)

    state["approved"] = True
    state["approved_at_version"] = state.get("version")
    state["approved_at_draft_version"] = state.get("draft_version")
    _audit(state, "approved", f"version={state.get('version')}, draft_v{state.get('draft_version')}")
    save_state(state, state_path)
    print(f"[OK] Version {state.get('version')} approved!")


def cmd_rollback(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    was_approved = state.get("approved", False)

    if was_approved:
        state["approved"] = False
        prev_ver = state.get("approved_at_version")
        prev_dv = state.get("approved_at_draft_version")
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        _audit(state, "unapprove", f"approval cleared for version={prev_ver} draft_v{prev_dv}")

        if len(state.get("drafts", [])) < 1:
            save_state(state, state_path)
            print(f"[OK] Approval rolled back (was version {prev_ver} draft_v{prev_dv}). No draft snapshots to revert.")
            return

        target = state["drafts"][-1]
        state["items"] = copy.deepcopy(target["items_snapshot"])
        state["migration_reminders"] = copy.deepcopy(target["migration_reminders_snapshot"])
        state["known_issues"] = copy.deepcopy(target["known_issues_snapshot"])
        state["confirmations"] = copy.deepcopy(target["confirmations_snapshot"])
        state["migration_processed"] = copy.deepcopy(target["migration_processed_snapshot"])
        state["known_issues_reviewed"] = target["known_issues_reviewed_snapshot"]
        state["draft_version"] = target["version"]

        _audit(
            state,
            "rollback",
            f"after unapprove: restored approved snapshot draft v{target['version']} (was approved at draft_v{prev_dv})"
        )
        save_state(state, state_path)
        print(
            f"[OK] Approval cleared and state restored to the approved snapshot "
            f"(draft v{target['version']}, version {state.get('version')})."
        )
        return

    if len(state.get("drafts", [])) < 2:
        print("[ERROR] No previous draft to rollback to (need at least 2 drafts).")
        sys.exit(1)

    current = state["drafts"][-1]
    previous = state["drafts"][-2]

    state["items"] = copy.deepcopy(previous["items_snapshot"])
    state["migration_reminders"] = copy.deepcopy(previous["migration_reminders_snapshot"])
    state["known_issues"] = copy.deepcopy(previous["known_issues_snapshot"])
    state["confirmations"] = copy.deepcopy(previous["confirmations_snapshot"])
    state["migration_processed"] = copy.deepcopy(previous["migration_processed_snapshot"])
    state["known_issues_reviewed"] = previous["known_issues_reviewed_snapshot"]
    state["draft_version"] = previous["version"]

    state["drafts"] = state["drafts"][:-1]

    _audit(state, "rollback", f"from draft v{current['version']} back to draft v{previous['version']}")
    save_state(state, state_path)
    print(f"[OK] Rolled back to draft v{previous['version']}.")


def cmd_amend(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    item_id = args.item_id
    fields = args.field
    operator = getattr(args, "operator", "cli:amend")

    if not fields:
        print("[ERROR] No fields to amend. Use --field key=value (may repeat).")
        sys.exit(1)

    target = None
    for it in state.get("items", []):
        if it.get("id") == item_id:
            target = it
            break

    if target is None:
        print(f"[ERROR] Item '{item_id}' not found in current state.")
        sys.exit(1)

    _ensure_item_versioning(target, operator=operator)
    valid_risks = rules.get("valid_risk_levels", [])
    changes = {}
    for fv in fields:
        if "=" not in fv:
            print(f"[ERROR] Invalid field format '{fv}', expected key=value")
            sys.exit(1)
        k, v = fv.split("=", 1)
        if k == "risk_level" and valid_risks and v not in valid_risks:
            print(f"[ERROR] Invalid risk_level '{v}'. Valid: {valid_risks}")
            sys.exit(1)
        changes[k] = v

    for k, v in changes.items():
        old = target.get(k, "")
        target[k] = v
        print(f"  {item_id}.{k}: '{old}' -> '{v}'")

    _bump_item_version(target, operator=operator)

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print(f"  [NOTE] Approval cleared (data changed after approval).")

    _audit(state, "amend", f"item={item_id} operator={operator} fields={changes}")
    save_state(state, state_path)
    print(f"[OK] Item '{item_id}' amended.")


def cmd_export(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    if not state.get("approved"):
        print("[ERROR] Version not approved yet. Run 'approve' first.")
        sys.exit(1)

    if (
        state.get("approved_at_version") != state.get("version")
        or state.get("approved_at_draft_version") != state.get("draft_version")
    ):
        print(
            "[REJECTED] State has drifted since approval "
            f"(approved v{state.get('approved_at_version')}/draft{state.get('approved_at_draft_version')} "
            f"!= current v{state.get('version')}/draft{state.get('draft_version')}). "
            "Run 'approve' again after resolving drift."
        )
        _audit(
            state,
            "export_rejected",
            f"drift detected: approved v{state.get('approved_at_version')}/d{state.get('approved_at_draft_version')} "
            f"vs current v{state.get('version')}/d{state.get('draft_version')}"
        )
        save_state(state, state_path)
        sys.exit(1)

    md = _render_markdown(state)
    out_path = args.output or os.path.join(SCRIPT_DIR, f"release_notes_v{state['version']}_final.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    _audit(state, "export", f"version={state['version']} draft_v{state.get('draft_version')} -> {out_path}")
    save_state(state, state_path)
    print(f"[OK] Final release notes exported -> {out_path}")


def cmd_status(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[INFO] No state found. Run 'import' first.")
        return

    print(f"Version:       {state.get('version', '?')}")
    print(f"Draft:         v{state.get('draft_version', 0)}")
    print(f"Approved:      {state.get('approved', False)}")
    if state.get("approved"):
        print(f"Approved at:   v{state.get('approved_at_version')} / draft v{state.get('approved_at_draft_version')}")
    print(f"Items:         {len(state.get('items', []))}")
    print(f"Batches:       {state.get('imported_batches', [])}")
    print()
    print("Confirmations:")
    for sec, val in state.get("confirmations", {}).items():
        tag = "DONE" if val else "PENDING"
        print(f"  {sec}: {tag}")
    print()
    print("Migration reminders:")
    for mid, proc in state.get("migration_processed", {}).items():
        tag = "PROCESSED" if proc else "PENDING"
        print(f"  {mid}: {tag}")
    print()
    print(f"Known issues reviewed: {state.get('known_issues_reviewed', False)}")
    print()
    print(f"Draft history: {len(state.get('drafts', []))} draft(s)")
    print(f"Audit entries:  {len(state.get('audit_log', []))}")

    pending = state.get("pending_takeover")
    if pending:
        print()
        print(f"{'=' * 60}")
        print(f"[TAKEOVER: PENDING CONFIRMATION]")
        print(f"  Takeover ID:      {pending.get('takeover_id', '?')}")
        print(f"  Imported at:      {pending.get('imported_at')}")
        print(f"  Imported by:      {pending.get('imported_by')}")
        print(f"  Exported by:      {pending.get('exported_by')}")
        print(f"  Mode:             {pending.get('mode')}")
        print(f"  Package:          {pending.get('package_path')}")
        conflicts = pending.get("conflicts", [])
        if conflicts:
            print(f"  Conflicts:        {len(conflicts)} detected (run takeover_detail for list)")
        blocked = pending.get("blocked_reasons", {})
        if blocked.get("target_approved") or blocked.get("target_newer"):
            print(f"  BLOCKS:           need --force during takeover_confirm")
        summary = pending.get("handover_summary") or {}
        if summary.get("suggested_next_steps"):
            print(f"  Suggested next:   {summary['suggested_next_steps'][0]}")
        print(f"  Next:             takeover_detail -> takeover_confirm{force_flag_for(state)} | takeover_revoke")
        print(f"{'=' * 60}")

    sessions = state.get("confirmed_takeover_sessions", {})
    active_sessions = [s for s in sessions.values() if not s.get("revoked")]
    if active_sessions:
        print()
        print(f"[Active Takeover Sessions: {len(active_sessions)}]")
        for s in active_sessions:
            resumed_tag = " [RESUMED]" if s.get("resumed_across_restart") else ""
            print(f"  - {s.get('takeover_id')} by {s.get('confirmed_by')} @ {s.get('confirmed_at')}"
                  f"{resumed_tag} (mode={s.get('mode')})")

    tk_hist = state.get("takeover_history", [])
    revoked = [t for t in tk_hist if t.get("revoked")]
    if revoked:
        print(f"[Revoked takeovers: {len(revoked)}]")
        for t in revoked:
            print(f"  - {t.get('takeover_id')} revoked by {t.get('revoked_by')} @ {t.get('revoked_at')}"
                  f" reason={t.get('revoke_reason','')[:40]}")

    rules_version = state.get("rules_version")
    if rules_version:
        print()
        print(f"Rules version:   {rules_version[:16]}...")
        rules_hist = state.get("rules_upgrade_history", [])
        if rules_hist:
            active = [r for r in rules_hist if not r.get("revoked")]
            revoked_rules = [r for r in rules_hist if r.get("revoked")]
            print(f"Rules upgrades:  {len(rules_hist)} total ({len(active)} active, {len(revoked_rules)} revoked)")

    pending_rules = state.get("pending_rules_upgrade")
    if pending_rules:
        print()
        print(f"{'=' * 60}")
        print(f"[RULES UPGRADE: PENDING]")
        print(f"  Upgrade ID:    {pending_rules.get('upgrade_id', '?')}")
        print(f"  Checked at:    {pending_rules.get('checked_at')}")
        impact = pending_rules.get("impact", {}).get("summary", {})
        print(f"  Auto-migrate:  {impact.get('auto_migratable_count', 0)}")
        print(f"  Conf rollback: {impact.get('confirmation_rollback_count', 0)}")
        print(f"  Manual dec:    {impact.get('manual_decision_count', 0)}")
        if pending_rules.get("resumed_across_restart"):
            print(f"  Resumed:       Yes (cross-restart)")
        print(f"  Next:          rules_upgrade_apply | rules_upgrade_skip")
        print(f"{'=' * 60}")

    pending_ru_import = state.get("pending_rules_upgrade_import")
    if pending_ru_import:
        print()
        print(f"{'=' * 60}")
        print(f"[RULES UPGRADE HANDOVER: PENDING CONFIRMATION]")
        print(f"  Import ID:       {pending_ru_import.get('import_id', '?')}")
        print(f"  Upgrade ID:      {pending_ru_import.get('upgrade_id')}")
        print(f"  Imported at:     {pending_ru_import.get('imported_at')}")
        print(f"  Imported by:     {pending_ru_import.get('imported_by')}")
        print(f"  Exported by:     {pending_ru_import.get('exported_by')}")
        conflicts = pending_ru_import.get("conflicts", [])
        if conflicts:
            print(f"  Conflicts:       {len(conflicts)} detected (run rules_upgrade_handover_detail for list)")
        if pending_ru_import.get("resumed_across_restart"):
            print(f"  Resumed:         Yes (cross-restart)")
        print(f"  Next:            rules_upgrade_handover_detail -> rules_upgrade_handover_confirm | rules_upgrade_handover_revoke")
        print(f"{'=' * 60}")

    ru_imports = state.get("imported_rules_upgrade_packages", [])
    active_ru_imports = [imp for imp in ru_imports if not imp.get("revoked")]
    if active_ru_imports:
        print()
        print(f"[Active Rules Upgrade Handover Imports: {len(active_ru_imports)}]")
        for imp in active_ru_imports:
            resumed_tag = " [RESUMED]" if imp.get("resumed_across_restart") else ""
            print(f"  - {imp.get('import_id')} (upgrade:{imp.get('upgrade_id')}) by {imp.get('confirmed_by')} @ {imp.get('confirmed_at')}"
                  f"{resumed_tag}")

    revoked_ru_imports = [imp for imp in ru_imports if imp.get("revoked")]
    if revoked_ru_imports:
        print(f"[Revoked Rules Upgrade Handover Imports: {len(revoked_ru_imports)}]")
        for imp in revoked_ru_imports:
            print(f"  - {imp.get('import_id')} revoked by {imp.get('revoked_by')} @ {imp.get('revoked_at')}"
                  f" reason={imp.get('revoke_reason','')[:40]}")


def cmd_history(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[INFO] No state found.")
        return

    log = state.get("audit_log", [])
    if not log:
        print("[INFO] No audit entries.")
        return

    for entry in log:
        ts = entry.get("timestamp", "?")
        action = entry.get("action", "?")
        detail = entry.get("detail", "")
        print(f"  [{ts}] {action}: {detail}")


def cmd_audit_view(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[INFO] No state found.")
        return

    state.setdefault("takeover_history", [])
    state.setdefault("confirmed_takeover_sessions", {})
    state.setdefault("rules_upgrade_handover_history", [])
    state.setdefault("imported_rules_upgrade_packages", [])
    takeover_history = state.get("takeover_history", [])
    sessions = state.get("confirmed_takeover_sessions", {})
    ru_imports = state.get("imported_rules_upgrade_packages", [])

    current_pid = os.getpid()
    state_updated = False
    audit_log = state.get("audit_log", [])
    confirmed_action_whitelist = {
        "takeover_snapshot_stored",
        "import_package_reconciled",
        "import_package_takeover",
        "import_package_merge",
        "takeover_confirmed",
        "rules_restored",
        "takeover_revoked_confirmed",
        "takeover_revoked_confirmed_rollback",
        "rules_upgrade_package_import_reconciled",
        "rules_upgrade_handover_confirmed",
        "rules_upgrade_handover_revoked",
        "rules_restored_from_handover_preview",
    }

    for takeover in takeover_history:
        tid = takeover.get("takeover_id")
        sess = sessions.get(tid, {})
        if not takeover.get("resumed_across_restart", False) and not sess.get("resumed_across_restart", False):
            import_pid = takeover.get("import_pid")
            session_pid = sess.get("session_pid")
            check_pid = session_pid if session_pid else import_pid
            imported_at = takeover.get("imported_at") or sess.get("imported_at")
            confirmed_at = takeover.get("confirmed_at") or sess.get("confirmed_at")
            baseline_ts = confirmed_at if confirmed_at else imported_at
            if check_pid is not None and check_pid != current_pid:
                has_subsequent_work = False
                for event in audit_log:
                    if event.get("timestamp", "") > (baseline_ts or "") and \
                       event.get("action") not in confirmed_action_whitelist:
                        has_subsequent_work = True
                        break
                if has_subsequent_work:
                    takeover["resumed_across_restart"] = True
                    sess["resumed_across_restart"] = True
                    sess["resumed_count"] = sess.get("resumed_count", 0) + 1
                    state_updated = True
                    _audit(state, "takeover_resumed_across_restart",
                           f"takeover_id={tid} import_pid={import_pid} "
                           f"session_pid={session_pid} current_pid={current_pid}")

    for ru_imp in ru_imports:
        tid = ru_imp.get("import_id")
        if not ru_imp.get("resumed_across_restart", False):
            import_pid = ru_imp.get("import_pid")
            imported_at = ru_imp.get("imported_at")
            confirmed_at = ru_imp.get("confirmed_at")
            baseline_ts = confirmed_at if confirmed_at else imported_at
            if import_pid is not None and import_pid != current_pid:
                has_subsequent_work = False
                for event in audit_log:
                    if event.get("timestamp", "") > (baseline_ts or "") and \
                       event.get("action") not in confirmed_action_whitelist:
                        has_subsequent_work = True
                        break
                if has_subsequent_work:
                    ru_imp["resumed_across_restart"] = True
                    state_updated = True
                    _audit(state, "rules_upgrade_handover_resumed_across_restart",
                           f"import_id={tid} import_pid={import_pid} "
                           f"current_pid={current_pid}")

    pending = state.get("pending_takeover")
    if pending:
        for event in audit_log:
            if event.get("timestamp", "") > pending.get("imported_at", "") and \
               event.get("action") not in ("import_package_reconciled",
                                           "rules_restored_preview",
                                           "takeover_detail"):
                if not pending.get("resumed_across_restart"):
                    pending["resumed_across_restart"] = True
                    state_updated = True
                    _audit(state, "pending_takeover_resumed_across_restart",
                           f"takeover_id={pending.get('takeover_id')} current_pid={current_pid}")
                break

    pending_ru = state.get("pending_rules_upgrade_import")
    if pending_ru:
        for event in audit_log:
            if event.get("timestamp", "") > pending_ru.get("imported_at", "") and \
               event.get("action") not in ("rules_upgrade_package_import_reconciled",
                                           "rules_restored_from_handover_preview",
                                           "rules_upgrade_handover_detail"):
                if not pending_ru.get("resumed_across_restart"):
                    pending_ru["resumed_across_restart"] = True
                    state_updated = True
                    _audit(state, "pending_rules_upgrade_handover_resumed_across_restart",
                           f"import_id={pending_ru.get('import_id')} current_pid={current_pid}")
                break

    if state_updated:
        save_state(state, state_path)

    has_takeover = bool(takeover_history or state.get("pending_takeover"))
    has_ru_handover = bool(ru_imports or state.get("pending_rules_upgrade_import"))

    if not has_takeover and not has_ru_handover:
        print("[INFO] No takeover or rules upgrade handover history found.")
        return

    print("\n" + "=" * 70)
    print("AUDIT VIEW - Takeover & Rules Upgrade Handover Timeline")
    print("=" * 70)

    if pending:
        tid = pending.get("takeover_id", "?")
        print(f"\n{'─' * 70}")
        print(f"[PENDING TAKEOVER] {tid}")
        print(f"{'─' * 70}")
        print(f"  Imported at:    {pending.get('imported_at')}")
        print(f"  Imported by:    {pending.get('imported_by')}")
        print(f"  Exported by:    {pending.get('exported_by')}")
        print(f"  Exported at:    {pending.get('exported_at')}")
        print(f"  Package:        {pending.get('package_path')}")
        print(f"  Mode:           {pending.get('mode')}")
        conflicts = pending.get("conflicts", [])
        if conflicts:
            print(f"  Conflicts:      {len(conflicts)} detected (use takeover_detail for list)")
        blocked = pending.get("blocked_reasons", {})
        if blocked.get("target_approved") or blocked.get("target_newer"):
            print(f"  Status:         BLOCKED (needs --force in takeover_confirm)")
        else:
            print(f"  Status:         AWAITING CONFIRMATION")
        if pending.get("resumed_across_restart"):
            print(f"  Cross-restart:  YES (pending takeover survived restart)")

    for idx, takeover in enumerate(reversed(takeover_history)):
        tid = takeover.get("takeover_id", "?")
        sess = sessions.get(tid, {})
        revoked = takeover.get("revoked") or sess.get("revoked")
        print(f"\n{'─' * 70}")
        status_tag = " [REVOKED]" if revoked else ""
        print(f"Takeover #{len(takeover_history) - idx}: {tid}{status_tag}")
        print(f"{'─' * 70}")
        print(f"  Imported at:    {takeover.get('imported_at')}")
        print(f"  Imported by:    {takeover.get('imported_by')}")
        print(f"  Exported by:    {takeover.get('exported_by')}")
        print(f"  Exported at:    {takeover.get('exported_at')}")
        print(f"  Package:        {takeover.get('package_path')}")
        print(f"  Mode:           {takeover.get('mode')}")
        print(f"  Force:          {takeover.get('force', False)}")
        if takeover.get('note'):
            print(f"  Note:           {takeover.get('note')}")

        if takeover.get("confirmed_at"):
            print(f"\n  [Confirmation]")
            print(f"    Confirmed at: {takeover.get('confirmed_at')}")
            print(f"    Confirmed by: {takeover.get('confirmed_by')}")

        if sess and sess.get("session_created_at"):
            print(f"\n  [Persistent Session]")
            print(f"    Created at:   {sess.get('session_created_at')}")
            print(f"    Session PID:  {sess.get('session_pid')}")
            if sess.get('resumed_count', 0) > 0:
                print(f"    Resumed cnt:  {sess.get('resumed_count')}")

        if revoked:
            print(f"\n  [Revocation]")
            print(f"    Revoked at:   {takeover.get('revoked_at') or sess.get('revoked_at')}")
            print(f"    Revoked by:   {takeover.get('revoked_by') or sess.get('revoked_by')}")
            if takeover.get("revoke_reason") or sess.get("revoke_reason"):
                print(f"    Reason:       {takeover.get('revoke_reason') or sess.get('revoke_reason')}")

        pre_state = takeover.get("pre_import_state")
        post_state = takeover.get("post_import_state")

        if pre_state and post_state:
            print(f"\n  [State Transition]")
            print(f"    Before: v{pre_state.get('version')} / draft v{pre_state.get('draft_version', 0)} / "
                  f"approved={pre_state.get('approved', False)}")
            print(f"    After:  v{post_state.get('version')} / draft v{post_state.get('draft_version', 0)} / "
                  f"approved={post_state.get('approved', False)}")

        diff = takeover.get("diff")
        if diff:
            print(f"\n  [Item Changes]")
            items_mod = diff["items"]["modified"]
            items_add = diff["items"]["added"]
            items_rem = diff["items"]["removed"]

            if items_add:
                print(f"    + ADDED ({len(items_add)} items):")
                for item in items_add[:5]:
                    print(f"      - {item['id']}: {item['item'].get('title', '')}")
                if len(items_add) > 5:
                    print(f"      ... and {len(items_add) - 5} more")

            if items_rem:
                print(f"    - REMOVED ({len(items_rem)} items):")
                for item in items_rem[:5]:
                    print(f"      - {item['id']}: {item['item'].get('title', '')}")
                if len(items_rem) > 5:
                    print(f"      ... and {len(items_rem) - 5} more")

            if items_mod:
                print(f"    ~ MODIFIED ({len(items_mod)} items):")
                for item in items_mod[:10]:
                    print(f"      - {item['id']}:")
                    for d in item["diffs"]:
                        old_repr = str(d["old"])[:30] if len(str(d["old"])) > 30 else str(d["old"])
                        new_repr = str(d["new"])[:30] if len(str(d["new"])) > 30 else str(d["new"])
                        print(f"        {d['field']}: {old_repr!r} -> {new_repr!r}")
                if len(items_mod) > 10:
                    print(f"      ... and {len(items_mod) - 10} more modified items")

            print(f"\n  [Confirmation Status Changes]")
            conf_changes = diff["metadata"]["confirmations_changed"]
            if conf_changes:
                for c in conf_changes:
                    status_old = "CONFIRMED" if c["old"] else "PENDING"
                    status_new = "CONFIRMED" if c["new"] else "PENDING"
                    arrow = ">>>" if c["new"] else "<<<"
                    print(f"    {arrow} {c['section']}: {status_old} -> {status_new}")
            else:
                print(f"    (no changes)")

            print(f"\n  [Migration Reminder Changes]")
            mig_mod = diff["migration_reminders"]["modified"]
            mig_add = diff["migration_reminders"]["added"]
            mig_rem = diff["migration_reminders"]["removed"]
            if mig_add or mig_rem or mig_mod:
                print(f"    +{len(mig_add)} -{len(mig_rem)} ~{len(mig_mod)}")
                for m in mig_mod[:3]:
                    print(f"      ~ {m['id']}:")
                    for d in m["diffs"]:
                        print(f"        {d['field']}: {d['old']!r} -> {d['new']!r}")
            else:
                print(f"    (no changes)")

            print(f"\n  [Known Issue Changes]")
            ki_mod = diff["known_issues"]["modified"]
            ki_add = diff["known_issues"]["added"]
            ki_rem = diff["known_issues"]["removed"]
            if ki_add or ki_rem or ki_mod:
                print(f"    +{len(ki_add)} -{len(ki_rem)} ~{len(ki_mod)}")
                for k in ki_mod[:3]:
                    print(f"      ~ {k['id']}:")
                    for d in k["diffs"]:
                        print(f"        {d['field']}: {d['old']!r} -> {d['new']!r}")
            else:
                print(f"    (no changes)")

        conflicts = takeover.get("conflicts", [])
        if conflicts:
            print(f"\n  [Conflicts] ({len(conflicts)})")
            for c in conflicts[:5]:
                print(f"    - {c.get('type')}: {c.get('message','')[:60]}")
            if len(conflicts) > 5:
                print(f"    ... and {len(conflicts) - 5} more")

        decisions = takeover.get("decisions_log", [])
        if decisions:
            print(f"\n  [Conflict Decisions] ({len(decisions)})")
            for d in decisions[:5]:
                print(f"    [{d.get('ts','')[:19]}] {d.get('conflict_type')}: "
                      f"{d.get('decision')} by {d.get('operator','')}")
            if len(decisions) > 5:
                print(f"    ... and {len(decisions) - 5} more")

        print(f"\n  [Decisions]")
        if takeover.get("confirmed_by"):
            print(f"    Decision maker: {takeover.get('confirmed_by')} (confirmed)")
            print(f"    Decision time:  {takeover.get('confirmed_at')}")
        else:
            print(f"    Decision maker: {takeover.get('imported_by')} (imported, legacy)")
            print(f"    Decision time:  {takeover.get('imported_at')}")
        if takeover.get('resumed_across_restart') or sess.get('resumed_across_restart'):
            print(f"    Cross-restart:  YES (resumed after process restart)")
            if sess.get('resumed_count', 0) > 0:
                print(f"    Resume count:   {sess.get('resumed_count')}")
        else:
            print(f"    Cross-restart:  NO (same process session)")

    pending_ru = state.get("pending_rules_upgrade_import")
    if pending_ru:
        tid = pending_ru.get("import_id", "?")
        print(f"\n{'─' * 70}")
        print(f"[PENDING RULES UPGRADE HANDOVER] {tid}")
        print(f"{'─' * 70}")
        print(f"  Imported at:    {pending_ru.get('imported_at')}")
        print(f"  Imported by:    {pending_ru.get('imported_by')}")
        print(f"  Exported by:    {pending_ru.get('exported_by')}")
        print(f"  Exported at:    {pending_ru.get('exported_at')}")
        print(f"  Upgrade ID:     {pending_ru.get('upgrade_id')}")
        print(f"  Package:        {pending_ru.get('package_path')}")
        conflicts = pending_ru.get("conflicts", [])
        if conflicts:
            print(f"  Conflicts:      {len(conflicts)} detected (use rules_upgrade_handover_detail for list)")
        else:
            print(f"  Conflicts:      None")
        print(f"  Status:         AWAITING CONFIRMATION")
        if pending_ru.get("resumed_across_restart"):
            print(f"  Cross-restart:  YES (pending handover survived restart)")

    for idx, ru_imp in enumerate(reversed(ru_imports)):
        tid = ru_imp.get("import_id", "?")
        if ru_imp.get("revoked"):
            revoked_tag = " [REVOKED]"
        else:
            revoked_tag = ""
        print(f"\n{'─' * 70}")
        print(f"[RULES UPGRADE HANDOVER] {tid}{revoked_tag}")
        print(f"{'─' * 70}")
        print(f"  Imported at:    {ru_imp.get('imported_at')}")
        print(f"  Imported by:    {ru_imp.get('imported_by')}")
        print(f"  Exported by:    {ru_imp.get('exported_by')}")
        print(f"  Exported at:    {ru_imp.get('exported_at')}")
        print(f"  Upgrade ID:     {ru_imp.get('upgrade_id')}")
        print(f"  Package:        {ru_imp.get('package_path')}")
        if ru_imp.get("confirmed_at"):
            print(f"  Confirmed at:   {ru_imp.get('confirmed_at')}")
            print(f"  Confirmed by:   {ru_imp.get('confirmed_by')}")
        if ru_imp.get("note"):
            print(f"  Note:           {ru_imp.get('note')}")

        conflicts = ru_imp.get("conflicts", [])
        if conflicts:
            print(f"\n  [Conflicts] ({len(conflicts)})")
            for c in conflicts[:5]:
                print(f"    - {c.get('type')}: {c.get('message','')[:60]}")
            if len(conflicts) > 5:
                print(f"    ... and {len(conflicts) - 5} more")

        decisions = ru_imp.get("decisions_log", [])
        if decisions:
            print(f"\n  [Conflict Decisions] ({len(decisions)})")
            for d in decisions[:5]:
                print(f"    [{d.get('ts','')[:19]}] {d.get('conflict_type')}: "
                      f"{d.get('decision')} by {d.get('operator','')}")
            if len(decisions) > 5:
                print(f"    ... and {len(decisions) - 5} more")

        print(f"\n  [Decisions]")
        if ru_imp.get("confirmed_by"):
            print(f"    Decision maker: {ru_imp.get('confirmed_by')} (confirmed)")
            print(f"    Decision time:  {ru_imp.get('confirmed_at')}")
        else:
            print(f"    Decision maker: {ru_imp.get('imported_by')} (imported)")
            print(f"    Decision time:  {ru_imp.get('imported_at')}")
        if ru_imp.get('resumed_across_restart'):
            print(f"    Cross-restart:  YES (resumed after process restart)")
        else:
            print(f"    Cross-restart:  NO (same process session)")

        if ru_imp.get("revoked"):
            print(f"\n  [Revoked]")
            print(f"    Revoked at:     {ru_imp.get('revoked_at')}")
            print(f"    Revoked by:     {ru_imp.get('revoked_by')}")
            print(f"    Reason:         {ru_imp.get('revoke_reason')}")

    print(f"\n{'─' * 70}")
    print(f"[Timeline Summary]")
    all_events = []

    log = state.get("audit_log", [])
    for entry in log:
        all_events.append({
            "ts": entry.get("timestamp", "?"),
            "type": "audit",
            "action": entry.get("action", "?"),
            "detail": entry.get("detail", ""),
        })

    for takeover in takeover_history:
        tk_ts = takeover.get("confirmed_at") or takeover.get("imported_at")
        tk_action = "takeover_confirmed" if takeover.get("confirmed_at") else "import_package_takeover"
        tk_by = takeover.get("confirmed_by") or takeover.get("imported_by")
        all_events.append({
            "ts": tk_ts,
            "type": "takeover",
            "action": tk_action,
            "detail": f"takeover_id={takeover.get('takeover_id')} by {tk_by}",
            "resumed": takeover.get("resumed_across_restart", False),
            "revoked": takeover.get("revoked", False),
        })
        if takeover.get("revoked"):
            all_events.append({
                "ts": takeover.get("revoked_at"),
                "type": "takeover",
                "action": "takeover_revoked",
                "detail": (f"takeover_id={takeover.get('takeover_id')} "
                          f"by {takeover.get('revoked_by')} reason={takeover.get('revoke_reason','')[:40]}"),
            })

    if pending:
        all_events.append({
            "ts": pending.get("imported_at"),
            "type": "takeover",
            "action": "pending_takeover_reconciled",
            "detail": f"takeover_id={pending.get('takeover_id')} by {pending.get('imported_by')}",
            "pending": True,
        })

    pending_ru = state.get("pending_rules_upgrade_import")
    if pending_ru:
        all_events.append({
            "ts": pending_ru.get("imported_at"),
            "type": "rules_upgrade_handover",
            "action": "pending_rules_upgrade_handover_reconciled",
            "detail": f"import_id={pending_ru.get('import_id')} upgrade_id={pending_ru.get('upgrade_id')} by {pending_ru.get('imported_by')}",
            "pending": True,
            "resumed": pending_ru.get("resumed_across_restart", False),
        })

    for ru_imp in ru_imports:
        ru_ts = ru_imp.get("confirmed_at") or ru_imp.get("imported_at")
        ru_action = "rules_upgrade_handover_confirmed" if ru_imp.get("confirmed_at") else "rules_upgrade_package_import_reconciled"
        ru_by = ru_imp.get("confirmed_by") or ru_imp.get("imported_by")
        all_events.append({
            "ts": ru_ts,
            "type": "rules_upgrade_handover",
            "action": ru_action,
            "detail": f"import_id={ru_imp.get('import_id')} upgrade_id={ru_imp.get('upgrade_id')} by {ru_by}",
            "resumed": ru_imp.get("resumed_across_restart", False),
            "revoked": ru_imp.get("revoked", False),
        })
        if ru_imp.get("revoked"):
            all_events.append({
                "ts": ru_imp.get("revoked_at"),
                "type": "rules_upgrade_handover",
                "action": "rules_upgrade_handover_revoked",
                "detail": (f"import_id={ru_imp.get('import_id')} "
                          f"by {ru_imp.get('revoked_by')} reason={ru_imp.get('revoke_reason','')[:40]}"),
            })

    all_events.sort(key=lambda x: x.get("ts", ""))

    print(f"  Total events: {len(all_events)}")
    print()
    for ev in all_events:
        tags = []
        if ev.get("pending"):
            tags.append("[PENDING]")
        if ev.get("revoked"):
            tags.append("[REVOKED]")
        if ev["type"] == "takeover":
            tags.append("[TAKEOVER]")
        elif ev["type"] == "rules_upgrade_handover":
            tags.append("[RU HANDOVER]")
        if not tags:
            tags.append("[AUDIT]")
        resumed_tag = " [RESUMED]" if ev.get("resumed") else ""
        tag_str = "".join(tags)
        print(f"  [{ev['ts']}] {tag_str}{resumed_tag} {ev['action']}: {ev['detail'][:80]}")

    print("\n" + "=" * 70)
    print("AUDIT VIEW COMPLETE")
    print("=" * 70)
    print()


def _find_in_list(items_list, item_id, id_field="id"):
    for it in items_list:
        if it.get(id_field) == item_id:
            return it
    return None


def _load_patch_file(patch_path):
    if not os.path.exists(patch_path):
        print(f"[ERROR] Patch file not found: {patch_path}")
        sys.exit(1)

    ext = os.path.splitext(patch_path)[1].lower()

    if ext == ".json":
        with open(patch_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    elif ext == ".csv":
        rows = []
        with open(patch_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cleaned = {k: v for k, v in row.items() if v is not None and v != ""}
                if cleaned.get("id"):
                    rows.append(cleaned)
        raw = {"items": rows}
    else:
        print(f"[ERROR] Unsupported patch format '{ext}'. Use .json or .csv")
        sys.exit(1)

    patch = {
        "patch_id": raw.get("patch_id", f"patch-{datetime.now().strftime('%Y%m%d%H%M%S')}"),
        "operator": raw.get("operator", "unknown"),
        "reason": raw.get("reason", ""),
        "based_on_draft_version": raw.get("based_on_draft_version"),
        "items": raw.get("items", []),
        "migration_reminders": raw.get("migration_reminders", []),
        "known_issues": raw.get("known_issues", []),
    }
    return patch


def _validate_patch_entries(patch, rules, state):
    valid_risks = rules.get("valid_risk_levels", [])
    errors = []

    for entry in patch["items"]:
        iid = entry.get("id", "?")
        rl = entry.get("risk_level")
        if rl and valid_risks and rl not in valid_risks:
            errors.append(f"  item {iid}: invalid risk_level '{rl}' (valid: {valid_risks})")

    for entry in patch.get("migration_reminders", []):
        if not entry.get("id"):
            errors.append(f"  migration_reminder missing 'id' field")

    for entry in patch.get("known_issues", []):
        if not entry.get("id"):
            errors.append(f"  known_issue missing 'id' field")

    return errors


def _compute_field_diff(existing, patch_entry, exclude_fields=None):
    exclude = {"_version", "_last_modified_at", "_last_modified_by"}
    if exclude_fields:
        exclude.update(exclude_fields)
    diffs = []
    for k, new_val in patch_entry.items():
        if k in exclude or k == "id":
            continue
        old_val = existing.get(k, "")
        if str(old_val) != str(new_val):
            diffs.append({"field": k, "old": old_val, "new": new_val})
    return diffs


def _format_diff(diffs, indent="    "):
    lines = []
    for d in diffs:
        lines.append(f"{indent}{d['field']}: {d['old']!r} -> {d['new']!r}")
    return "\n".join(lines)


def _detect_conflicts(state, patch):
    conflicts = []
    current_draft_v = state.get("draft_version", 0)
    patch_base_v = patch.get("based_on_draft_version")

    item_ids_in_patch = {e.get("id") for e in patch["items"]}
    mig_ids_in_patch = {e.get("id") for e in patch.get("migration_reminders", [])}
    ki_ids_in_patch = {e.get("id") for e in patch.get("known_issues", [])}

    changes_confirmed = state.get("confirmations", {}).get("changes", False)
    migration_confirmed = state.get("confirmations", {}).get("migration", False)
    ki_confirmed = state.get("confirmations", {}).get("known_issues", False)

    for entry in patch["items"]:
        iid = entry.get("id")
        if not iid:
            continue
        existing = _find_in_list(state.get("items", []), iid)
        if existing is None:
            conflicts.append({
                "type": "not_found",
                "target_type": "item",
                "id": iid,
                "message": f"Item '{iid}' does not exist in state",
                "diff": [],
                "resolution": None,
            })
            continue

        diffs = _compute_field_diff(existing, entry)
        if not diffs:
            conflicts.append({
                "type": "no_change",
                "target_type": "item",
                "id": iid,
                "message": f"Item '{iid}': patch values identical to current state",
                "diff": [],
                "resolution": "skip",
            })
            continue

        if patch_base_v is not None and current_draft_v > patch_base_v:
            conflicts.append({
                "type": "draft_newer",
                "target_type": "item",
                "id": iid,
                "message": f"Item '{iid}': current draft v{current_draft_v} is newer than patch base v{patch_base_v}",
                "diff": diffs,
                "resolution": None,
            })
            continue

        existing_v = existing.get("_version", 1)
        last_by = existing.get("_last_modified_by", "import")
        if last_by and not last_by.startswith("import:") and existing_v > 1:
            conflicts.append({
                "type": "already_modified",
                "target_type": "item",
                "id": iid,
                "message": f"Item '{iid}': already modified by '{last_by}' (v{existing_v})",
                "diff": diffs,
                "resolution": None,
            })
            continue

        if changes_confirmed:
            conflicts.append({
                "type": "section_confirmed",
                "target_type": "item",
                "id": iid,
                "message": f"Item '{iid}': 'changes' section already confirmed",
                "diff": diffs,
                "resolution": None,
            })
            continue

    for entry in patch.get("migration_reminders", []):
        mid = entry.get("id")
        if not mid:
            continue
        existing = _find_in_list(state.get("migration_reminders", []), mid)
        if existing is None:
            conflicts.append({
                "type": "not_found",
                "target_type": "migration",
                "id": mid,
                "message": f"Migration reminder '{mid}' does not exist",
                "diff": [],
                "resolution": None,
            })
            continue

        diffs = _compute_field_diff(existing, entry)
        if not diffs:
            conflicts.append({
                "type": "no_change",
                "target_type": "migration",
                "id": mid,
                "message": f"Migration '{mid}': patch values identical to current state",
                "diff": [],
                "resolution": "skip",
            })
            continue

        if patch_base_v is not None and current_draft_v > patch_base_v:
            conflicts.append({
                "type": "draft_newer",
                "target_type": "migration",
                "id": mid,
                "message": f"Migration '{mid}': current draft v{current_draft_v} newer than patch base v{patch_base_v}",
                "diff": diffs,
                "resolution": None,
            })
            continue

        existing_v = existing.get("_version", 1)
        last_by = existing.get("_last_modified_by", "import")
        if last_by and not last_by.startswith("import:") and existing_v > 1:
            conflicts.append({
                "type": "already_modified",
                "target_type": "migration",
                "id": mid,
                "message": f"Migration '{mid}': already modified by '{last_by}' (v{existing_v})",
                "diff": diffs,
                "resolution": None,
            })
            continue

        if migration_confirmed:
            conflicts.append({
                "type": "section_confirmed",
                "target_type": "migration",
                "id": mid,
                "message": f"Migration '{mid}': 'migration' section already confirmed",
                "diff": diffs,
                "resolution": None,
            })
            continue

    for entry in patch.get("known_issues", []):
        kid = entry.get("id")
        if not kid:
            continue
        existing = _find_in_list(state.get("known_issues", []), kid)
        if existing is None:
            conflicts.append({
                "type": "not_found",
                "target_type": "known_issue",
                "id": kid,
                "message": f"Known issue '{kid}' does not exist",
                "diff": [],
                "resolution": None,
            })
            continue

        diffs = _compute_field_diff(existing, entry)
        if not diffs:
            conflicts.append({
                "type": "no_change",
                "target_type": "known_issue",
                "id": kid,
                "message": f"Known issue '{kid}': patch values identical to current state",
                "diff": [],
                "resolution": "skip",
            })
            continue

        if patch_base_v is not None and current_draft_v > patch_base_v:
            conflicts.append({
                "type": "draft_newer",
                "target_type": "known_issue",
                "id": kid,
                "message": f"Known issue '{kid}': current draft v{current_draft_v} newer than patch base v{patch_base_v}",
                "diff": diffs,
                "resolution": None,
            })
            continue

        existing_v = existing.get("_version", 1)
        last_by = existing.get("_last_modified_by", "import")
        if last_by and not last_by.startswith("import:") and existing_v > 1:
            conflicts.append({
                "type": "already_modified",
                "target_type": "known_issue",
                "id": kid,
                "message": f"Known issue '{kid}': already modified by '{last_by}' (v{existing_v})",
                "diff": diffs,
                "resolution": None,
            })
            continue

        if ki_confirmed:
            conflicts.append({
                "type": "section_confirmed",
                "target_type": "known_issue",
                "id": kid,
                "message": f"Known issue '{kid}': 'known_issues' section already confirmed",
                "diff": diffs,
                "resolution": None,
            })
            continue

    return conflicts


def _collect_decision_required_keys(patch, conflicts, strict):
    required = set()
    if strict:
        for e in patch.get("items", []):
            if e.get("id"):
                required.add(("item", e["id"]))
        for e in patch.get("migration_reminders", []):
            if e.get("id"):
                required.add(("migration", e["id"]))
        for e in patch.get("known_issues", []):
            if e.get("id"):
                required.add(("known_issue", e["id"]))
    else:
        hard_types = {"draft_newer", "already_modified", "section_confirmed"}
        for c in conflicts:
            if c.get("type") in hard_types:
                required.add((c["target_type"], c["id"]))
    return required


def _enforce_decision_coverage(patch, conflicts, decisions, strict, operator, patch_id):
    required = _collect_decision_required_keys(patch, conflicts, strict)
    missing = []
    for key in required:
        if key not in decisions:
            missing.append(key)
    if not missing:
        return None
    missing_str = ", ".join(f"{k[0]}:{k[1]}" for k in sorted(missing))
    mode_label = "resume (strict)" if strict else "non-interactive"
    print(f"[ABORT] {mode_label} mode: missing decision evidence for {len(missing)} entries:")
    print(f"        {missing_str}")
    print(f"        The conflicts snapshot may have been lost or corrupted.")
    print(f"        Refusing to write process conclusions without full audit evidence.")
    sys.exit(4)
    return missing_str


def _invalidate_section_confirmations(state, patch):
    item_ids_in_patch = {e.get("id") for e in patch["items"] if e.get("id")}
    mig_ids_in_patch = {e.get("id") for e in patch.get("migration_reminders", []) if e.get("id")}
    ki_ids_in_patch = {e.get("id") for e in patch.get("known_issues", []) if e.get("id")}

    invalidated = []

    if item_ids_in_patch and state.get("confirmations", {}).get("changes"):
        state["confirmations"]["changes"] = False
        invalidated.append("changes")
    if mig_ids_in_patch and state.get("confirmations", {}).get("migration"):
        state["confirmations"]["migration"] = False
        for mid in mig_ids_in_patch:
            if mid in state.get("migration_processed", {}):
                state["migration_processed"][mid] = False
        invalidated.append("migration")
    if ki_ids_in_patch and state.get("confirmations", {}).get("known_issues"):
        state["confirmations"]["known_issues"] = False
        state["known_issues_reviewed"] = False
        invalidated.append("known_issues")

    return invalidated


def _apply_patch_entry(state, patch_entry, target_type, operator):
    if target_type == "item":
        target = _find_in_list(state["items"], patch_entry["id"])
    elif target_type == "migration":
        target = _find_in_list(state["migration_reminders"], patch_entry["id"])
    elif target_type == "known_issue":
        target = _find_in_list(state["known_issues"], patch_entry["id"])
    else:
        return None

    if target is None:
        return None

    _ensure_item_versioning(target, operator=operator)
    applied_fields = {}
    for k, v in patch_entry.items():
        if k in {"id", "_version", "_last_modified_at", "_last_modified_by"}:
            continue
        target[k] = v
        applied_fields[k] = v

    _bump_item_version(target, operator=operator)
    return applied_fields


def _build_default_resolutions(conflicts, mode):
    for c in conflicts:
        if c.get("resolution") is not None:
            continue
        if mode == "abort":
            c["resolution"] = "abort"
        elif mode == "skip":
            c["resolution"] = "skip"
        elif mode == "overwrite":
            c["resolution"] = "overwrite"
    return conflicts


def _persist_pending_bulk(state, patch, conflicts, mode, operator, reason):
    pending = {
        "patch_id": patch["patch_id"],
        "patch_snapshot": copy.deepcopy(patch),
        "conflicts_snapshot": copy.deepcopy(conflicts),
        "mode": mode,
        "operator": operator,
        "reason": reason,
        "created_at": datetime.now().isoformat(),
        "resolved": False,
    }
    state.setdefault("pending_bulk_ops", []).append(pending)
    return pending


def _serialize_decisions(decisions_dict):
    return {f"{k[0]}::{k[1]}": list(v) if isinstance(v, tuple) else v
            for k, v in decisions_dict.items()}


def _deserialize_decisions(serialized):
    result = {}
    for k, v in serialized.items():
        if "::" in k:
            tt, iid = k.split("::", 1)
            result[(tt, iid)] = tuple(v) if isinstance(v, list) else v
    return result


def _resolve_pending(state, pending_idx, final_decisions, operator):
    ops = state.get("pending_bulk_ops", [])
    if pending_idx < 0 or pending_idx >= len(ops):
        return None
    pending = ops[pending_idx]
    pending["final_decisions"] = _serialize_decisions(final_decisions)
    pending["resolved_at"] = datetime.now().isoformat()
    pending["resolved_operator"] = operator
    pending["resolved"] = True
    return pending


def cmd_bulk_amend(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    if getattr(args, "resume", None) is not None:
        return _cmd_bulk_resume(args, rules, state)

    patch_path = args.patch
    patch = _load_patch_file(patch_path)
    operator = args.operator or patch.get("operator", "unknown")
    reason = args.reason or patch.get("reason", "")
    mode = args.mode or "interactive"

    patch["operator"] = operator

    val_errors = _validate_patch_entries(patch, rules, state)
    if val_errors:
        print("[REJECTED] Patch validation failed:")
        for e in val_errors:
            print(e)
        _audit(state, "bulk_amend_rejected",
               f"patch={patch['patch_id']} reason=validation errors={val_errors}")
        save_state(state, state_path)
        sys.exit(1)

    conflicts = _detect_conflicts(state, patch)

    hard_conflicts = [c for c in conflicts if c.get("resolution") is None and c["type"] != "no_change"]
    no_changes = [c for c in conflicts if c.get("resolution") == "skip" and c["type"] == "no_change"]
    not_founds = [c for c in conflicts if c["type"] == "not_found"]

    print(f"\n== Bulk Amend: patch '{patch['patch_id']}' by '{operator}' ==")
    print(f"   Items: {len(patch['items'])}, Migrations: {len(patch.get('migration_reminders',[]))}, Known issues: {len(patch.get('known_issues',[]))}")
    if reason:
        print(f"   Reason: {reason}")
    print()

    total_patch = len(patch["items"]) + len(patch.get("migration_reminders", [])) + len(patch.get("known_issues", []))
    print(f"Total patch entries: {total_patch}")
    print(f"  - Conflicts requiring decision: {len(hard_conflicts)}")
    print(f"  - No-op (identical values, auto-skip): {len(no_changes)}")
    print(f"  - Not found (will be skipped): {len(not_founds)}")
    print()

    if not_founds:
        print("[WARN] Entries not found in state (will skip):")
        for c in not_founds:
            print(f"  - [{c['target_type']}] {c['id']}")
        print()

    if no_changes:
        print("[INFO] No-change entries (auto-skipped):")
        for c in no_changes:
            print(f"  - [{c['target_type']}] {c['id']}: values already match")
        print()

    if hard_conflicts:
        print("[CONFLICT] The following entries require a decision:")
        for i, c in enumerate(hard_conflicts):
            print(f"  [{i+1}] [{c['type']}] [{c['target_type']}] {c['id']}: {c['message']}")
            if c.get("diff"):
                print(_format_diff(c["diff"]))
        print()

    if mode == "interactive" and hard_conflicts:
        _persist_pending_bulk(state, patch, conflicts, mode, operator, reason)
        pending_idx = len(state["pending_bulk_ops"]) - 1
        save_state(state, state_path)
        print(f"Conflict info persisted. Run this command to resume:")
        print(f"  release_cli.py bulk_amend --resume {pending_idx} --decision abort|skip|overwrite [--per-item id=dec,id=dec]")
        print()
        print("Quick non-interactive modes (no resume needed):")
        print(f"  release_cli.py bulk_amend {patch_path} --mode abort        # entire batch fails on any conflict")
        print(f"  release_cli.py bulk_amend {patch_path} --mode skip         # skip only conflicted entries, apply the rest")
        print(f"  release_cli.py bulk_amend {patch_path} --mode overwrite    # force-overwrite all conflicts")
        sys.exit(2)

    decisions_map = {}
    if mode == "abort":
        if hard_conflicts:
            print(f"[ABORT] Mode=abort: {len(hard_conflicts)} conflict(s) found. Entire batch rolled back.")
            _audit(state, "bulk_amend_aborted",
                   f"patch={patch['patch_id']} operator={operator} mode=abort conflicts={len(hard_conflicts)} reason={reason}")
            save_state(state, state_path)
            sys.exit(3)
        for c in conflicts:
            decisions_map[(c["target_type"], c["id"])] = c["type"]
    elif mode == "skip":
        for c in conflicts:
            if c.get("resolution") == "skip" or c["type"] in ("no_change", "not_found"):
                decisions_map[(c["target_type"], c["id"])] = "skip"
            else:
                decisions_map[(c["target_type"], c["id"])] = "skip"
    elif mode == "overwrite":
        for c in conflicts:
            if c["type"] == "not_found":
                decisions_map[(c["target_type"], c["id"])] = "skip"
            elif c.get("resolution") == "skip":
                decisions_map[(c["target_type"], c["id"])] = "skip"
            else:
                decisions_map[(c["target_type"], c["id"])] = "overwrite"

    _enforce_decision_coverage(
        patch, conflicts, decisions_map, strict=False,
        operator=operator, patch_id=patch["patch_id"],
    )

    applied_count = 0
    skipped_count = 0
    per_item_results = []

    for entry in patch["items"]:
        key = ("item", entry.get("id"))
        decision = decisions_map.get(key, "apply")
        if decision in ("skip",) or entry.get("id") is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "item", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "item", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "item", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((entry.get("id"), "item", decision, str(fields)))

    for entry in patch.get("migration_reminders", []):
        key = ("migration", entry.get("id"))
        decision = decisions_map.get(key, "apply")
        if decision in ("skip",) or entry.get("id") is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "migration", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "migration", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "migration", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((entry.get("id"), "migration", decision, str(fields)))

    for entry in patch.get("known_issues", []):
        key = ("known_issue", entry.get("id"))
        decision = decisions_map.get(key, "apply")
        if decision in ("skip",) or entry.get("id") is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "known_issue", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "known_issue", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((entry.get("id"), "known_issue", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((entry.get("id"), "known_issue", decision, str(fields)))

    invalidated = _invalidate_section_confirmations(state, patch)

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print("  [NOTE] Approval cleared (bulk amend changed data).")

    _audit(state, "bulk_amend_applied",
           f"patch={patch['patch_id']} operator={operator} mode={mode} "
           f"applied={applied_count} skipped={skipped_count} "
           f"invalidated_sections={invalidated} "
           f"per_item={per_item_results} reason={reason}")

    save_state(state, state_path)

    print(f"[OK] Bulk amend complete.")
    print(f"  Applied: {applied_count}")
    print(f"  Skipped: {skipped_count}")
    if invalidated:
        print(f"  Sections invalidated (re-confirm required): {invalidated}")


def _cmd_bulk_resume(args, rules, state):
    state_path = args.state
    pending_idx = args.resume
    ops = state.get("pending_bulk_ops", [])

    if pending_idx < 0 or pending_idx >= len(ops):
        print(f"[ERROR] Pending bulk op index {pending_idx} out of range (have {len(ops)} entries)")
        sys.exit(1)

    pending = ops[pending_idx]
    if pending.get("resolved"):
        print(f"[ERROR] Pending bulk op #{pending_idx} already resolved at {pending.get('resolved_at')}")
        sys.exit(1)

    patch = pending["patch_snapshot"]
    conflicts = pending["conflicts_snapshot"]
    operator = args.operator or pending.get("operator", "unknown")
    reason = args.reason or pending.get("reason", "")
    default_decision = args.decision

    per_item_override = {}
    if getattr(args, "per_item", None):
        for piece in args.per_item.split(","):
            if "=" in piece:
                k, v = piece.split("=", 1)
                per_item_override[k.strip()] = v.strip()

    print(f"\n== Resuming bulk amend #{pending_idx}: patch '{patch['patch_id']}' ==")
    print(f"   Created at: {pending.get('created_at')}")
    print(f"   Original operator: {pending.get('operator')}")
    print()

    final_decisions = {}
    hard_conflicts = [c for c in conflicts if c.get("resolution") is None and c["type"] != "no_change"]

    for c in conflicts:
        key = (c["target_type"], c["id"])
        override = per_item_override.get(c["id"])
        if c["type"] == "not_found":
            final_decisions[key] = ("skip", "not_found")
        elif c["type"] == "no_change":
            final_decisions[key] = ("skip", "no_change")
        elif override:
            if override not in ("skip", "overwrite", "abort"):
                print(f"[ERROR] Invalid per-item decision '{override}' for {c['id']}")
                sys.exit(1)
            final_decisions[key] = (override, f"per-item override by {operator}")
        elif default_decision:
            if default_decision not in ("skip", "overwrite", "abort"):
                print(f"[ERROR] Invalid default decision '{default_decision}'")
                sys.exit(1)
            final_decisions[key] = (default_decision, f"default={default_decision} by {operator}")
        else:
            print(f"[ERROR] No decision for conflict [{c['type']}] {c['target_type']}:{c['id']}")
            print(f"        Use --decision skip|overwrite|abort and/or --per-item id=dec,id=dec")
            sys.exit(1)

    _enforce_decision_coverage(
        patch, conflicts, final_decisions, strict=True,
        operator=operator, patch_id=patch["patch_id"],
    )

    abort_all = any(d[0] == "abort" for d in final_decisions.values())
    if abort_all:
        print(f"[ABORT] At least one conflict resolved as 'abort'. Entire batch cancelled.")
        _audit(state, "bulk_amend_aborted",
               f"patch={patch['patch_id']} operator={operator} resumed=true decisions={final_decisions} reason={reason}")
        _resolve_pending(state, pending_idx, final_decisions, operator)
        save_state(state, state_path)
        sys.exit(3)

    applied_count = 0
    skipped_count = 0
    per_item_results = []

    for entry in patch["items"]:
        iid = entry.get("id")
        key = ("item", iid)
        decision_info = final_decisions.get(key)
        if decision_info is None:
            decision = "apply"
        else:
            decision, _ = decision_info
        if decision == "skip" or iid is None:
            skipped_count += 1
            per_item_results.append((iid, "item", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "item", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((iid, "item", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((iid, "item", decision, str(fields)))

    for entry in patch.get("migration_reminders", []):
        mid = entry.get("id")
        key = ("migration", mid)
        decision_info = final_decisions.get(key)
        if decision_info is None:
            decision = "apply"
        else:
            decision, _ = decision_info
        if decision == "skip" or mid is None:
            skipped_count += 1
            per_item_results.append((mid, "migration", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "migration", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((mid, "migration", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((mid, "migration", decision, str(fields)))

    for entry in patch.get("known_issues", []):
        kid = entry.get("id")
        key = ("known_issue", kid)
        decision_info = final_decisions.get(key)
        if decision_info is None:
            decision = "apply"
        else:
            decision, _ = decision_info
        if decision == "skip" or kid is None:
            skipped_count += 1
            per_item_results.append((kid, "known_issue", "skipped", ""))
            continue
        fields = _apply_patch_entry(state, entry, "known_issue", operator)
        if fields is None:
            skipped_count += 1
            per_item_results.append((kid, "known_issue", "not_found", ""))
        else:
            applied_count += 1
            per_item_results.append((kid, "known_issue", decision, str(fields)))

    invalidated = _invalidate_section_confirmations(state, patch)

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print("  [NOTE] Approval cleared (bulk amend changed data).")

    _resolve_pending(state, pending_idx, final_decisions, operator)
    _audit(state, "bulk_amend_applied",
           f"patch={patch['patch_id']} operator={operator} resumed=true "
           f"applied={applied_count} skipped={skipped_count} "
           f"invalidated_sections={invalidated} "
           f"per_item={per_item_results} reason={reason}")

    save_state(state, state_path)

    print(f"[OK] Bulk amend (resumed) complete.")
    print(f"  Applied: {applied_count}")
    print(f"  Skipped: {skipped_count}")
    if invalidated:
        print(f"  Sections invalidated (re-confirm required): {invalidated}")


def cmd_rules_upgrade_check(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    _ensure_rules_snapshot(state, args.rules)

    old_rules = state.get("rules_snapshot") or {}
    new_rules = rules

    rules_diff = _compare_rules_configs(old_rules, new_rules)

    if not rules_diff["has_changes"]:
        print("\n" + "=" * 70)
        print("RULES UPGRADE CHECK")
        print("=" * 70)
        print()
        print("  Local rules are IDENTICAL to state rules snapshot.")
        print("  No upgrade needed.")
        print()
        print(f"  Rules version (state): {state.get('rules_version', 'N/A')[:16]}...")
        print("=" * 70)
        print()
        return

    impact = _assess_rules_upgrade_impact(state, old_rules, new_rules, rules_diff)

    current_pid = os.getpid()
    existing_pending = state.get("pending_rules_upgrade")
    resumed = False

    if existing_pending and existing_pending.get("check_pid") is not None and \
       existing_pending["check_pid"] != current_pid and \
       not existing_pending.get("resumed_across_restart"):
        audit_log = state.get("audit_log", [])
        checked_at = existing_pending.get("checked_at", "")
        has_subsequent_work = False
        for event in audit_log:
            if event.get("timestamp", "") > checked_at and \
               event.get("action") not in ("rules_upgrade_checked", "rules_upgrade_skipped"):
                has_subsequent_work = True
                break
        if has_subsequent_work:
            existing_pending["resumed_across_restart"] = True
            existing_pending["resumed_count"] = existing_pending.get("resumed_count", 0) + 1
            resumed = True
            _audit(state, "pending_rules_upgrade_resumed",
                   f"upgrade_id={existing_pending.get('upgrade_id')} "
                   f"old_pid={existing_pending.get('check_pid')} current_pid={current_pid}")

    upgrade_id = hashlib.sha256(
        f"{datetime.now().isoformat()}{_compute_rules_checksum(old_rules)}{_compute_rules_checksum(new_rules)}".encode()
    ).hexdigest()[:16]

    pending = {
        "upgrade_id": upgrade_id,
        "checked_at": datetime.now().isoformat(),
        "checked_by": getattr(args, "operator", None) or _get_identity(),
        "check_pid": current_pid,
        "old_rules_snapshot": copy.deepcopy(old_rules),
        "new_rules_snapshot": copy.deepcopy(new_rules),
        "old_rules_version": state.get("rules_version"),
        "new_rules_version": _compute_rules_checksum(new_rules),
        "rules_diff": rules_diff,
        "impact": impact,
        "status": "pending_confirmation",
        "decisions": {},
        "decisions_log": [],
        "resumed_across_restart": False,
    }

    state["pending_rules_upgrade"] = pending
    _audit(state, "rules_upgrade_checked",
           f"upgrade_id={upgrade_id} "
           f"auto_migratable={impact['summary']['auto_migratable_count']} "
           f"conf_rollback={impact['summary']['confirmation_rollback_count']} "
           f"manual_decision={impact['summary']['manual_decision_count']}")
    save_state(state, state_path)

    print("\n" + "=" * 70)
    print("RULES UPGRADE CHECK - IMPACT ANALYSIS")
    print("=" * 70)
    print()
    print(f"  Upgrade ID:        {upgrade_id}")
    print(f"  Checked at:        {pending['checked_at']}")
    print(f"  Old rules version: {state.get('rules_version', 'N/A')[:16]}...")
    print(f"  New rules version: {_compute_rules_checksum(new_rules)[:16]}...")
    print()

    print("[Rules Configuration Changes]")
    print()
    print(f"  Required sections:  +{len(rules_diff['required_sections']['added'])} "
          f"-{len(rules_diff['required_sections']['removed'])}")
    if rules_diff['required_sections']['added']:
        print(f"    + Added:   {', '.join(rules_diff['required_sections']['added'])}")
    if rules_diff['required_sections']['removed']:
        print(f"    - Removed: {', '.join(rules_diff['required_sections']['removed'])}")

    print(f"  Valid risk levels:  +{len(rules_diff['valid_risk_levels']['added'])} "
          f"-{len(rules_diff['valid_risk_levels']['removed'])}")
    if rules_diff['valid_risk_levels']['added']:
        print(f"    + Added:   {', '.join(rules_diff['valid_risk_levels']['added'])}")
    if rules_diff['valid_risk_levels']['removed']:
        print(f"    - Removed: {', '.join(rules_diff['valid_risk_levels']['removed'])}")

    print(f"  Required fields:    +{len(rules_diff['required_fields_per_item']['added'])} "
          f"-{len(rules_diff['required_fields_per_item']['removed'])}")
    if rules_diff['required_fields_per_item']['added']:
        print(f"    + Added:   {', '.join(rules_diff['required_fields_per_item']['added'])}")
    if rules_diff['required_fields_per_item']['removed']:
        print(f"    - Removed: {', '.join(rules_diff['required_fields_per_item']['removed'])}")

    print(f"  Categories:         +{len(rules_diff['categories']['added'])} "
          f"-{len(rules_diff['categories']['removed'])}")
    if rules_diff['categories']['added']:
        print(f"    + Added:   {', '.join(rules_diff['categories']['added'])}")
    if rules_diff['categories']['removed']:
        print(f"    - Removed: {', '.join(rules_diff['categories']['removed'])}")

    print()
    print("[Impact on Current State]")
    print()
    s = impact["summary"]
    print(f"  Total affected items:    {s['total_affected_items']}")
    print(f"  Auto-migratable:         {s['auto_migratable_count']}")
    print(f"  Confirmation rollbacks:  {s['confirmation_rollback_count']}")
    print(f"  Manual decisions needed: {s['manual_decision_count']}")
    print()

    if impact["auto_migratable"]:
        print("[Auto-Migratable Changes]")
        for m in impact["auto_migratable"]:
            print(f"  - {m['message']}")
        print()

    if impact["confirmation_rollback_needed"]:
        print("[Confirmation Rollbacks Needed]")
        for m in impact["confirmation_rollback_needed"]:
            print(f"  - {m['message']} (action: {m['rollback_action']})")
        print()

    if impact["manual_decision_required"]:
        print("[Manual Decisions Required]")
        for i, m in enumerate(impact["manual_decision_required"], 1):
            print(f"  {i}. [{m['type']}] {m['message']}")
            if m.get("valid_values"):
                print(f"     Valid values: {', '.join(m['valid_values'])}")
        print()

    has_manual = len(impact["manual_decision_required"]) > 0

    print("=" * 70)
    if has_manual:
        print("STATUS: PENDING - Manual decisions required")
        print("  Use --decision and/or --per-item to resolve conflicts during apply.")
    else:
        print("STATUS: READY - All changes can be auto-migrated")
    print()
    print("Next steps:")
    print(f"  Apply upgrade:  release_cli.py --state {state_path} rules_upgrade_apply"
          f"{' --decision set=high' if has_manual else ''}")
    print(f"  Skip upgrade:   release_cli.py --state {state_path} rules_upgrade_skip")
    print(f"  Undo upgrade:   release_cli.py --state {state_path} rules_upgrade_undo (after apply)")
    print("=" * 70)
    print()


def cmd_rules_upgrade_apply(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    pending = state.get("pending_rules_upgrade")
    if pending is None:
        print("[ERROR] No pending rules upgrade found.")
        print("  Run 'rules_upgrade_check' first to detect and preview changes.")
        sys.exit(1)

    if pending.get("status") != "pending_confirmation":
        print(f"[ERROR] Upgrade status is '{pending.get('status')}', not pending_confirmation.")
        sys.exit(1)

    operator = getattr(args, "operator", None) or _get_identity()
    mode = getattr(args, "mode", "auto")
    default_decision = getattr(args, "decision", None)
    per_item_raw = getattr(args, "per_item", None)

    current_pid = os.getpid()
    if pending.get("check_pid") is not None and pending["check_pid"] != current_pid and \
       not pending.get("resumed_across_restart"):
        audit_log = state.get("audit_log", [])
        checked_at = pending.get("checked_at", "")
        has_subsequent_work = False
        for event in audit_log:
            if event.get("timestamp", "") > checked_at and \
               event.get("action") not in ("rules_upgrade_checked", "rules_upgrade_skipped"):
                has_subsequent_work = True
                break
        if has_subsequent_work:
            pending["resumed_across_restart"] = True
            pending["resumed_count"] = pending.get("resumed_count", 0) + 1
            _audit(state, "pending_rules_upgrade_resumed",
                   f"upgrade_id={pending.get('upgrade_id')} old_pid={pending.get('check_pid')} current_pid={current_pid}")

    impact = pending["impact"]
    old_rules = pending["old_rules_snapshot"]
    new_rules = pending["new_rules_snapshot"]
    upgrade_id = pending["upgrade_id"]

    decisions = {}
    decisions_log = []
    ts_now = datetime.now().isoformat()

    if impact["confirmation_rollback_needed"]:
        for m in impact["confirmation_rollback_needed"]:
            sec = m["section"]
            key = f"section_added:{sec}"
            decisions[key] = "unconfirm"
            decisions_log.append({
                "ts": ts_now,
                "type": "section_added",
                "section": sec,
                "decision": "unconfirm",
                "operator": operator,
                "detail": f"section {sec} unconfirmed due to new required section",
            })

    manual_items = impact["manual_decision_required"]
    per_item_decision = {}
    if per_item_raw:
        for pair in per_item_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                per_item_decision[k.strip()] = v.strip()

    if manual_items:
        for m in manual_items:
            iid = m["item_id"]
            field = m["field"]
            key = f"{m['type']}:{iid}:{field}"
            pi_key = f"{iid}.{field}"
            dec = None

            if pi_key in per_item_decision:
                val = per_item_decision[pi_key]
                dec = {"action": "set", "value": val}
                decisions_log.append({
                    "ts": ts_now,
                    "type": m["type"],
                    "item_id": iid,
                    "field": field,
                    "decision": f"set={val}",
                    "operator": operator,
                    "detail": f"per-item: {iid}.{field} = {val}",
                })
            elif default_decision and "=" in default_decision:
                act, val = default_decision.split("=", 1)
                if act == "set":
                    if m["type"] == "invalid_risk_level" and val == "first_valid":
                        valid_vals = m.get("valid_values", [])
                        if valid_vals:
                            dec = {"action": "set", "value": valid_vals[0]}
                            decisions_log.append({
                                "ts": ts_now,
                                "type": m["type"],
                                "item_id": iid,
                                "field": field,
                                "decision": f"set_first_valid={valid_vals[0]}",
                                "operator": operator,
                                "detail": f"default: first valid value for {iid}.{field}",
                            })
                    else:
                        dec = {"action": "set", "value": val}
                        decisions_log.append({
                            "ts": ts_now,
                            "type": m["type"],
                            "item_id": iid,
                            "field": field,
                            "decision": f"set={val}",
                            "operator": operator,
                            "detail": f"default: {iid}.{field} = {val}",
                        })
            elif m["type"] == "missing_required_field":
                dec = None
            elif default_decision == "skip":
                dec = {"action": "skip"}
                decisions_log.append({
                    "ts": ts_now,
                    "type": m["type"],
                    "item_id": iid,
                    "field": field,
                    "decision": "skip",
                    "operator": operator,
                    "detail": f"skip {iid}.{field} (manual decision needed later)",
                })

            if dec is not None:
                decisions[key] = dec

        unresolved = [
            m for m in manual_items
            if f"{m['type']}:{m['item_id']}:{m['field']}" not in decisions
        ]
        if unresolved and mode != "force":
            print("[BLOCKED] Unresolved manual decisions:")
            for m in unresolved:
                print(f"  - [{m['type']}] {m['item_id']}.{m['field']}: {m['message']}")
            print()
            print("  Use --decision set=<value> or --per-item id.field=value,... to resolve.")
            print("  Use --mode force to apply anyway (unresolved items will remain invalid).")
            sys.exit(1)

    pre_upgrade_state = copy.deepcopy(state)
    _apply_rules_upgrade_to_state(state, old_rules, new_rules, impact, decisions, operator)

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print("  [NOTE] Approval cleared (rules changed after approval).")

    upgrade_record = {
        "upgrade_id": upgrade_id,
        "applied_at": datetime.now().isoformat(),
        "applied_by": operator,
        "old_rules_snapshot": copy.deepcopy(old_rules),
        "new_rules_snapshot": copy.deepcopy(new_rules),
        "old_rules_version": pending["old_rules_version"],
        "new_rules_version": pending["new_rules_version"],
        "rules_diff": pending["rules_diff"],
        "impact": impact,
        "decisions": decisions,
        "decisions_log": decisions_log,
        "pre_upgrade_state_snapshot": {
            "confirmations": copy.deepcopy(pre_upgrade_state.get("confirmations", {})),
            "items": copy.deepcopy(pre_upgrade_state.get("items", [])),
            "approved": pre_upgrade_state.get("approved", False),
            "approved_at_version": pre_upgrade_state.get("approved_at_version"),
            "approved_at_draft_version": pre_upgrade_state.get("approved_at_draft_version"),
            "rules_version": pre_upgrade_state.get("rules_version"),
            "rules_snapshot": copy.deepcopy(pre_upgrade_state.get("rules_snapshot")),
        },
        "status": "applied",
        "revoked": False,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
    }

    state.setdefault("rules_upgrade_history", []).append(upgrade_record)
    state["pending_rules_upgrade"] = None

    _audit(state, "rules_upgrade_applied",
           f"upgrade_id={upgrade_id} operator={operator} "
           f"auto_migratable={impact['summary']['auto_migratable_count']} "
           f"conf_rollback={impact['summary']['confirmation_rollback_count']} "
           f"manual_decisions_resolved={len(decisions_log)}")

    save_state(state, state_path)

    print("\n" + "=" * 70)
    print("RULES UPGRADE APPLIED")
    print("=" * 70)
    print()
    print(f"  Upgrade ID:        {upgrade_id}")
    print(f"  Applied at:        {upgrade_record['applied_at']}")
    print(f"  Applied by:        {operator}")
    if pending.get("resumed_across_restart"):
        print(f"  Resumed:           Yes (across restart)")
    print(f"  Rules version:     {pending['old_rules_version'][:16]}... -> {pending['new_rules_version'][:16]}...")
    print()
    print(f"  Auto-migrated:     {impact['summary']['auto_migratable_count']}")
    print(f"  Conf rollbacks:    {impact['summary']['confirmation_rollback_count']}")
    print(f"  Manual decisions:  {len(decisions_log)} applied")
    print()
    print("  Entry added to rules_upgrade_history.")
    print("  Cross-restart safe.")
    print()
    print("Next steps:")
    print(f"  Review status:     release_cli.py --state {state_path} status")
    print(f"  Undo upgrade:      release_cli.py --state {state_path} rules_upgrade_undo")
    print("=" * 70)
    print()


def cmd_rules_upgrade_skip(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    pending = state.get("pending_rules_upgrade")
    if pending is None:
        print("[INFO] No pending rules upgrade to skip.")
        return

    operator = getattr(args, "operator", None) or _get_identity()
    reason = getattr(args, "reason", "") or "manual skip"

    upgrade_id = pending.get("upgrade_id", "?")
    _audit(state, "rules_upgrade_skipped",
           f"upgrade_id={upgrade_id} operator={operator} reason={reason}")

    state["pending_rules_upgrade"] = None
    save_state(state, state_path)

    print(f"[OK] Pending rules upgrade {upgrade_id} skipped.")
    print(f"  Reason: {reason}")
    print()


def cmd_rules_upgrade_undo(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("rules_upgrade_history", [])
    history = state["rules_upgrade_history"]

    upgrade_id = getattr(args, "upgrade_id", None)
    reason = getattr(args, "reason", "") or "manual undo"
    operator = getattr(args, "operator", None) or _get_identity()

    target_record = None
    target_idx = None

    if upgrade_id:
        for i, rec in enumerate(history):
            if rec.get("upgrade_id") == upgrade_id:
                target_record = rec
                target_idx = i
                break
    elif history:
        for i in range(len(history) - 1, -1, -1):
            if not history[i].get("revoked"):
                target_record = history[i]
                target_idx = i
                break

    if target_record is None:
        print("[ERROR] No applied rules upgrade found to undo.")
        if history:
            print(f"  Total history entries: {len(history)}")
            revoked = [r for r in history if r.get("revoked")]
            print(f"  Already revoked: {len(revoked)}")
        sys.exit(1)

    if target_record.get("revoked"):
        print(f"[ERROR] Upgrade {target_record.get('upgrade_id')} is already revoked.")
        sys.exit(1)

    pre_snapshot = target_record.get("pre_upgrade_state_snapshot", {})

    if pre_snapshot.get("confirmations") is not None:
        state["confirmations"] = copy.deepcopy(pre_snapshot["confirmations"])
    if pre_snapshot.get("items") is not None:
        state["items"] = copy.deepcopy(pre_snapshot["items"])
    state["approved"] = pre_snapshot.get("approved", False)
    state["approved_at_version"] = pre_snapshot.get("approved_at_version")
    state["approved_at_draft_version"] = pre_snapshot.get("approved_at_draft_version")
    state["rules_version"] = pre_snapshot.get("rules_version")
    state["rules_snapshot"] = copy.deepcopy(pre_snapshot.get("rules_snapshot", {}))

    ts_now = datetime.now().isoformat()
    target_record["revoked"] = True
    target_record["revoked_at"] = ts_now
    target_record["revoked_by"] = operator
    target_record["revoke_reason"] = reason

    _audit(state, "rules_upgrade_undone",
           f"upgrade_id={target_record['upgrade_id']} operator={operator} reason={reason}")

    save_state(state, state_path)

    print("\n" + "=" * 70)
    print("RULES UPGRADE UNDONE")
    print("=" * 70)
    print()
    print(f"  Upgrade ID:    {target_record['upgrade_id']}")
    print(f"  Undone at:     {ts_now}")
    print(f"  Undone by:     {operator}")
    print(f"  Reason:        {reason}")
    print()
    print(f"  Rules restored to: {pre_snapshot.get('rules_version', 'N/A')[:16]}...")
    print(f"  Confirmations restored: {len(pre_snapshot.get('confirmations', {}))} sections")
    print(f"  Items restored: {len(pre_snapshot.get('items', []))} items")
    print(f"  Approval status: {pre_snapshot.get('approved', False)}")
    print()
    print("  Undo record preserved in rules_upgrade_history.")
    print("=" * 70)
    print()


def cmd_rules_history(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[INFO] No state found.")
        return

    state.setdefault("rules_upgrade_history", [])
    history = state["rules_upgrade_history"]

    if not history:
        print("[INFO] No rules upgrade history.")
        print("  Run 'rules_upgrade_check' to detect changes.")
        return

    pending = state.get("pending_rules_upgrade")

    print("\n" + "=" * 70)
    print("RULES UPGRADE HISTORY")
    print("=" * 70)
    print()

    if pending:
        print(f"[PENDING] {pending.get('upgrade_id')}")
        print(f"  Checked at: {pending.get('checked_at')}")
        print(f"  Checked by: {pending.get('checked_by')}")
        print(f"  Status:     {pending.get('status')}")
        if pending.get("resumed_across_restart"):
            print(f"  Resumed:    Yes (cross-restart)")
        print()

    for idx, rec in enumerate(reversed(history), 1):
        status_tag = " [REVOKED]" if rec.get("revoked") else ""
        print(f"Upgrade #{len(history) - idx + 1}: {rec.get('upgrade_id')}{status_tag}")
        print(f"  Applied at: {rec.get('applied_at')}")
        print(f"  Applied by: {rec.get('applied_by')}")
        print(f"  Old ver:    {str(rec.get('old_rules_version', ''))[:16]}...")
        print(f"  New ver:    {str(rec.get('new_rules_version', ''))[:16]}...")
        impact = rec.get("impact", {}).get("summary", {})
        print(f"  Impact:     auto={impact.get('auto_migratable_count', 0)} "
              f"rollback={impact.get('confirmation_rollback_count', 0)} "
              f"manual={impact.get('manual_decision_count', 0)}")
        if rec.get("revoked"):
            print(f"  Revoked at: {rec.get('revoked_at')}")
            print(f"  Revoked by: {rec.get('revoked_by')}")
            print(f"  Reason:     {rec.get('revoke_reason', '')}")
        print()

    print(f"Total upgrades: {len(history)} "
          f"(active: {len([r for r in history if not r.get('revoked')])}, "
          f"revoked: {len([r for r in history if r.get('revoked')])})")
    print("=" * 70)
    print()


def _compute_rules_upgrade_package_checksum(package):
    relevant = {
        "package_format_version": package.get("package_format_version"),
        "exported_at": package.get("exported_at"),
        "upgrade_id": package.get("upgrade_id"),
        "old_rules_version": package.get("old_rules_version"),
        "new_rules_version": package.get("new_rules_version"),
        "decisions_log": package.get("decisions_log", []),
    }
    serialized = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_rules_upgrade_handover_package(state, upgrade_source, operator, rules_path, description, note):
    rules_copy = None
    if rules_path and os.path.exists(rules_path):
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_copy = f.read()

    if upgrade_source == "pending":
        pending = state.get("pending_rules_upgrade")
        if not pending:
            print("[ERROR] No pending rules upgrade found.")
            return None
        upgrade_id = pending["upgrade_id"]
        upgrade_data = pending
        source_type = "pending"
    else:
        history = state.get("rules_upgrade_history", [])
        target = None
        for rec in reversed(history):
            if rec.get("upgrade_id") == upgrade_source or (upgrade_source == "latest" and not rec.get("revoked")):
                target = rec
                break
        if target is None:
            print(f"[ERROR] Rules upgrade '{upgrade_source}' not found in history.")
            return None
        upgrade_id = target["upgrade_id"]
        upgrade_data = target
        source_type = "applied"

    package = {
        "package_format_version": PACKAGE_FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "exported_by": operator or _get_identity(),
        "description": description or "",
        "note": note or "",
        "upgrade_id": upgrade_id,
        "source_type": source_type,
        "source_upgrade_snapshot": copy.deepcopy(upgrade_data),
        "old_rules_version": upgrade_data.get("old_rules_version"),
        "new_rules_version": upgrade_data.get("new_rules_version"),
        "rules_diff": upgrade_data.get("rules_diff"),
        "impact": upgrade_data.get("impact"),
        "decisions": upgrade_data.get("decisions", {}),
        "decisions_log": upgrade_data.get("decisions_log", []),
        "pre_upgrade_items_snapshot": copy.deepcopy(
            upgrade_data.get("pre_upgrade_state_snapshot", {}).get("items")
            if source_type == "applied" and upgrade_data.get("pre_upgrade_state_snapshot", {}).get("items")
            else state.get("items", [])
        ),
        "current_rules_snapshot": rules_copy,
        "state_rules_version_at_export": state.get("rules_version"),
        "package_checksum": None,
        "metadata": {
            "exported_from_state_version": state.get("version"),
            "exported_from_draft_version": state.get("draft_version"),
            "exported_from_approved": state.get("approved"),
            "rules_upgrade_history_count": len(state.get("rules_upgrade_history", [])),
            "revoked_in_source": upgrade_data.get("revoked", False),
            "has_pending_rules_upgrade": state.get("pending_rules_upgrade") is not None,
        }
    }

    package["package_checksum"] = _compute_rules_upgrade_package_checksum(package)
    return package


def _validate_rules_upgrade_handover_package(package):
    if not isinstance(package, dict):
        return False, "Package is not a valid JSON object"
    if "package_format_version" not in package:
        return False, "Missing package_format_version"
    if package["package_format_version"] != PACKAGE_FORMAT_VERSION:
        return False, (f"Incompatible package format version: {package['package_format_version']} "
                      f"(expected {PACKAGE_FORMAT_VERSION})")
    if "upgrade_id" not in package:
        return False, "Package missing 'upgrade_id' field"
    if "source_upgrade_snapshot" not in package:
        return False, "Package missing 'source_upgrade_snapshot' field"
    if "package_checksum" not in package:
        return False, "Package missing 'package_checksum' field"

    temp_pkg = copy.deepcopy(package)
    temp_pkg["package_checksum"] = None
    expected = _compute_rules_upgrade_package_checksum(temp_pkg)
    if expected != package["package_checksum"]:
        return False, "Package checksum mismatch: package may be corrupted"

    return True, "Package valid"


def _detect_rules_upgrade_handover_conflicts(local_state, package, rules_path, existing_import_info=None):
    conflicts = []

    if existing_import_info:
        if existing_import_info.get("package_checksum") != package.get("package_checksum"):
            conflicts.append({
                "type": "package_checksum_changed",
                "severity": "high",
                "message": ("Package content has changed since last reconcile "
                           "(different package_checksum). Re-export from source recommended."),
                "resolution_options": ["skip", "reimport", "override"],
            })
        if existing_import_info.get("exported_at") != package.get("exported_at"):
            conflicts.append({
                "type": "package_re_exported",
                "severity": "high",
                "message": ("Package has been re-exported at a different time. "
                           f"Old: {existing_import_info.get('exported_at')} "
                           f"New: {package.get('exported_at')}"),
                "resolution_options": ["skip", "reimport", "override"],
            })

    if local_state is not None:
        local_rules_ver = local_state.get("rules_version")
        pkg_old_ver = package.get("old_rules_version")
        if local_rules_ver and pkg_old_ver and local_rules_ver != pkg_old_ver:
            conflicts.append({
                "type": "local_rules_version_mismatch",
                "severity": "high",
                "message": (f"Local rules version mismatch. "
                           f"Local: {local_rules_ver[:16]}..., "
                           f"Package expects base: {pkg_old_ver[:16]}..."),
                "resolution_options": ["skip", "override"],
            })

        local_items = {it.get("id"): it for it in local_state.get("items", [])}
        pkg_pre_items = {it.get("id"): it for it in package.get("pre_upgrade_items_snapshot", [])}
        diverged_items = []
        for iid in set(local_items.keys()) & set(pkg_pre_items.keys()):
            lit = local_items[iid]
            pit = pkg_pre_items[iid]
            if (lit.get("_version", 1) != pit.get("_version", 1) or
                lit.get("_last_modified_at") != pit.get("_last_modified_at")):
                diverged_items.append(iid)
        if diverged_items:
            conflicts.append({
                "type": "item_state_diverged",
                "severity": "high",
                "message": (f"Some items have diverged between local state and package base snapshot: "
                           f"{', '.join(diverged_items[:5])}"
                           f"{' ...' if len(diverged_items) > 5 else ''}"),
                "resolution_options": ["skip", "override"],
            })

        if local_state.get("approved"):
            conflicts.append({
                "type": "state_already_approved",
                "severity": "high",
                "message": "Local state is already APPROVED - import will clear approval",
                "resolution_options": ["skip", "override"],
            })

    rules_diff = _compare_rules_diff(rules_path, package.get("current_rules_snapshot"))
    if rules_diff.get("has_rules_snapshot") and not rules_diff.get("identical", True):
        if rules_diff.get("local_rules_missing"):
            conflicts.append({
                "type": "local_rules_missing",
                "severity": "low",
                "message": "Local rules file missing. Package contains a rules snapshot.",
                "resolution_options": ["skip", "override"],
            })
        else:
            conflicts.append({
                "type": "rules_differs",
                "severity": "medium",
                "message": "Local rules.yaml differs from package rules snapshot.",
                "resolution_options": ["skip", "override"],
            })

    return conflicts


def cmd_export_rules_upgrade_package(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found. Run 'import' first.")
        sys.exit(1)

    upgrade_source = args.upgrade_source or "pending"
    operator = args.operator or _get_identity()
    description = args.description or ""
    note = args.note or ""
    include_rules = not args.no_rules

    package = _build_rules_upgrade_handover_package(
        state, upgrade_source, operator,
        args.rules if include_rules else None,
        description, note
    )
    if package is None:
        sys.exit(1)

    out_path = args.output
    if not out_path:
        upgrade_id = package["upgrade_id"]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(SCRIPT_DIR, f"rules_upgrade_pkg_{upgrade_id}_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(package, f, ensure_ascii=False, indent=2)

    _audit(state, "rules_upgrade_package_exported",
           f"operator={operator} output={out_path} "
           f"upgrade_id={package['upgrade_id']} "
           f"source_type={package['source_type']} "
           f"description={description} note={note}")

    state.setdefault("rules_upgrade_handover_history", []).append({
        "type": "export",
        "package_checksum": package["package_checksum"],
        "exported_at": package["exported_at"],
        "exported_by": operator,
        "upgrade_id": package["upgrade_id"],
        "source_type": package["source_type"],
        "output_path": out_path,
        "description": description,
        "note": note,
    })
    save_state(state, state_path)

    print(f"\n{'=' * 70}")
    print(f"RULES UPGRADE HANDOVER PACKAGE EXPORTED")
    print(f"{'=' * 70}")
    print(f"  Output:            {out_path}")
    print(f"  Format version:    {PACKAGE_FORMAT_VERSION}")
    print(f"  Exported by:       {operator}")
    print(f"  Upgrade ID:      {package['upgrade_id']}")
    print(f"  Source type:     {package['source_type']}")
    if package.get('description'):
        print(f"  Description:      {package['description']}")
    if package.get('note'):
        print(f"  Note:             {package['note']}")
    print(f"  Rules snapshot:    {'included' if include_rules and package.get('current_rules_snapshot') else 'excluded'}")
    print()
    print(f"  Old rules ver:    {str(package.get('old_rules_version', ''))[:16]}...")
    print(f"  New rules ver:    {str(package.get('new_rules_version', ''))[:16]}...")
    print(f"  Auto-migrate:    {package.get('impact', {}).get('summary', {}).get('auto_migratable_count', 0)}")
    print(f"  Manual decisions: {len(package.get('decisions_log', []))}")
    print(f"  Package checksum: {package['package_checksum'][:16]}...")
    print()
    print(f"  Receiver: run 'rules_upgrade_import_package' to import this package")
    print(f"  Receiver: run 'rules_upgrade_handover_confirm' after import")
    print(f"{'=' * 70}")
    print()


def cmd_import_rules_upgrade_package(args, rules):
    state_path = args.state
    package_path = args.package

    if not os.path.exists(package_path):
        print(f"[ERROR] Package file not found: {package_path}")
        sys.exit(1)

    with open(package_path, "r", encoding="utf-8") as f:
        package = json.load(f)

    valid, msg = _validate_rules_upgrade_handover_package(package)
    if not valid:
        print(f"[REJECTED] Package validation failed: {msg}")
        sys.exit(1)

    target_state = load_state(state_path)
    operator = args.operator or _get_identity()

    print(f"\n{'=' * 70}")
    print(f"RULES UPGRADE PACKAGE IMPORT - READ-ONLY RECONCILE")
    print(f"{'=' * 70}")
    print(f"  Package file:    {package_path}")
    print(f"  Package format:  {package['package_format_version']}")
    print(f"  Exported at:   {package.get('exported_at')}")
    print(f"  Exported by:     {package.get('exported_by')}")
    print(f"  Upgrade ID:     {package['upgrade_id']}")
    print(f"  Source type:    {package['source_type']}")
    print(f"  Imported by:     {operator}")
    print()

    imported_pkgs = target_state.get("imported_rules_upgrade_packages", []) if target_state else []
    existing_import = None
    for imp in imported_pkgs:
        if imp.get("upgrade_id") == package["upgrade_id"] and not imp.get("revoked"):
            existing_import = imp
            break

    if existing_import and existing_import.get("confirmed"):
        print(f"[BLOCKED] Package for upgrade_id={package['upgrade_id']} already imported and confirmed.")
        print(f"  Imported at: {existing_import.get('imported_at')}")
        print(f"  Imported by: {existing_import.get('imported_by')}")
        print(f"  Use --force to re-import (will create new reconciliation, requires re-confirm)")
        if not args.force:
            sys.exit(1)

    print(f"[Package Information]")
    print(f"  Old rules ver:  {str(package.get('old_rules_version', ''))[:16]}...")
    print(f"  New rules ver:  {str(package.get('new_rules_version', ''))[:16]}...")
    impact = package.get("impact", {}).get("summary", {})
    print(f"  Auto-migrate:  {impact.get('auto_migratable_count', 0)}")
    print(f"  Conf rollback: {impact.get('confirmation_rollback_count', 0)}")
    print(f"  Manual dec:    {impact.get('manual_decision_count', 0)}")
    print(f"  Decisions log: {len(package.get('decisions_log', []))} entries")
    print()

    existing_import_info = None
    if target_state and target_state.get("pending_rules_upgrade_import"):
        pending = target_state["pending_rules_upgrade_import"]
        if pending.get("upgrade_id") == package["upgrade_id"]:
            existing_import_info = {
                "package_checksum": pending.get("package_checksum"),
                "exported_at": pending.get("exported_at"),
            }

    conflicts = _detect_rules_upgrade_handover_conflicts(
        target_state, package, args.rules, existing_import_info
    )

    if conflicts:
        print(f"[CONFLICTS DETECTED] ({len(conflicts)})")
        for i, c in enumerate(conflicts, 1):
            sev_tag = {
                "high": "[HIGH]",
                "medium": "[MED]",
                "low": "[LOW]",
            }.get(c.get("severity", "medium"), "[MED]")
            print(f"  {i}. {sev_tag} {c['type']}: {c['message']}")
            opts = c.get("resolution_options", [])
            if opts:
                print(f"     Resolution options: {', '.join(opts)}")
        print()

    state_for_pending = copy.deepcopy(target_state) if target_state else _new_state(
        package["metadata"].get("exported_from_state_version", "UNKNOWN"),
        package["metadata"].get("current_batch_id", "pending-batch"),
    )
    state_for_pending.setdefault("rules_upgrade_handover_history", [])
    state_for_pending.setdefault("imported_rules_upgrade_packages", [])

    import_id = hashlib.sha256(
        f"{datetime.now().isoformat()}{operator}{package_path}{package['upgrade_id']}".encode()
    ).hexdigest()[:16]

    state_for_pending["pending_rules_upgrade_import"] = {
        "import_id": import_id,
        "imported_at": datetime.now().isoformat(),
        "imported_by": operator,
        "import_pid": os.getpid(),
        "upgrade_id": package["upgrade_id"],
        "package_path": os.path.basename(package_path),
        "package_path_full": package_path,
        "package_checksum": package["package_checksum"],
        "exported_at": package["exported_at"],
        "exported_by": package["exported_by"],
        "source_type": package["source_type"],
        "package": copy.deepcopy(package),
        "conflicts": conflicts,
        "decisions_log": [],
        "status": "pending_confirmation",
        "confirmed": False,
        "confirmed_at": None,
        "confirmed_by": None,
        "resumed_across_restart": False,
    }

    if package.get("current_rules_snapshot") and args.apply_rules_snapshot:
        rules_dir = os.path.dirname(args.rules)
        if not os.path.exists(rules_dir):
            os.makedirs(rules_dir, exist_ok=True)
        with open(args.rules, "w", encoding="utf-8") as f:
            f.write(package["current_rules_snapshot"])
        print(f"[INFO] Rules snapshot restored to {args.rules}")
        _audit(state_for_pending, "rules_restored_from_handover_preview",
               f"from_package={package_path} operator={operator} import_id={import_id}")

    _audit(state_for_pending, "rules_upgrade_package_import_reconciled",
           f"import_id={import_id} operator={operator} package={package_path} "
           f"upgrade_id={package['upgrade_id']} "
           f"exported_by={package.get('exported_by')} "
           f"conflicts={len(conflicts)} status=pending_confirmation")

    save_state(state_for_pending, state_path)

    print()
    print(f"{'=' * 70}")
    print(f"[STATUS] RECONCILED - AWAITING CONFIRMATION")
    print(f"{'=' * 70}")
    print(f"  Import ID:       {import_id}")
    print(f"  State file written with pending_rules_upgrade_import (read-only, not committed).")
    print()
    print(f"  Next steps:")
    print(f"    1. View details:   release_cli.py --state {state_path} rules_upgrade_handover_detail")
    print(f"    2. Confirm import: release_cli.py --state {state_path} rules_upgrade_handover_confirm")
    print(f"    3. Cancel:          release_cli.py --state {state_path} rules_upgrade_handover_revoke")
    print(f"{'=' * 70}")
    print()


def cmd_rules_upgrade_handover_detail(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("rules_upgrade_handover_history", [])
    state.setdefault("imported_rules_upgrade_packages", [])

    pending = state.get("pending_rules_upgrade_import")
    import_id = getattr(args, "import_id", None)
    target = None
    is_pending = False

    if pending and (import_id is None or pending.get("import_id") == import_id):
        target = pending
        is_pending = True
    elif import_id:
        for imp in state.get("imported_rules_upgrade_packages", []):
            if imp.get("import_id") == import_id:
                target = imp
                break
        if target is None:
            for hist in state.get("rules_upgrade_handover_history", []):
                if hist.get("import_id") == import_id:
                    target = hist
                    break

    if target is None:
        print("[INFO] No rules upgrade handover record found.")
        print("  Run 'rules_upgrade_import_package' first.")
        return

    tid = target.get("import_id", "?")
    print(f"\n{'=' * 70}")
    tag = " [PENDING CONFIRMATION]" if is_pending else ""
    if target.get("confirmed") or target.get("confirmed_at"):
        tag = " [CONFIRMED / PERSISTED]"
    print(f"RULES UPGRADE HANDOVER DETAIL: {tid}{tag}")
    print(f"{'=' * 70}")

    print(f"\n[Basic Info]")
    print(f"  Import ID:        {tid}")
    print(f"  Imported at:      {target.get('imported_at')}")
    print(f"  Imported by:    {target.get('imported_by')}")
    print(f"  Exported by:    {target.get('exported_by')}")
    print(f"  Exported at:      {target.get('exported_at')}")
    print(f"  Upgrade ID:      {target.get('upgrade_id')}")
    print(f"  Source type:     {target.get('source_type')}")
    print(f"  Package:          {target.get('package_path')}")
    print(f"  Package checksum: {target.get('package_checksum', '(legacy)')}")

    if target.get("confirmed_at"):
        print(f"\n[Confirmation]")
        print(f"  Confirmed at:     {target.get('confirmed_at')}")
        print(f"  Confirmed by:     {target.get('confirmed_by')}")

    pkg = target.get("package")
    if pkg:
        print(f"\n[Upgrade Impact]")
        impact = pkg.get("impact", {}).get("summary", {})
        print(f"  Auto-migratable:   {impact.get('auto_migratable_count', 0)}")
        print(f"  Confirmation rollback: {impact.get('confirmation_rollback_count', 0)}")
        print(f"  Manual decisions:  {impact.get('manual_decision_count', 0)}")
        print(f"  Decisions log:    {len(pkg.get('decisions_log', []))} entries")
        print()
        print(f"  Old rules ver:    {str(pkg.get('old_rules_version', ''))[:16]}...")
        print(f"  New rules ver:    {str(pkg.get('new_rules_version', ''))[:16]}...")

    conflicts = target.get("conflicts", [])
    if conflicts:
        print(f"\n[Conflicts Detected] ({len(conflicts)})")
        for i, c in enumerate(conflicts, 1):
            sev_tag = {
                "high": "[HIGH]",
                "medium": "[MED]",
                "low": "[LOW]",
            }.get(c.get("severity", "medium"), "[MED]")
            print(f"  {i}. {sev_tag} {c['type']}: {c['message']}")
            opts = c.get("resolution_options", [])
            if opts:
                print(f"     Options: {', '.join(opts)}")

    decisions = target.get("decisions_log", [])
    if decisions:
        print(f"\n[Conflict Decisions Applied]")
        for d in decisions:
            print(f"  [{d.get('ts')}] {d.get('conflict_type')}: {d.get('decision')}"
                  f" - {d.get('detail', '')}")

    if target.get("resumed_across_restart"):
        print(f"\n[Cross-Restart]")
        print(f"  This session has been RESUMED after a process restart.")

    print(f"\n{'=' * 70}")
    if is_pending:
        print(f"STATUS: PENDING CONFIRMATION")
        print(f"  Confirm:  release_cli.py --state {state_path} rules_upgrade_handover_confirm")
        print(f"  Revoke:   release_cli.py --state {state_path} rules_upgrade_handover_revoke")
    else:
        print(f"STATUS: CONFIRMED (persisted in imported_rules_upgrade_packages")
    print(f"{'=' * 70}")
    print()


def cmd_rules_upgrade_handover_confirm(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("rules_upgrade_handover_history", [])
    state.setdefault("imported_rules_upgrade_packages", [])

    pending = state.get("pending_rules_upgrade_import")
    if not pending:
        print("[ERROR] No pending rules upgrade handover import found.")
        print("  Run 'rules_upgrade_import_package' first.")
        sys.exit(1)

    import_id = pending.get("import_id", "?")
    operator = getattr(args, "operator", None) or _get_identity()
    package = pending.get("package")
    upgrade_id = pending.get("upgrade_id")

    existing_imports = state.get("imported_rules_upgrade_packages", [])
    duplicate = None
    for imp in existing_imports:
        if imp.get("upgrade_id") == upgrade_id and not imp.get("revoked"):
            duplicate = imp
            break

    if duplicate and not args.force:
        print(f"[BLOCKED] Upgrade {upgrade_id} already imported and confirmed.")
        print(f"  Confirmed at: {duplicate.get('confirmed_at')}")
        print(f"  Confirmed by: {duplicate.get('confirmed_by')}")
        print(f"  Use --force to override (will mark previous as revoked and apply this one).")
        sys.exit(1)

    if duplicate and args.force:
        duplicate["revoked"] = True
        duplicate["revoked_at"] = datetime.now().isoformat()
        duplicate["revoked_by"] = operator
        duplicate["revoke_reason"] = "re-imported with --force"
        _audit(state, "rules_upgrade_handover_revoked",
               f"import_id={duplicate.get('import_id')} operator={operator} reason=re-imported")

    decision_mode = getattr(args, "decision", "override_all")
    per_item_raw = getattr(args, "per_item", None)
    per_item_decision = {}
    if per_item_raw:
        for pair in per_item_raw.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                per_item_decision[k.strip()] = v.strip()

    decisions_log = pending.setdefault("decisions_log", [])
    ts_now = datetime.now().isoformat()

    for conflict in pending.get("conflicts", []):
        ctype = conflict.get("type")
        per_key = f"conflict:{ctype}"
        if per_key in per_item_decision:
            dec = per_item_decision[per_key]
        elif decision_mode == "override_all":
            dec = "override"
        elif decision_mode == "skip_all":
            dec = "skip"
        else:
            dec = "override"

        opts = conflict.get("resolution_options", ["override"])
        if dec not in opts and "override" in opts:
            dec = "override"
        decisions_log.append({
            "ts": ts_now,
            "conflict_type": ctype,
            "decision": dec,
            "operator": operator,
            "detail": f"conflict={ctype} decision={dec} mode={decision_mode}",
        })

    upgrade_snapshot = package["source_upgrade_snapshot"]
    old_rules = upgrade_snapshot.get("old_rules_snapshot") or upgrade_snapshot.get("rules_snapshot")
    new_rules = upgrade_snapshot.get("new_rules_snapshot")
    impact = upgrade_snapshot.get("impact")
    decisions = copy.deepcopy(package.get("decisions", {}))

    manual_items = impact.get("manual_decision_required", [])
    if manual_items and per_item_decision:
        for m in manual_items:
            iid = m.get("item_id")
            field = m.get("field")
            key = f"{m['type']}:{iid}:{field}"
            pi_key = f"{iid}.{field}"

            if pi_key in per_item_decision:
                val = per_item_decision[pi_key]
                decisions[key] = {"action": "set", "value": val}
                decisions_log.append({
                    "ts": ts_now,
                    "type": m["type"],
                    "item_id": iid,
                    "field": field,
                    "decision": f"set={val}",
                    "operator": operator,
                    "detail": f"per-item: {iid}.{field} = {val}",
                })

    if per_item_decision:
        for pi_key, val in per_item_decision.items():
            if "." not in pi_key:
                continue
            iid, field = pi_key.split(".", 1)
            already_applied = any(
                (d.get("item_id") == iid and d.get("field") == field)
                for d in decisions_log
            )
            if already_applied:
                continue
            target = _find_in_list(state.get("items", []), iid)
            if target:
                old_val = target.get(field)
                target[field] = val
                _bump_item_version(target, operator=operator)
                decisions_log.append({
                    "ts": ts_now,
                    "type": "per_item_override",
                    "item_id": iid,
                    "field": field,
                    "decision": f"set={val}",
                    "operator": operator,
                    "detail": f"per-item override: {iid}.{field} {old_val} -> {val}",
                })

    pre_upgrade_state = copy.deepcopy(state)
    _apply_rules_upgrade_to_state(state, old_rules, new_rules, impact, decisions, operator)

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print("  [NOTE] Approval cleared (rules changed after approval).")

    import_record = {
        "import_id": import_id,
        "imported_at": pending.get("imported_at"),
        "imported_by": pending.get("imported_by"),
        "import_pid": pending.get("import_pid"),
        "upgrade_id": upgrade_id,
        "exported_at": pending.get("exported_at"),
        "exported_by": pending.get("exported_by"),
        "package_path": pending.get("package_path"),
        "package_checksum": pending.get("package_checksum"),
        "source_type": pending.get("source_type"),
        "package": copy.deepcopy(package),
        "conflicts": pending.get("conflicts", []),
        "decisions_log": decisions_log,
        "pre_import_state": pre_upgrade_state,
        "status": "confirmed",
        "confirmed": True,
        "confirmed_at": datetime.now().isoformat(),
        "confirmed_by": operator,
        "revoked": False,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
        "resumed_across_restart": pending.get("resumed_across_restart", False),
    }

    state["imported_rules_upgrade_packages"].append(import_record)
    state["rules_upgrade_handover_history"].append({
        "type": "import",
        "import_id": import_id,
        "upgrade_id": upgrade_id,
        "imported_at": import_record["imported_at"],
        "imported_by": operator,
        "exported_by": pending.get("exported_by"),
        "confirmed_at": import_record["confirmed_at"],
        "package_checksum": pending.get("package_checksum"),
        "revoked": False,
    })

    upgrade_record = {
        "upgrade_id": upgrade_id,
        "applied_at": import_record["confirmed_at"],
        "applied_by": operator,
        "old_rules_snapshot": copy.deepcopy(old_rules),
        "new_rules_snapshot": copy.deepcopy(new_rules),
        "old_rules_version": package.get("old_rules_version"),
        "new_rules_version": package.get("new_rules_version"),
        "rules_diff": package.get("rules_diff"),
        "impact": package.get("impact"),
        "decisions": decisions,
        "decisions_log": package.get("decisions_log", []),
        "pre_upgrade_state_snapshot": {
            "confirmations": copy.deepcopy(pre_upgrade_state.get("confirmations", {})),
            "items": copy.deepcopy(pre_upgrade_state.get("items", [])),
            "approved": pre_upgrade_state.get("approved", False),
            "approved_at_version": pre_upgrade_state.get("approved_at_version"),
            "approved_at_draft_version": pre_upgrade_state.get("approved_at_draft_version"),
            "rules_version": pre_upgrade_state.get("rules_version"),
            "rules_snapshot": copy.deepcopy(pre_upgrade_state.get("rules_snapshot")),
        },
        "status": "applied",
        "revoked": False,
        "revoked_at": None,
        "revoked_by": None,
        "revoke_reason": None,
        "handover_import_id": import_id,
        "handover_exported_by": pending.get("exported_by"),
    }

    state.setdefault("rules_upgrade_history", []).append(upgrade_record)
    state["pending_rules_upgrade_import"] = None

    _audit(state, "rules_upgrade_handover_confirmed",
           f"import_id={import_id} operator={operator} "
           f"upgrade_id={upgrade_id} "
           f"decisions={len(decisions_log)} conflicts={len(pending.get('conflicts', []))} "
           f"exported_by={pending.get('exported_by')}")

    save_state(state, state_path)

    print(f"\n{'=' * 70}")
    print(f"RULES UPGRADE HANDOVER CONFIRMED")
    print(f"{'=' * 70}")
    print(f"  Import ID:        {import_id}")
    print(f"  Upgrade ID:      {upgrade_id}")
    print(f"  Confirmed at:   {import_record['confirmed_at']}")
    print(f"  Confirmed by:   {operator}")
    print(f"  Source type:     {pending.get('source_type')}")
    print(f"  Conflicts resolved: {len(pending.get('conflicts', []))}")
    if decisions_log:
        print(f"  Decisions applied:  {len(decisions_log)}")
    print(f"  Rules version:    {str(package.get('old_rules_version', ''))[:16]}... -> {str(package.get('new_rules_version', ''))[:16]}...")
    print()
    print(f"  Session is now PERSISTED (cross-restart safe).")
    print(f"  Entry added to imported_rules_upgrade_packages and rules_upgrade_history.")
    print(f"  Full package preserved with handover traces.")
    print()
    print(f"  Next steps:")
    print(f"     release_cli.py --state {state_path} status")
    print(f"     release_cli.py --state {state_path} rules_history")
    print(f"{'=' * 70}")
    print()


def cmd_rules_upgrade_handover_revoke(args, rules):
    state_path = args.state
    state = load_state(state_path)
    if state is None:
        print("[ERROR] No state found.")
        sys.exit(1)

    state.setdefault("rules_upgrade_handover_history", [])
    state.setdefault("imported_rules_upgrade_packages", [])

    import_id = getattr(args, "import_id", None)
    reason = getattr(args, "reason", "") or "manual revocation"
    operator = getattr(args, "operator", None) or _get_identity()
    force = getattr(args, "force", False)

    pending = state.get("pending_rules_upgrade_import")
    if pending and (import_id is None or pending.get("import_id") == import_id):
        tid = pending.get("import_id", "?")
        print(f"\n[Revoking PENDING HANDOVER IMPORT] {tid}")
        print(f"  Reason: {reason}")

        _audit(state, "rules_upgrade_handover_revoked_pending",
               f"import_id={tid} operator={operator} reason={reason}")

        pre_import_state = pending.get("pre_import_state")
        if pre_import_state is None:
            state["pending_rules_upgrade_import"] = None
            save_state(state, state_path)
            print(f"  Pending handover import removed.")
        else:
            restored = copy.deepcopy(pre_import_state)
            restored["rules_upgrade_handover_history"] = state.get("rules_upgrade_handover_history", [])
            restored["imported_rules_upgrade_packages"] = state.get("imported_rules_upgrade_packages", [])
            restored["audit_log"] = state.get("audit_log", [])
            restored["pending_rules_upgrade_import"] = None
            _audit(restored, "rules_upgrade_handover_revoked_pending_restore",
                   f"import_id={tid} operator={operator} reason={reason}")
            save_state(restored, state_path)
            print(f"  Pending handover import removed. Pre-import state restored.")

        print(f"[OK] Pending handover import {tid} revoked successfully.")
        print()
        return

    target_id = import_id
    if target_id is None:
        imp_ids = [imp["import_id"] for imp in state.get("imported_rules_upgrade_packages", []) if not imp.get("revoked")]
        if not imp_ids:
            print("[ERROR] No pending import and no confirmed imports to revoke.")
            sys.exit(1)
        target_id = imp_ids[-1]

    target = None
    for imp in state.get("imported_rules_upgrade_packages", []):
        if imp.get("import_id") == target_id:
            target = imp
            break

    if not target:
        print(f"[ERROR] Confirmed handover import import_id={target_id} not found.")
        sys.exit(1)

    if not force:
        print(f"[WARNING] You are revoking a CONFIRMED (persisted) handover import.")
        print(f"  Import ID: {target_id}")
        print(f"  Upgrade ID: {target.get('upgrade_id')}")
        print(f"  Confirmed at: {target.get('confirmed_at')}")
        print(f"  Confirmed by: {target.get('confirmed_by')}")
        print(f"  This will rollback state to pre-import snapshot (if available).")
        print(f"  Re-run with --force to proceed, or --import-id to pick another.")
        sys.exit(1)

    tid = target_id
    print(f"\n[Revoking CONFIRMED HANDOVER IMPORT] {tid}")
    print(f"  Reason: {reason}")
    print(f"  Force:  yes")

    ts_now = datetime.now().isoformat()
    target["revoked"] = True
    target["revoked_at"] = ts_now
    target["revoked_by"] = operator
    target["revoke_reason"] = reason

    for hist in state.get("rules_upgrade_handover_history", []):
        if hist.get("import_id") == tid:
            hist["revoked"] = True
            hist["revoked_at"] = ts_now
            hist["revoked_by"] = operator
            hist["revoke_reason"] = reason
            break

    for upg in state.get("rules_upgrade_history", []):
        if upg.get("handover_import_id") == tid and not upg.get("revoked"):
            upg["revoked"] = True
            upg["revoked_at"] = ts_now
            upg["revoked_by"] = operator
            upg["revoke_reason"] = reason
            break

    _audit(state, "rules_upgrade_handover_revoked_confirmed",
           f"import_id={tid} operator={operator} reason={reason}")

    pre_import = target.get("pre_import_state")
    if pre_import is not None:
        restored = copy.deepcopy(pre_import)
        restored["rules_upgrade_handover_history"] = state.get("rules_upgrade_handover_history", [])
        restored["imported_rules_upgrade_packages"] = state.get("imported_rules_upgrade_packages", [])
        restored["rules_upgrade_history"] = state.get("rules_upgrade_history", [])
        restored["audit_log"] = state.get("audit_log", [])
        restored["pending_rules_upgrade_import"] = None
        _audit(restored, "rules_upgrade_handover_revoked_confirmed_rollback",
               f"import_id={tid} operator={operator} reason={reason}")
        save_state(restored, state_path)
        print(f"  State rolled back to pre-import snapshot.")
    else:
        save_state(state, state_path)
        print(f"  Session marked revoked. (No pre-import snapshot available for rollback)")

    print(f"[OK] Confirmed handover import {tid} revoked successfully.")
    print()


def _render_markdown(state):
    lines = []
    v = state.get("version", "UNKNOWN")
    lines.append(f"# Release Notes v{v}")
    lines.append("")

    lines.append("## Overview")
    lines.append("")
    lines.append(f"Version **{v}** release notes. Please review all sections before approval.")
    lines.append("")

    lines.append("## Changes")
    lines.append("")
    items = state.get("items", [])
    if items:
        by_cat = {}
        for it in items:
            cat = it.get("category", "other")
            by_cat.setdefault(cat, []).append(it)
        for cat, group in sorted(by_cat.items()):
            lines.append(f"### {cat.upper()}")
            lines.append("")
            for it in group:
                risk = it.get("risk_level", "unknown")
                owner = it.get("owner") or "(missing owner)"
                lines.append(f"- **{it.get('id', '?')}** {it.get('title', '')} `risk:{risk}` `owner:{owner}`")
                desc = it.get("description", "")
                if desc:
                    lines.append(f"  > {desc}")
            lines.append("")
    else:
        lines.append("_No change items imported yet._")
        lines.append("")

    lines.append("## Migration")
    lines.append("")
    migs = state.get("migration_reminders", [])
    if migs:
        for m in migs:
            mid = m.get("id", "?")
            processed = state.get("migration_processed", {}).get(mid, False)
            status_tag = "[PROCESSED]" if processed else "[PENDING]"
            lines.append(f"- **{mid}** {m.get('title', '')} {status_tag}")
            lines.append(f"  > {m.get('description', '')}")
            ar = m.get("action_required", "")
            if ar:
                lines.append(f"  - Action: {ar}")
            lines.append("")
    else:
        lines.append("_No migration reminders._")
        lines.append("")

    lines.append("## Known Issues")
    lines.append("")
    kis = state.get("known_issues", [])
    if kis:
        reviewed = state.get("known_issues_reviewed", False)
        tag = "[REVIEWED]" if reviewed else "[NOT REVIEWED]"
        lines.append(f"_Status: {tag}_")
        lines.append("")
        for ki in kis:
            lines.append(f"- **{ki.get('id', '?')}** {ki.get('title', '')}")
            lines.append(f"  > {ki.get('description', '')}")
            wa = ki.get("workaround", "")
            if wa:
                lines.append(f"  - Workaround: {wa}")
            lines.append("")
    else:
        lines.append("_No known issues._")
        lines.append("")

    handover_imports = state.get("imported_rules_upgrade_packages", [])
    active_imports = [imp for imp in handover_imports if not imp.get("revoked")]
    revoked_imports = [imp for imp in handover_imports if imp.get("revoked")]

    if active_imports or revoked_imports:
        lines.append("## Rules Upgrade Handover History")
        lines.append("")
        lines.append("### Active Imports")
        lines.append("")
        if active_imports:
            for imp in active_imports:
                lines.append(f"- **{imp.get('import_id')}** (Upgrade ID: {imp.get('upgrade_id')})")
                lines.append(f"  - Exported by: {imp.get('exported_by')} @ {imp.get('exported_at')}")
                lines.append(f"  - Imported by: {imp.get('imported_by')} @ {imp.get('imported_at')}")
                lines.append(f"  - Confirmed by: {imp.get('confirmed_by')} @ {imp.get('confirmed_at')}")
                lines.append(f"  - Rules: {str(imp.get('package', {}).get('old_rules_version', ''))[:16]}... -> {str(imp.get('package', {}).get('new_rules_version', ''))[:16]}...")
                lines.append(f"  - Decisions: {len(imp.get('decisions_log', []))} applied")
                conflicts = imp.get("conflicts", [])
                if conflicts:
                    lines.append(f"  - Conflicts resolved: {len(conflicts)}")
                lines.append("")
        else:
            lines.append("_No active handover imports._")
            lines.append("")

        if revoked_imports:
            lines.append("### Revoked Imports")
            lines.append("")
            for imp in revoked_imports:
                lines.append(f"- ~~{imp.get('import_id')}~~ (Upgrade ID: {imp.get('upgrade_id')})")
                lines.append(f"  - Revoked by: {imp.get('revoked_by')} @ {imp.get('revoked_at')}")
                lines.append(f"  - Reason: {imp.get('revoke_reason')}")
                lines.append("")

    rules_upgrades = state.get("rules_upgrade_history", [])
    active_upgrades = [u for u in rules_upgrades if not u.get("revoked")]
    revoked_upgrades = [u for u in rules_upgrades if u.get("revoked")]

    if active_upgrades or revoked_upgrades:
        lines.append("## Rules Upgrade History")
        lines.append("")
        lines.append("### Applied Upgrades")
        lines.append("")
        if active_upgrades:
            for u in active_upgrades:
                handover_tag = ""
                if u.get("handover_import_id"):
                    handover_tag = f" (via handover import {u.get('handover_import_id')} from {u.get('handover_exported_by')})"
                lines.append(f"- **{u.get('upgrade_id')}**{handover_tag}")
                lines.append(f"  - Applied by: {u.get('applied_by')} @ {u.get('applied_at')}")
                lines.append(f"  - Rules: {str(u.get('old_rules_version', ''))[:16]}... -> {str(u.get('new_rules_version', ''))[:16]}...")
                impact = u.get("impact", {}).get("summary", {})
                lines.append(f"  - Impact: auto={impact.get('auto_migratable_count', 0)} rollback={impact.get('confirmation_rollback_count', 0)} manual={impact.get('manual_decision_count', 0)}")
                lines.append("")
        else:
            lines.append("_No applied rules upgrades._")
            lines.append("")

        if revoked_upgrades:
            lines.append("### Revoked Upgrades")
            lines.append("")
            for u in revoked_upgrades:
                lines.append(f"- ~~{u.get('upgrade_id')}~~")
                lines.append(f"  - Revoked by: {u.get('revoked_by')} @ {u.get('revoked_at')}")
                lines.append(f"  - Reason: {u.get('revoke_reason')}")
                lines.append("")

    lines.append("---")
    lines.append(f"_Generated at {datetime.now().isoformat()} | Draft v{state.get('draft_version', 0)}_")
    return "\n".join(lines)


def main():
    try:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if hasattr(sys.stderr, "reconfigure"):
            try:
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    except Exception:
        pass
    parser = argparse.ArgumentParser(
        prog="release_cli",
        description="Release Notes Consistency Check CLI",
    )
    parser.add_argument("--rules", default=DEFAULT_RULES, help="Path to rules.yaml")
    parser.add_argument("--state", default=DEFAULT_STATE, help="Path to state file")

    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="Import change items from manifest")
    p_import.add_argument("manifest", help="Path to manifest JSON file")

    p_draft = sub.add_parser("draft", help="Generate Markdown draft")
    p_draft.add_argument("-o", "--output", help="Output file path")

    p_confirm = sub.add_parser("confirm", help="Confirm a section")
    p_confirm.add_argument("section", help="Section name (overview, changes, migration, known_issues)")

    p_reject = sub.add_parser("reject", help="Reject/return a section")
    p_reject.add_argument("section", help="Section name")
    p_reject.add_argument("--reason", help="Reason for rejection")

    sub.add_parser("approve", help="Approve the release version")
    sub.add_parser("rollback", help="Rollback to previous draft")

    p_amend = sub.add_parser("amend", help="Amend a field on an existing item")
    p_amend.add_argument("item_id", help="Item ID to amend (e.g. CHG-003)")
    p_amend.add_argument("--field", action="append", default=[],
                         help="Field to amend as key=value (repeatable, e.g. --field owner=周七 --field risk_level=critical)")
    p_amend.add_argument("--operator", default="cli:amend",
                         help="Operator identifier (for audit & version tracking)")

    p_bulk = sub.add_parser("bulk_amend", help="Batch-amend items from JSON/CSV patch")
    p_bulk.add_argument("patch", nargs="?", default=None,
                        help="Path to patch file (.json or .csv). Omit when using --resume.")
    p_bulk.add_argument("--operator", default=None,
                        help="Operator identifier (for audit & version tracking)")
    p_bulk.add_argument("--reason", default=None,
                        help="Reason for this batch amend (stored in audit)")
    p_bulk.add_argument("--mode", choices=["interactive", "abort", "skip", "overwrite"], default=None,
                        help="Conflict resolution mode: interactive (default, prompts via --resume), "
                             "abort (fail batch), skip (skip conflicted only), overwrite (force all)")
    p_bulk.add_argument("--resume", type=int, default=None, metavar="IDX",
                        help="Resume a previously persisted bulk amend at given pending index")
    p_bulk.add_argument("--decision", choices=["abort", "skip", "overwrite"], default=None,
                        help="Default decision when resuming (used for all conflicts unless --per-item overrides)")
    p_bulk.add_argument("--per-item", default=None, metavar="id=dec,id=dec",
                        help="Per-item conflict overrides (comma-separated key=value, e.g. CHG-003=overwrite,CHG-005=skip)")

    p_export = sub.add_parser("export", help="Export final release notes")
    p_export.add_argument("-o", "--output", help="Output file path")

    sub.add_parser("status", help="Show current status")
    sub.add_parser("history", help="Show audit history")

    p_export_pkg = sub.add_parser("export_package", help="Export full revision package for handoff")
    p_export_pkg.add_argument("-o", "--output", help="Output package file path (.json)")
    p_export_pkg.add_argument("--operator", help="Operator identifier (for audit)")
    p_export_pkg.add_argument("--description", help="Description of this package snapshot")
    p_export_pkg.add_argument("--no-rules", action="store_true",
                              help="Exclude rules.yaml snapshot from package")

    p_import_pkg = sub.add_parser("import_package", help="Import revision package from handoff")
    p_import_pkg.add_argument("package", help="Path to package .json file")
    p_import_pkg.add_argument("--operator", help="Operator identifier (for audit)")
    p_import_pkg.add_argument("--mode", choices=["merge", "takeover"], default="merge",
                              help="Import mode: merge (continue, preserve target history) or takeover (replace entire state)")
    p_import_pkg.add_argument("--force", action="store_true",
                              help="Force import even if target is newer or already approved")
    p_import_pkg.add_argument("--keep-target-batches", action="store_true",
                              help="In merge mode, keep target's imported_batches in addition to package's")
    p_import_pkg.add_argument("--apply-rules-snapshot", action="store_true",
                              help="Apply rules snapshot from package to local rules.yaml")

    p_preflight = sub.add_parser("preflight_check", help="Pre-check a package without modifying state - show diffs, conflicts, recommended mode")
    p_preflight.add_argument("package", help="Path to package .json file to preflight")
    p_preflight.add_argument("--operator", help="Operator identifier (for audit, though no state is written)")

    p_audit_view = sub.add_parser("audit_view", help="Show audit timeline of takeovers - field changes, decision makers, cross-restart status")

    p_tk_detail = sub.add_parser("takeover_detail", help="Show handover detail - reconcile summary, conflicts, handover_summary, diffs")
    p_tk_detail.add_argument("--takeover-id", default=None, help="Specific takeover_id to view (default: latest pending or latest confirmed)")

    p_tk_confirm = sub.add_parser("takeover_confirm", help="Confirm pending takeover and persist cross-restart session")
    p_tk_confirm.add_argument("--operator", help="Operator identifier (for audit & decision log)")
    p_tk_confirm.add_argument("--force", action="store_true",
                              help="Force confirm even if blocked (target already approved or newer)")
    p_tk_confirm.add_argument("--decision",
                              choices=["override_all", "skip_all"],
                              default="override_all",
                              help="Default conflict resolution strategy")
    p_tk_confirm.add_argument("--per-item", default=None, metavar="conflict:TYPE=DEC,...",
                              help=("Per-conflict-type overrides "
                                    "(e.g. conflict:rules_differs=skip,conflict:draft_version_mismatch=override)"))

    p_tk_revoke = sub.add_parser("takeover_revoke", help="Revoke/undo a takeover (pending or confirmed)")
    p_tk_revoke.add_argument("--takeover-id", default=None, help="Specific takeover_id to revoke (default: pending or latest confirmed)")
    p_tk_revoke.add_argument("--reason", default="", help="Reason for revocation (written to audit log)")
    p_tk_revoke.add_argument("--operator", help="Operator identifier (for audit)")
    p_tk_revoke.add_argument("--force", action="store_true",
                             help="Required to revoke a confirmed (persisted) takeover session")

    p_rules_check = sub.add_parser("rules_upgrade_check", help="Check rules upgrade impact - compare current state rules with local rules, show affected items")
    p_rules_check.add_argument("--operator", help="Operator identifier (for audit)")

    p_rules_apply = sub.add_parser("rules_upgrade_apply", help="Apply pending rules upgrade with decisions")
    p_rules_apply.add_argument("--operator", help="Operator identifier (for audit & decision log)")
    p_rules_apply.add_argument("--mode", choices=["auto", "force"], default="auto",
                               help="Apply mode: auto (default, blocks on unresolved) or force (apply anyway)")
    p_rules_apply.add_argument("--decision", default=None,
                               help="Default decision for manual conflicts (e.g. set=high, set=first_valid, skip)")
    p_rules_apply.add_argument("--per-item", default=None, metavar="id.field=val,id2.field=val2",
                               help="Per-item overrides (comma-separated, e.g. CHG-003.risk_level=high,CHG-005.category=security)")

    p_rules_skip = sub.add_parser("rules_upgrade_skip", help="Skip/dismiss pending rules upgrade")
    p_rules_skip.add_argument("--operator", help="Operator identifier (for audit)")
    p_rules_skip.add_argument("--reason", default="", help="Reason for skipping")

    p_rules_undo = sub.add_parser("rules_upgrade_undo", help="Undo a previously applied rules upgrade")
    p_rules_undo.add_argument("--upgrade-id", default=None, help="Specific upgrade_id to undo (default: latest applied)")
    p_rules_undo.add_argument("--reason", default="", help="Reason for undo (written to audit log)")
    p_rules_undo.add_argument("--operator", help="Operator identifier (for audit)")

    p_rules_hist = sub.add_parser("rules_history", help="Show rules upgrade history - applied, revoked, pending")

    p_export_ru_pkg = sub.add_parser("export_rules_upgrade_package", help="Export rules upgrade handover package - decision context, conflicts, rules snapshot")
    p_export_ru_pkg.add_argument("--upgrade-source", default="pending",
                                  help="Source of upgrade: 'pending' (default) or upgrade_id or 'latest' for applied")
    p_export_ru_pkg.add_argument("-o", "--output", help="Output package file path (.json)")
    p_export_ru_pkg.add_argument("--operator", help="Operator identifier (for audit)")
    p_export_ru_pkg.add_argument("--description", help="Description of this handover package")
    p_export_ru_pkg.add_argument("--note", help="Operator note for receiver")
    p_export_ru_pkg.add_argument("--no-rules", action="store_true",
                                  help="Exclude rules.yaml snapshot from package")

    p_import_ru_pkg = sub.add_parser("import_rules_upgrade_package", help="Import rules upgrade handover package - reconcile, detect conflicts")
    p_import_ru_pkg.add_argument("package", help="Path to rules upgrade handover package .json file")
    p_import_ru_pkg.add_argument("--operator", help="Operator identifier (for audit)")
    p_import_ru_pkg.add_argument("--force", action="store_true",
                                  help="Force re-import even if same upgrade_id already imported")
    p_import_ru_pkg.add_argument("--apply-rules-snapshot", action="store_true",
                                  help="Apply rules snapshot from package to local rules.yaml during reconcile")

    p_ru_handoff_detail = sub.add_parser("rules_upgrade_handover_detail", help="Show rules upgrade handover detail - conflicts, decisions, import status")
    p_ru_handoff_detail.add_argument("--import-id", default=None, help="Specific import_id to view (default: latest pending or latest confirmed)")

    p_ru_handoff_confirm = sub.add_parser("rules_upgrade_handover_confirm", help="Confirm pending rules upgrade handover import - apply upgrade, persist records")
    p_ru_handoff_confirm.add_argument("--operator", help="Operator identifier (for audit & decision log)")
    p_ru_handoff_confirm.add_argument("--force", action="store_true",
                                       help="Force confirm even if same upgrade_id already imported (will revoke previous)")
    p_ru_handoff_confirm.add_argument("--decision",
                                       choices=["override_all", "skip_all"],
                                       default="override_all",
                                       help="Default conflict resolution strategy")
    p_ru_handoff_confirm.add_argument("--per-item", default=None, metavar="conflict:TYPE=DEC,...",
                                       help=("Per-conflict-type overrides "
                                             "(e.g. conflict:rules_differs=skip,conflict:item_state_diverged=override)"))

    p_ru_handoff_revoke = sub.add_parser("rules_upgrade_handover_revoke", help="Revoke/undo rules upgrade handover import (pending or confirmed)")
    p_ru_handoff_revoke.add_argument("--import-id", default=None, help="Specific import_id to revoke (default: pending or latest confirmed)")
    p_ru_handoff_revoke.add_argument("--reason", default="", help="Reason for revocation (written to audit log)")
    p_ru_handoff_revoke.add_argument("--operator", help="Operator identifier (for audit)")
    p_ru_handoff_revoke.add_argument("--force", action="store_true",
                                       help="Required to revoke a confirmed (persisted) handover import")

    args = parser.parse_args()
    rules = load_rules(args.rules)

    dispatch = {
        "import": cmd_import,
        "draft": cmd_draft,
        "confirm": cmd_confirm,
        "reject": cmd_reject,
        "approve": cmd_approve,
        "rollback": cmd_rollback,
        "amend": cmd_amend,
        "bulk_amend": cmd_bulk_amend,
        "export": cmd_export,
        "status": cmd_status,
        "history": cmd_history,
        "export_package": cmd_export_package,
        "import_package": cmd_import_package,
        "preflight_check": cmd_preflight_check,
        "audit_view": cmd_audit_view,
        "takeover_detail": cmd_takeover_detail,
        "takeover_confirm": cmd_takeover_confirm,
        "takeover_revoke": cmd_takeover_revoke,
        "rules_upgrade_check": cmd_rules_upgrade_check,
        "rules_upgrade_apply": cmd_rules_upgrade_apply,
        "rules_upgrade_skip": cmd_rules_upgrade_skip,
        "rules_upgrade_undo": cmd_rules_upgrade_undo,
        "rules_history": cmd_rules_history,
        "export_rules_upgrade_package": cmd_export_rules_upgrade_package,
        "import_rules_upgrade_package": cmd_import_rules_upgrade_package,
        "rules_upgrade_handover_detail": cmd_rules_upgrade_handover_detail,
        "rules_upgrade_handover_confirm": cmd_rules_upgrade_handover_confirm,
        "rules_upgrade_handover_revoke": cmd_rules_upgrade_handover_revoke,
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args, rules)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
