#!/usr/bin/env python3
import argparse
import copy
import csv
import io
import json
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(SCRIPT_DIR, "rules.yaml")
DEFAULT_STATE = os.path.join(SCRIPT_DIR, ".release_state.json")

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
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args, rules)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
