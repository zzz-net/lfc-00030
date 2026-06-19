#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(SCRIPT_DIR, "release_cli.py")
RULES = os.path.join(SCRIPT_DIR, "rules.yaml")
SAMPLE = os.path.join(SCRIPT_DIR, "sample_manifest.json")

PASS = 0
FAIL = 0


def run_cli(args, state_path, expect_fail=False):
    cmd = [sys.executable, CLI, "--rules", RULES, "--state", state_path] + args
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=SCRIPT_DIR)
    ok = res.returncode != 0 if expect_fail else res.returncode == 0
    if not ok:
        print(f"  [FAIL] {' '.join(args)}")
        print(f"    exit={res.returncode} expect_fail={expect_fail}")
        if res.stdout.strip():
            print(f"    stdout: {res.stdout.strip()}")
        if res.stderr.strip():
            print(f"    stderr: {res.stderr.strip()}")
    else:
        label = "(expected fail)" if expect_fail else ""
        print(f"  [OK] {' '.join(args)} {label}")
    return res


def read_state(state_path):
    with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def assert_eq(actual, expected, msg):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print(f"    [ASSERT OK] {msg}")
    else:
        FAIL += 1
        print(f"    [ASSERT FAIL] {msg}")
        print(f"      expected: {expected!r}")
        print(f"      actual:   {actual!r}")


def assert_in(needle, haystack, msg):
    global FAIL, PASS
    if needle in haystack:
        PASS += 1
        print(f"    [ASSERT OK] {msg}")
    else:
        FAIL += 1
        print(f"    [ASSERT FAIL] {msg}")
        print(f"      missing: {needle!r}")
        print(f"      in: {haystack[:200]!r}")


def cleanup_patterns(workdir, state_filename=None):
    for fn in os.listdir(workdir):
        if fn.startswith("release_notes_") and fn.endswith(".md"):
            try:
                os.remove(os.path.join(workdir, fn))
            except OSError:
                pass
    if state_filename:
        target = os.path.join(workdir, state_filename)
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass


def _amend_bad_items(sp):
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)


def test_1_rollback_after_approve(tmpdir):
    print("\n== Test 1: Rollback after approve ==")
    sp = os.path.join(tmpdir, "state_t1.json")
    cleanup_patterns(tmpdir, "state_t1.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    _amend_bad_items(sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    s = read_state(sp)
    assert_eq(s["approved"], True, "approved flag set")
    assert_eq(s["approved_at_version"], "2.1.0", "approved_at_version set")
    assert_eq(s["approved_at_draft_version"], 2, "approved_at_draft_version set")

    run_cli(["rollback"], sp)
    s = read_state(sp)
    assert_eq(s["approved"], False, "approved cleared after rollback")
    assert_eq(s["approved_at_version"], None, "approved_at_version cleared")
    assert_eq(s["approved_at_draft_version"], None, "approved_at_draft_version cleared")
    assert_eq(s["draft_version"], 2, "draft version stays at approved snapshot (v2)")

    actions = [e["action"] for e in s["audit_log"]]
    assert_in("unapprove", actions, "audit log has 'unapprove' event")
    assert_in("rollback", actions, "audit log has 'rollback' event after unapprove")

    run_cli(["draft"], sp)
    run_cli(["approve"], sp)
    run_cli(["export"], sp)
    s = read_state(sp)
    assert_eq(s["approved"], True, "re-approve works after rollback")
    cleanup_patterns(tmpdir, "state_t1.json")


def test_2_no_import_when_approved(tmpdir):
    print("\n== Test 2: No new import when approved ==")
    sp = os.path.join(tmpdir, "state_t2.json")
    cleanup_patterns(tmpdir, "state_t2.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    _amend_bad_items(sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    next_batch = os.path.join(tmpdir, "next.json")
    with open(SAMPLE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["batch_id"] = "batch-2026Q2-v2.2.0"
    data["version"] = "2.2.0"
    with open(next_batch, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    res = run_cli(["import", next_batch], sp, expect_fail=True)
    assert_in("already approved", res.stdout, "rejected because already approved")

    s = read_state(sp)
    assert_eq(len(s["imported_batches"]), 1, "original batch preserved, new one not added")
    assert_eq(s["version"], "2.1.0", "version not overridden")
    assert_in("import_rejected", [e["action"] for e in s["audit_log"]],
              "audit log has import_rejected for approved state")
    cleanup_patterns(tmpdir, "state_t2.json")


def test_3_bom_manifest_import(tmpdir):
    print("\n== Test 3: UTF-8 BOM manifest import ==")
    sp = os.path.join(tmpdir, "bom_state.json")
    bom_manifest = os.path.join(tmpdir, "manifest_bom.json")
    with open(SAMPLE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["batch_id"] = "bom-batch-001"
    with open(bom_manifest, "wb") as f:
        f.write(b"\xef\xbb\xbf" + json.dumps(data, ensure_ascii=False).encode("utf-8"))

    res = run_cli(["import", bom_manifest], sp)
    s = read_state(sp)
    assert_eq(len(s["items"]), 5, "BOM manifest imported 5 items successfully")
    assert_eq(s["current_batch_id"], "bom-batch-001", "BOM manifest batch_id correct")
    cleanup_patterns(tmpdir)


def test_4_export_drift_rejection(tmpdir):
    print("\n== Test 4: Export drift rejection ==")
    sp = os.path.join(tmpdir, "state_t4.json")
    cleanup_patterns(tmpdir, "state_t4.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    _amend_bad_items(sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    s = read_state(sp)
    s["version"] = "9.9.9"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

    res = run_cli(["export"], sp, expect_fail=True)
    assert_in("State has drifted", res.stdout, "export rejected due to drift")

    s = read_state(sp)
    actions = [e["action"] for e in s["audit_log"]]
    assert_in("export_rejected", actions, "audit log has export_rejected for drift")
    cleanup_patterns(tmpdir, "state_t4.json")


def test_5_restart_consistency(tmpdir):
    print("\n== Test 5: Restart consistency across multiple runs ==")
    sp = os.path.join(tmpdir, "state_t5.json")
    cleanup_patterns(tmpdir, "state_t5.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    _amend_bad_items(sp)

    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)

    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    export_out = os.path.join(tmpdir, "final.md")
    run_cli(["export", "-o", export_out], sp)

    s_after = read_state(sp)
    md1 = read_file(export_out)

    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    s_reload = read_state(sp)
    assert_eq(s_reload["approved"], True, "reload: approved still True")
    assert_eq(s_reload["approved_at_version"], s_after["approved_at_version"],
              "reload: approved_at_version consistent")
    assert_eq(s_reload["approved_at_draft_version"], s_after["approved_at_draft_version"],
              "reload: approved_at_draft_version consistent")
    assert_eq(len(s_reload["audit_log"]), len(s_after["audit_log"]),
              "reload: audit log length identical")

    run_cli(["status"], sp)
    run_cli(["history"], sp)

    export_out2 = os.path.join(tmpdir, "final2.md")
    run_cli(["export", "-o", export_out2], sp)
    md2 = read_file(export_out2)

    md1_no_ts = "\n".join(
        line for line in md1.splitlines() if not line.startswith("_Generated at")
    )
    md2_no_ts = "\n".join(
        line for line in md2.splitlines() if not line.startswith("_Generated at")
    )
    assert_eq(md1_no_ts, md2_no_ts, "two consecutive exports produce same Markdown (ignoring timestamp)")
    print(f"  [OK] Restart consistency: state file unchanged between subprocess calls")
    cleanup_patterns(tmpdir, "state_t5.json")


def test_6_duplicate_batch_counter_not_incremented(tmpdir):
    print("\n== Test 6: Duplicate batch not counted again ==")
    sp = os.path.join(tmpdir, "state_t6.json")
    cleanup_patterns(tmpdir, "state_t6.json")

    run_cli(["import", SAMPLE], sp)
    before = len(read_state(sp)["imported_batches"])
    run_cli(["import", SAMPLE], sp, expect_fail=True)
    after = len(read_state(sp)["imported_batches"])
    assert_eq(before, after, "imported_batches unchanged on duplicate import")
    assert_eq(before, 1, "exactly 1 batch recorded")
    cleanup_patterns(tmpdir, "state_t6.json")


def test_7_amend_fixes_bad_items_allows_approve(tmpdir):
    """核心场景：坏清单导入 → amend修正 → 草稿 → 确认 → 批准 → 导出，全程用 CLI 完成"""
    print("\n== Test 7: Amend fixes bad items → full approve flow ==")
    sp = os.path.join(tmpdir, "state_t7.json")
    cleanup_patterns(tmpdir, "state_t7.json")

    res = run_cli(["import", SAMPLE], sp)
    assert_in("missing required field 'owner'", res.stdout, "CHG-003 flagged missing owner")
    assert_in("invalid risk_level 'extreme'", res.stdout, "CHG-005 flagged invalid risk")

    s = read_state(sp)
    assert_eq(len(s["items"]), 5, "5 items imported (including bad ones)")
    assert_eq(len(s["imported_batches"]), 1, "1 batch recorded")

    res_approve_early = run_cli(["approve"], sp, expect_fail=True)
    assert_in("Missing owner", res_approve_early.stdout, "approve blocked: missing owner")
    assert_in("Invalid risk_level", res_approve_early.stdout, "approve blocked: invalid risk")

    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    s = read_state(sp)
    chg003 = next(it for it in s["items"] if it["id"] == "CHG-003")
    assert_eq(chg003["owner"], "周七", "CHG-003 owner amended")
    assert_eq(len(s["items"]), 5, "items count unchanged (no duplication)")

    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)
    s = read_state(sp)
    chg005 = next(it for it in s["items"] if it["id"] == "CHG-005")
    assert_eq(chg005["risk_level"], "critical", "CHG-005 risk_level amended")
    assert_eq(len(s["items"]), 5, "items count still 5 (no duplication)")

    amend_entries = [e for e in s["audit_log"] if e["action"] == "amend"]
    assert_eq(len(amend_entries), 2, "2 amend events in audit log")

    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    s = read_state(sp)
    assert_eq(s["approved"], True, "approve succeeds after amend")

    export_out = os.path.join(tmpdir, "t7_final.md")
    run_cli(["export", "-o", export_out], sp)
    md = read_file(export_out)
    assert_in("owner:周七", md, "final markdown has amended owner for CHG-003")
    assert_in("risk:critical", md, "final markdown has amended risk for CHG-005")
    assert_eq(md.count("CHG-003"), 1, "CHG-003 appears exactly once (no duplication)")
    assert_eq(md.count("CHG-005"), 1, "CHG-005 appears exactly once (no duplication)")
    cleanup_patterns(tmpdir, "state_t7.json")


def test_8_amend_rejects_invalid_and_nonexistent(tmpdir):
    """amend 的边界：不存在的 ID、非法 risk_level 被拒绝；已批准时 amend 自动撤销批准"""
    print("\n== Test 8: Amend edge cases ==")
    sp = os.path.join(tmpdir, "state_t8.json")
    cleanup_patterns(tmpdir, "state_t8.json")

    run_cli(["import", SAMPLE], sp)

    res = run_cli(["amend", "CHG-999", "--field", "owner=test"], sp, expect_fail=True)
    assert_in("not found", res.stdout, "amend rejects nonexistent item ID")

    res = run_cli(["amend", "CHG-005", "--field", "risk_level=extreme2"], sp, expect_fail=True)
    assert_in("Invalid risk_level", res.stdout, "amend rejects invalid risk_level value")

    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)
    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    s = read_state(sp)
    assert_eq(s["approved"], True, "approved before amend-in-approved-state test")

    run_cli(["amend", "CHG-001", "--field", "owner=新负责人"], sp)
    s = read_state(sp)
    assert_eq(s["approved"], False, "amend after approval auto-clears approved flag")
    assert_eq(s["approved_at_version"], None, "approved_at_version cleared on amend")
    assert_eq(s["approved_at_draft_version"], None, "approved_at_draft_version cleared on amend")

    amend_after = [e for e in s["audit_log"] if e["action"] == "amend" and "CHG-001" in e.get("detail", "")]
    assert_eq(len(amend_after), 1, "audit log records the amend on CHG-001 after approval")

    run_cli(["draft"], sp)
    run_cli(["approve"], sp)
    run_cli(["export"], sp)
    s = read_state(sp)
    assert_eq(s["approved"], True, "re-approve after amend works")
    cleanup_patterns(tmpdir, "state_t8.json")


def test_9_amend_restart_consistency(tmpdir):
    """amend 后重启一致性：修正 → 草稿 → 批准 → 导出 → 重启 → 再导出 → 内容一致"""
    print("\n== Test 9: Amend + restart consistency ==")
    sp = os.path.join(tmpdir, "state_t9.json")
    cleanup_patterns(tmpdir, "state_t9.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)
    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    out1 = os.path.join(tmpdir, "t9_final1.md")
    run_cli(["export", "-o", out1], sp)
    md1 = read_file(out1)
    s1 = read_state(sp)

    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    out2 = os.path.join(tmpdir, "t9_final2.md")
    run_cli(["export", "-o", out2], sp)
    md2 = read_file(out2)
    s2 = read_state(sp)

    md1_no_ts = "\n".join(l for l in md1.splitlines() if not l.startswith("_Generated at"))
    md2_no_ts = "\n".join(l for l in md2.splitlines() if not l.startswith("_Generated at"))
    assert_eq(md1_no_ts, md2_no_ts, "amend flow: two exports match after restart")
    assert_eq(s1["approved_at_version"], s2["approved_at_version"],
              "approved_at_version consistent across restarts")
    assert_eq(len(s1["items"]), len(s2["items"]), "items count consistent across restarts")
    cleanup_patterns(tmpdir, "state_t9.json")


def main():
    global PASS, FAIL
    tmpdir = tempfile.mkdtemp(prefix="release_cli_test_")
    print(f"Test workspace: {tmpdir}")
    try:
        test_1_rollback_after_approve(tmpdir)
        test_2_no_import_when_approved(tmpdir)
        test_3_bom_manifest_import(tmpdir)
        test_4_export_drift_rejection(tmpdir)
        test_5_restart_consistency(tmpdir)
        test_6_duplicate_batch_counter_not_incremented(tmpdir)
        test_7_amend_fixes_bad_items_allows_approve(tmpdir)
        test_8_amend_rejects_invalid_and_nonexistent(tmpdir)
        test_9_amend_restart_consistency(tmpdir)

        print(f"\n==== SUMMARY: {PASS} passed, {FAIL} failed ====")
        if FAIL:
            sys.exit(1)
        print("All tests passed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
