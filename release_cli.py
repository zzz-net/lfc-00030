#!/usr/bin/env python3
import argparse
import copy
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
    }


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
    state["items"].extend(new_items)
    state["migration_reminders"].extend(manifest.get("migration_reminders", []))
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

    md = _render_markdown(state)
    state["draft_version"] += 1
    dv = state["draft_version"]

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

    _audit(state, "draft", f"generated draft v{dv}")
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

    if state.get("approved"):
        state["approved"] = False
        state["approved_at_version"] = None
        state["approved_at_draft_version"] = None
        print(f"  [NOTE] Approval cleared (data changed after approval).")

    _audit(state, "amend", f"item={item_id} fields={changes}")
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
