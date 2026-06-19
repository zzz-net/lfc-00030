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


def _build_package(state, operator, rules_path, description):
    rules_copy = None
    if rules_path and os.path.exists(rules_path):
        with open(rules_path, "r", encoding="utf-8") as f:
            rules_copy = f.read()

    package = {
        "package_format_version": PACKAGE_FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "exported_by": operator or _get_identity(),
        "description": description or "",
        "state": copy.deepcopy(state),
        "state_checksum": _compute_state_checksum(state),
        "rules_snapshot": rules_copy,
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

    print(f"\n== Import Package: '{package_path}' ==")
    print(f"   Package format:  {package['package_format_version']}")
    print(f"   Exported at:     {package.get('exported_at')}")
    print(f"   Exported by:     {package.get('exported_by')}")
    print(f"   Package state:   v{package_state.get('version')} / draft v{package_state.get('draft_version')}")
    print(f"   Import mode:     {mode}")
    print(f"   Imported by:     {operator}")
    print()

    if target_state is not None:
        age_info = _compare_state_age(target_state, package_state)
        print(f"Target state:      v{target_state.get('version')} / draft v{age_info['target_draft_version']}")
        print(f"Package state:     v{package_state.get('version')} / draft v{age_info['package_draft_version']}")
        print(f"Target audit len:  {age_info['target_audit_len']}")
        print(f"Package audit len: {age_info['package_audit_len']}")
        print(f"Target approved:   {age_info['target_approved']}")
        print(f"Package approved:  {age_info['package_approved']}")
        print()

        if target_state.get("approved") and not args.force:
            print("[REJECTED] Target state is already approved.")
            print("   Use --force to override.")
            if target_state is not None:
                _audit(target_state, "import_package_rejected",
                       f"operator={operator} package={package_path} reason=target_approved")
                save_state(target_state, state_path)
            sys.exit(1)

        if age_info["target_newer"] and not args.force:
            print("[REJECTED] Target state is NEWER than package state.")
            print("   Importing would overwrite newer changes.")
            print("   Use --force to override, or consider:")
            print("     --mode merge    Continue from package state, merging history")
            print("     --mode takeover Completely replace target state with package")
            print()
            if target_state is not None:
                _audit(target_state, "import_package_rejected",
                       f"operator={operator} package={package_path} "
                       f"reason=target_newer target_score={age_info['target_score']} "
                       f"package_score={age_info['package_score']}")
                save_state(target_state, state_path)
            sys.exit(1)

    if mode == "takeover":
        if target_state is not None:
            _audit(package_state, "import_package_takeover",
                   f"operator={operator} package={package_path} "
                   f"exported_by={package.get('exported_by')} "
                   f"exported_at={package.get('exported_at')} "
                   f"old_version={target_state.get('version')} "
                   f"old_draft_v={target_state.get('draft_version')} "
                   f"new_version={package_state.get('version')} "
                   f"new_draft_v={package_state.get('draft_version')} "
                   f"force={args.force}")
        else:
            _audit(package_state, "import_package_takeover",
                   f"operator={operator} package={package_path} "
                   f"exported_by={package.get('exported_by')} "
                   f"exported_at={package.get('exported_at')} "
                   f"new_version={package_state.get('version')} "
                   f"new_draft_v={package_state.get('draft_version')} "
                   f"force={args.force}")
        final_state = package_state

    elif mode == "merge":
        if target_state is None:
            _audit(package_state, "import_package_merge",
                   f"operator={operator} package={package_path} "
                   f"exported_by={package.get('exported_by')} "
                   f"exported_at={package.get('exported_at')} "
                   f"note=no_target_creating_new "
                   f"version={package_state.get('version')} "
                   f"draft_v={package_state.get('draft_version')}")
            final_state = package_state
        else:
            merged = copy.deepcopy(package_state)
            merged["audit_log"] = target_state.get("audit_log", []) + [
                {"timestamp": datetime.now().isoformat(),
                 "action": "import_package_merge",
                 "detail": (f"operator={operator} package={package_path} "
                            f"exported_by={package.get('exported_by')} "
                            f"exported_at={package.get('exported_at')} "
                            f"target_audit_len={len(target_state.get('audit_log', []))} "
                            f"package_audit_len={len(package_state.get('audit_log', []))} "
                            f"force={args.force}")}
            ] + package_state.get("audit_log", [])

            if args.keep_target_batches:
                target_batches = set(target_state.get("imported_batches", []))
                package_batches = package_state.get("imported_batches", [])
                merged["imported_batches"] = list(target_batches) + [
                    b for b in package_batches if b not in target_batches
                ]

            _audit(merged, "import_package_merge",
                   f"operator={operator} package={package_path} "
                   f"exported_by={package.get('exported_by')} "
                   f"exported_at={package.get('exported_at')} "
                   f"keep_target_batches={args.keep_target_batches} "
                   f"force={args.force}")
            final_state = merged
    else:
        print(f"[ERROR] Unknown mode '{mode}'. Valid: merge, takeover")
        sys.exit(1)

    if package.get("rules_snapshot") and args.apply_rules_snapshot:
        rules_dir = os.path.dirname(args.rules)
        if not os.path.exists(rules_dir):
            os.makedirs(rules_dir, exist_ok=True)
        with open(args.rules, "w", encoding="utf-8") as f:
            f.write(package["rules_snapshot"])
        print(f"[INFO] Rules snapshot restored to {args.rules}")
        _audit(final_state, "rules_restored",
               f"from_package={package_path} operator={operator}")

    if mode == "takeover" and target_state_snapshot is not None:
        deep_diff = _compute_deep_diff(target_state_snapshot, package_state)
        takeover_snapshot = {
            "takeover_id": hashlib.sha256(
                f"{datetime.now().isoformat()}{operator}{package_path}".encode()
            ).hexdigest()[:16],
            "imported_at": datetime.now().isoformat(),
            "imported_by": operator,
            "import_pid": os.getpid(),
            "exported_by": package.get("exported_by"),
            "exported_at": package.get("exported_at"),
            "package_path": os.path.basename(package_path),
            "mode": mode,
            "force": args.force,
            "pre_import_state": target_state_snapshot,
            "post_import_state": copy.deepcopy(final_state),
            "diff": deep_diff,
            "resumed_across_restart": False,
        }
        final_state.setdefault("takeover_history", []).append(takeover_snapshot)
        _audit(final_state, "takeover_snapshot_stored",
               f"takeover_id={takeover_snapshot['takeover_id']} "
               f"operator={operator} mode={mode} "
               f"modified_items={len(deep_diff['items']['modified'])} "
               f"added_items={len(deep_diff['items']['added'])} "
               f"removed_items={len(deep_diff['items']['removed'])}")
    elif mode == "takeover" and target_state_snapshot is None:
        takeover_snapshot = {
            "takeover_id": hashlib.sha256(
                f"{datetime.now().isoformat()}{operator}{package_path}".encode()
            ).hexdigest()[:16],
            "imported_at": datetime.now().isoformat(),
            "imported_by": operator,
            "import_pid": os.getpid(),
            "exported_by": package.get("exported_by"),
            "exported_at": package.get("exported_at"),
            "package_path": os.path.basename(package_path),
            "mode": mode,
            "force": args.force,
            "pre_import_state": None,
            "post_import_state": copy.deepcopy(final_state),
            "diff": None,
            "note": "fresh_import_no_target_state",
            "resumed_across_restart": False,
        }
        final_state.setdefault("takeover_history", []).append(takeover_snapshot)

    save_state(final_state, state_path)

    print()
    print(f"[OK] Package imported successfully (mode={mode}).")
    print(f"   Final state:  v{final_state.get('version')} / draft v{final_state.get('draft_version')}")
    print(f"   Items:        {len(final_state.get('items', []))}")
    print(f"   Audit log:    {len(final_state.get('audit_log', []))} entries")
    if final_state.get("pending_bulk_ops"):
        unresolved = len([p for p in final_state["pending_bulk_ops"] if not p.get("resolved")])
        print(f"   Pending bulk: {unresolved} unresolved / {len(final_state['pending_bulk_ops'])} total")
    print()
    print(f"   Next steps:")
    print(f"     release_cli.py --state {state_path} status")
    print(f"     release_cli.py --state {state_path} history")
    if final_state.get("pending_bulk_ops"):
        unresolved_indices = [i for i, p in enumerate(final_state["pending_bulk_ops"]) if not p.get("resolved")]
        for idx in unresolved_indices:
            print(f"     release_cli.py --state {state_path} bulk_amend --resume {idx} --decision overwrite")


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

    takeover_history = state.get("takeover_history", [])
    if not takeover_history:
        print("[INFO] No takeover history found.")
        print("  (This state was never imported via import_package mode=takeover)")
        return

    current_pid = os.getpid()
    state_updated = False
    for takeover in takeover_history:
        if not takeover.get("resumed_across_restart", False):
            import_pid = takeover.get("import_pid")
            if import_pid is not None and import_pid != current_pid:
                takeover["resumed_across_restart"] = True
                state_updated = True
                _audit(state, "takeover_resumed_across_restart",
                       f"takeover_id={takeover['takeover_id']} "
                       f"import_pid={import_pid} current_pid={current_pid}")
    if state_updated:
        save_state(state, state_path)

    print("\n" + "=" * 70)
    print("AUDIT VIEW - Takeover Timeline")
    print("=" * 70)

    for idx, takeover in enumerate(reversed(takeover_history)):
        tid = takeover.get("takeover_id", "?")
        print(f"\n{'─' * 70}")
        print(f"Takeover #{len(takeover_history) - idx}: {tid}")
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

        print(f"\n  [Decisions]")
        print(f"    Decision maker: {takeover.get('imported_by')}")
        print(f"    Decision time:  {takeover.get('imported_at')}")
        if takeover.get('resumed_across_restart'):
            print(f"    Cross-restart:  YES (resumed after process restart)")
        else:
            print(f"    Cross-restart:  NO (same process session)")

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
        all_events.append({
            "ts": takeover.get("imported_at"),
            "type": "takeover",
            "action": "import_package_takeover",
            "detail": f"takeover_id={takeover.get('takeover_id')} by {takeover.get('imported_by')}",
            "resumed": takeover.get("resumed_across_restart", False),
        })

    all_events.sort(key=lambda x: x.get("ts", ""))

    print(f"  Total events: {len(all_events)}")
    print()
    for ev in all_events:
        tag = "[TAKEOVER]" if ev["type"] == "takeover" else "[AUDIT]"
        resumed_tag = " [RESUMED]" if ev.get("resumed") else ""
        print(f"  [{ev['ts']}] {tag}{resumed_tag} {ev['action']}: {ev['detail'][:80]}")

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


def main():
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
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args, rules)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
