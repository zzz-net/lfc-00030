#!/usr/bin/env python3
import copy
import csv
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


def test_10_bulk_no_conflict_json_and_csv(tmpdir):
    """无冲突场景：JSON 和 CSV 两种补丁格式都能正常批量修订"""
    print("\n== Test 10: Bulk amend no-conflict (JSON & CSV) ==")
    sp = os.path.join(tmpdir, "state_t10.json")
    cleanup_patterns(tmpdir, "state_t10.json")

    run_cli(["import", SAMPLE], sp)

    json_patch = os.path.join(tmpdir, "patch_t10.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t10-json-001",
            "operator": "产品经理A",
            "reason": "Q2负责人回填+风险重评",
            "items": [
                {"id": "CHG-003", "owner": "周七", "risk_level": "high", "category": "removal"},
                {"id": "CHG-005", "owner": "赵六", "risk_level": "critical", "category": "security"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "skip", "--operator", "产品经理A",
                   "--reason", "Q2负责人回填+风险重评"], sp)
    assert_in("Applied: 2", res.stdout, "json patch applied 2 items")
    assert_in("Skipped: 0", res.stdout, "json patch skipped 0 items")

    s = read_state(sp)
    chg003 = next(it for it in s["items"] if it["id"] == "CHG-003")
    assert_eq(chg003["owner"], "周七", "t10 CHG-003 owner updated via json bulk")
    assert_eq(chg003["risk_level"], "high", "t10 CHG-003 risk_level updated via json bulk")
    chg005 = next(it for it in s["items"] if it["id"] == "CHG-005")
    assert_eq(chg005["risk_level"], "critical", "t10 CHG-005 risk_level updated via json bulk")
    assert_eq(chg005["_last_modified_by"], "产品经理A", "t10 CHG-005 operator recorded in _last_modified_by")

    bulk_events = [e for e in s["audit_log"] if e["action"] == "bulk_amend_applied"]
    assert_eq(len(bulk_events), 1, "t10 audit has 1 bulk_amend_applied event")
    assert_in("产品经理A", bulk_events[0]["detail"], "t10 audit records operator")

    csv_patch = os.path.join(tmpdir, "patch_t10.csv")
    with open(csv_patch, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "owner", "category"])
        w.writerow(["CHG-001", "新张三A", "feature"])
        w.writerow(["CHG-002", "新李四B", "bugfix"])

    res2 = run_cli(["bulk_amend", csv_patch, "--mode", "skip", "--operator", "运营专员B"], sp)
    assert_in("Applied: 2", res2.stdout, "csv patch applied 2 items")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "新张三A", "t10 CHG-001 owner updated via csv bulk")
    chg002 = next(it for it in s["items"] if it["id"] == "CHG-002")
    assert_eq(chg002["owner"], "新李四B", "t10 CHG-002 owner updated via csv bulk")
    cleanup_patterns(tmpdir, "state_t10.json")


def test_11_bulk_conflict_mode_abort(tmpdir):
    """冲突场景：mode=abort 遇到冲突立即中止整个批次"""
    print("\n== Test 11: Bulk amend conflict -> mode=abort ==")
    sp = os.path.join(tmpdir, "state_t11.json")
    cleanup_patterns(tmpdir, "state_t11.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["confirm", "overview"], sp)
    run_cli(["confirm", "changes"], sp)

    json_patch = os.path.join(tmpdir, "patch_t11.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t11-abort-001",
            "operator": "tester11",
            "items": [
                {"id": "CHG-001", "owner": "变更A"},
                {"id": "CHG-002", "owner": "变更B"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "abort", "--operator", "tester11"],
                  sp, expect_fail=True)
    assert_in("ABORT", res.stdout, "t11 mode=abort reports ABORT")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "张三", "t11 CHG-001 unchanged after abort")
    events = [e for e in s["audit_log"] if e["action"] == "bulk_amend_aborted"]
    assert_eq(len(events), 1, "t11 audit has bulk_amend_aborted event")
    cleanup_patterns(tmpdir, "state_t11.json")


def test_12_bulk_conflict_mode_skip(tmpdir):
    """冲突场景：mode=skip 跳过冲突项，其余正常应用"""
    print("\n== Test 12: Bulk amend conflict -> mode=skip ==")
    sp = os.path.join(tmpdir, "state_t12.json")
    cleanup_patterns(tmpdir, "state_t12.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["amend", "CHG-001", "--field", "owner=先改了", "--operator", "test12-prev"], sp)

    json_patch = os.path.join(tmpdir, "patch_t12.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t12-skip-001",
            "operator": "tester12",
            "items": [
                {"id": "CHG-001", "owner": "冲突A"},
                {"id": "CHG-003", "owner": "周七", "risk_level": "critical"},
                {"id": "CHG-005", "risk_level": "high"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "skip", "--operator", "tester12"], sp)
    assert_in("already modified by", res.stdout, "t12 reports already_modified conflict for CHG-001")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "先改了", "t12 CHG-001 (conflicted) unchanged")
    chg003 = next(it for it in s["items"] if it["id"] == "CHG-003")
    assert_eq(chg003["owner"], "周七", "t12 CHG-003 (no conflict) owner updated")
    chg005 = next(it for it in s["items"] if it["id"] == "CHG-005")
    assert_eq(chg005["risk_level"], "high", "t12 CHG-005 (no conflict) risk updated")
    cleanup_patterns(tmpdir, "state_t12.json")


def test_13_bulk_conflict_mode_overwrite(tmpdir):
    """冲突场景：mode=overwrite 显式覆盖所有冲突"""
    print("\n== Test 13: Bulk amend conflict -> mode=overwrite ==")
    sp = os.path.join(tmpdir, "state_t13.json")
    cleanup_patterns(tmpdir, "state_t13.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["confirm", "overview"], sp)
    run_cli(["confirm", "changes"], sp)

    json_patch = os.path.join(tmpdir, "patch_t13.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t13-ow-001",
            "operator": "tester13",
            "items": [
                {"id": "CHG-001", "owner": "覆盖后的张三", "category": "feature"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "overwrite", "--operator", "tester13",
                   "--reason", "强制覆盖纠正负责人"], sp)

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "覆盖后的张三", "t13 CHG-001 force-overwrote conflicted field")
    assert_eq(s["confirmations"]["changes"], False, "t13 changes section confirmation invalidated after overwrite")
    assert_in("changes", res.stdout, "t13 reports invalidated sections")
    bulk_ev = [e for e in s["audit_log"] if e["action"] == "bulk_amend_applied"]
    assert_eq(len(bulk_ev), 1, "t13 audit records bulk_amend_applied for overwrite")
    assert_in("强制覆盖纠正负责人", bulk_ev[0]["detail"], "t13 reason recorded in audit")
    cleanup_patterns(tmpdir, "state_t13.json")


def test_14_section_confirmed_detection(tmpdir):
    """章节确认冲突：changes/migration/known_issues 已确认时的冲突检测"""
    print("\n== Test 14: Section-confirmed conflict detection ==")
    sp = os.path.join(tmpdir, "state_t14.json")
    cleanup_patterns(tmpdir, "state_t14.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)

    json_patch = os.path.join(tmpdir, "patch_t14.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t14-sec-001",
            "operator": "tester14",
            "items": [{"id": "CHG-001", "owner": "X"}],
            "migration_reminders": [{"id": "MIG-001", "title": "新标题A"}],
            "known_issues": [{"id": "KI-001", "workaround": "新临时方案"}],
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "overwrite", "--operator", "tester14"], sp)

    s = read_state(sp)
    assert_eq(s["confirmations"]["changes"], False, "t14 changes invalidated after item patch")
    assert_eq(s["confirmations"]["migration"], False, "t14 migration invalidated after mig patch")
    assert_eq(s["confirmations"]["known_issues"], False, "t14 known_issues invalidated after ki patch")
    assert_eq(s["known_issues_reviewed"], False, "t14 known_issues_reviewed reset")
    mig001 = next(m for m in s["migration_reminders"] if m["id"] == "MIG-001")
    assert_eq(mig001["title"], "新标题A", "t14 MIG-001 title updated")
    ki001 = next(k for k in s["known_issues"] if k["id"] == "KI-001")
    assert_eq(ki001["workaround"], "新临时方案", "t14 KI-001 workaround updated")
    cleanup_patterns(tmpdir, "state_t14.json")


def test_15_already_modified_conflict(tmpdir):
    """already_modified 冲突：条目已被单独 amend 修改过"""
    print("\n== Test 15: already_modified conflict (item previously amended) ==")
    sp = os.path.join(tmpdir, "state_t15.json")
    cleanup_patterns(tmpdir, "state_t15.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["amend", "CHG-001", "--field", "owner=先改的人", "--operator", "张三先改"], sp)

    json_patch = os.path.join(tmpdir, "patch_t15.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t15-am-001",
            "operator": "后批量改的人",
            "items": [
                {"id": "CHG-001", "owner": "批量值"},
                {"id": "CHG-002", "owner": "批量值B"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "skip"], sp)
    assert_in("already modified by", res.stdout, "t15 reports already_modified conflict")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "先改的人", "t15 CHG-001 preserved (conflicted, skipped)")
    chg002 = next(it for it in s["items"] if it["id"] == "CHG-002")
    assert_eq(chg002["owner"], "批量值B", "t15 CHG-002 applied (no conflict)")
    cleanup_patterns(tmpdir, "state_t15.json")


def test_16_restart_conflict_resume(tmpdir):
    """重启恢复冲突：interactive 模式下冲突持久化，重启后 resume 决策"""
    print("\n== Test 16: Restart conflict persistence + resume ==")
    sp = os.path.join(tmpdir, "state_t16.json")
    cleanup_patterns(tmpdir, "state_t16.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["confirm", "changes"], sp)

    json_patch = os.path.join(tmpdir, "patch_t16.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t16-resume-001",
            "operator": "先发起者",
            "items": [
                {"id": "CHG-001", "owner": "批值A"},
                {"id": "CHG-002", "owner": "批值B"},
                {"id": "CHG-999", "owner": "不存在"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res1 = run_cli(["bulk_amend", json_patch, "--operator", "先发起者",
                    "--reason", "待主管决定"], sp, expect_fail=True)
    assert_in("Conflict info persisted", res1.stdout, "t16 interactive persists conflicts")
    assert_in("--resume 0", res1.stdout, "t16 shows resume index 0")

    s_before = read_state(sp)
    pending = s_before.get("pending_bulk_ops", [])
    assert_eq(len(pending), 1, "t16 1 pending_bulk_ops entry persisted")
    assert_eq(pending[0]["resolved"], False, "t16 pending entry is unresolved")
    assert_eq(pending[0]["patch_snapshot"]["patch_id"], "t16-resume-001",
              "t16 pending snapshot has patch_id")

    chg001_before = next(it for it in s_before["items"] if it["id"] == "CHG-001")
    assert_eq(chg001_before["owner"], "张三", "t16 CHG-001 still unchanged before resume")

    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    s_reload = read_state(sp)
    pending2 = s_reload.get("pending_bulk_ops", [])
    assert_eq(len(pending2), 1, "t16 pending_bulk_ops preserved across reload (restart)")
    assert_eq(pending2[0]["resolved"], False, "t16 pending still unresolved after reload")

    res_resume = run_cli(["bulk_amend", "--resume", "0", "--decision", "overwrite",
                          "--operator", "主管决定", "--per-item", "CHG-002=skip"], sp)

    s_after = read_state(sp)
    chg001 = next(it for it in s_after["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "批值A", "t16 CHG-001 overwritten via default=overwrite")
    chg002 = next(it for it in s_after["items"] if it["id"] == "CHG-002")
    assert_eq(chg002["owner"], "李四", "t16 CHG-002 skipped via per-item override")

    pending_after = s_after["pending_bulk_ops"]
    assert_eq(pending_after[0]["resolved"], True, "t16 pending marked resolved after resume")
    assert_eq(pending_after[0]["resolved_operator"], "主管决定",
              "t16 resolved_operator recorded")
    assert_in("final_decisions", pending_after[0], "t16 final_decisions stored")

    bulk_events = [e for e in s_after["audit_log"] if e["action"] == "bulk_amend_applied"
                   and "resumed=true" in e["detail"]]
    assert_eq(len(bulk_events), 1, "t16 audit records resumed bulk apply")
    cleanup_patterns(tmpdir, "state_t16.json")


def test_17_full_e2e_bulk_workflow(tmpdir):
    """完整端到端：导入→批量修订→重生draft→重新确认→批准→导出，验证最终结果"""
    print("\n== Test 17: Full E2E bulk amend workflow ==")
    sp = os.path.join(tmpdir, "state_t17.json")
    cleanup_patterns(tmpdir, "state_t17.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)

    json_patch = os.path.join(tmpdir, "patch_t17.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t17-e2e-001",
            "operator": "发布经理X",
            "reason": "发布前统一回填owner/risk/category+补充迁移提醒",
            "items": [
                {"id": "CHG-003", "owner": "周七", "risk_level": "critical", "category": "removal"},
                {"id": "CHG-005", "owner": "赵六", "risk_level": "critical", "category": "security"},
                {"id": "CHG-002", "owner": "李四", "risk_level": "high", "category": "bugfix"},
            ],
            "migration_reminders": [
                {"id": "MIG-002", "action_required": "下载模板v2并补充 role/department 两列"},
            ],
            "known_issues": [
                {"id": "KI-001", "description": "Safari 17以下批量导入进度条不刷新（仅UI，不影响功能）"},
            ],
        }, f, ensure_ascii=False, indent=2)

    res_bulk = run_cli(["bulk_amend", json_patch, "--mode", "overwrite",
                        "--operator", "发布经理X", "--reason", "发布前统一回填"], sp)
    assert_in("Applied: 4", res_bulk.stdout,
              "t17 applied 4 entries (CHG-002 no_change auto-skipped, 2 other items+1 mig+1 ki=4)")
    assert_in("No-op", res_bulk.stdout, "t17 reports no-change for CHG-002")
    assert_in("invalidated", res_bulk.stdout, "t17 reports invalidated sections")

    s = read_state(sp)
    assert_eq(s["confirmations"]["changes"], False, "t17 changes confirmation invalidated")
    assert_eq(s["confirmations"]["migration"], False, "t17 migration confirmation invalidated")
    assert_eq(s["confirmations"]["known_issues"], False, "t17 known_issues confirmation invalidated")

    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)

    s = read_state(sp)
    assert_eq(s["approved"], True, "t17 approve succeeds after bulk amend + re-confirm")

    export_out = os.path.join(tmpdir, "t17_final.md")
    run_cli(["export", "-o", export_out], sp)
    md = read_file(export_out)

    assert_in("owner:周七", md, "t17 final md: CHG-003 amended owner=周七")
    assert_in("owner:赵六", md, "t17 final md: CHG-005 amended owner=赵六")
    assert_in("risk:critical", md, "t17 final md: critical risks present")
    assert_in("下载模板v2并补充 role/department 两列", md, "t17 final md: updated migration reminder")
    assert_in("Safari 17以下批量导入进度条不刷新", md, "t17 final md: updated known issue description")

    bulk_applied = [e for e in s["audit_log"] if e["action"] == "bulk_amend_applied"]
    assert_eq(len(bulk_applied), 1, "t17 audit has bulk_amend_applied")
    assert_in("发布经理X", bulk_applied[0]["detail"], "t17 audit records operator")

    status_res = run_cli(["status"], sp)
    assert_in("Items:         5", status_res.stdout, "t17 status shows 5 items")
    assert_in("Approved:      True", status_res.stdout, "t17 status shows approved=True")

    hist_res = run_cli(["history"], sp)
    assert_in("bulk_amend_applied", hist_res.stdout, "t17 history shows bulk event")
    assert_in("import", hist_res.stdout, "t17 history shows import event")
    assert_in("approved", hist_res.stdout, "t17 history shows approve event")
    cleanup_patterns(tmpdir, "state_t17.json")


def test_18_bulk_draft_newer_conflict(tmpdir):
    """draft_newer 冲突：补丁基于旧版本生成，当前 draft 已更新"""
    print("\n== Test 18: draft_newer conflict detection ==")
    sp = os.path.join(tmpdir, "state_t18.json")
    cleanup_patterns(tmpdir, "state_t18.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)

    json_patch = os.path.join(tmpdir, "patch_t18.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t18-dn-001",
            "operator": "tester18",
            "based_on_draft_version": 0,
            "items": [
                {"id": "CHG-001", "owner": "基于旧草稿的值"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "skip", "--operator", "tester18"], sp)
    assert_in("draft_newer", res.stdout or "", "t18 reports draft_newer conflict")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    assert_eq(chg001["owner"], "张三", "t18 CHG-001 preserved (draft_newer skipped)")
    cleanup_patterns(tmpdir, "state_t18.json")


def test_19_bulk_invalid_risk_rejected(tmpdir):
    """补丁风险级别非法时整体拒绝"""
    print("\n== Test 19: Bulk amend invalid risk validation ==")
    sp = os.path.join(tmpdir, "state_t19.json")
    cleanup_patterns(tmpdir, "state_t19.json")

    run_cli(["import", SAMPLE], sp)

    json_patch = os.path.join(tmpdir, "patch_t19.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t19-val-001",
            "operator": "tester19",
            "items": [
                {"id": "CHG-001", "risk_level": "extreme_invalid"},
            ]
        }, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", json_patch, "--mode", "skip"], sp, expect_fail=True)
    assert_in("REJECTED", res.stdout, "t19 rejected due to validation error")
    assert_in("invalid risk_level", res.stdout, "t19 reports invalid risk_level")

    s = read_state(sp)
    rej_events = [e for e in s["audit_log"] if e["action"] == "bulk_amend_rejected"]
    assert_eq(len(rej_events), 1, "t19 audit has bulk_amend_rejected event")
    cleanup_patterns(tmpdir, "state_t19.json")


def test_20_resume_abort_per_item(tmpdir):
    """resume + per-item=abort 导致整批中止"""
    print("\n== Test 20: Resume per-item=abort cancels batch ==")
    sp = os.path.join(tmpdir, "state_t20.json")
    cleanup_patterns(tmpdir, "state_t20.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["confirm", "changes"], sp)

    json_patch = os.path.join(tmpdir, "patch_t20.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t20-abort-001",
            "operator": "tester20",
            "items": [
                {"id": "CHG-001", "owner": "A"},
                {"id": "CHG-002", "owner": "B"},
            ]
        }, f, ensure_ascii=False, indent=2)

    run_cli(["bulk_amend", json_patch, "--operator", "tester20"], sp, expect_fail=True)

    res_resume = run_cli(["bulk_amend", "--resume", "0", "--decision", "overwrite",
                          "--operator", "主管", "--per-item", "CHG-001=abort"],
                         sp, expect_fail=True)
    assert_in("ABORT", res_resume.stdout, "t20 per-item=abort triggers whole-batch abort")

    s = read_state(sp)
    chg001 = next(it for it in s["items"] if it["id"] == "CHG-001")
    chg002 = next(it for it in s["items"] if it["id"] == "CHG-002")
    assert_eq(chg001["owner"], "张三", "t20 CHG-001 unchanged after abort")
    assert_eq(chg002["owner"], "李四", "t20 CHG-002 unchanged after abort")
    abort_events = [e for e in s["audit_log"] if e["action"] == "bulk_amend_aborted"
                    and "resumed=true" in e["detail"]]
    assert_eq(len(abort_events), 1, "t20 audit records resumed aborted event")
    assert_eq(s["pending_bulk_ops"][0]["resolved"], True, "t20 pending still marked resolved even on abort")
    cleanup_patterns(tmpdir, "state_t20.json")


def test_21_draft_version_alignment(tmpdir):
    """Bug regression: Markdown 文末 Draft 版本号、state.draft_version、snapshot.version 必须严格对齐"""
    print("\n== Test 21: Draft version alignment (Markdown <-> state <-> snapshot <-> history) ==")
    sp = os.path.join(tmpdir, "state_t21.json")
    cleanup_patterns(tmpdir, "state_t21.json")

    run_cli(["import", SAMPLE], sp)

    for expected_v in [1, 2, 3]:
        out = os.path.join(tmpdir, f"t21_draft_v{expected_v}.md")
        run_cli(["draft", "-o", out], sp)
        md = read_file(out)
        s = read_state(sp)
        draft_events = [e for e in s["audit_log"] if e["action"] == "draft_generated"]
        last_audit_v = None
        if draft_events:
            last_detail = draft_events[-1]["detail"]
            import re as _re
            m = _re.search(r"draft_v=(\d+)", last_detail)
            if m:
                last_audit_v = int(m.group(1))
        assert_eq(s["draft_version"], expected_v,
                  f"t21 state.draft_version == {expected_v} after {expected_v}th draft")
        assert_in(f"Draft v{expected_v}_", md,
                  f"t21 Markdown ends with Draft v{expected_v}")
        snap = s["drafts"][-1]
        assert_eq(snap["version"], expected_v,
                  f"t21 drafts[-1].version == {expected_v} (snapshot aligns)")
        if last_audit_v is not None:
            assert_eq(last_audit_v, expected_v,
                      f"t21 audit draft_v == {expected_v} (history aligns)")

    json_patch = os.path.join(tmpdir, "patch_t21.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t21-bulk",
            "operator": "t21-op",
            "items": [{"id": "CHG-003", "owner": "周七"}]
        }, f, ensure_ascii=False, indent=2)
    run_cli(["bulk_amend", json_patch, "--mode", "skip", "--operator", "t21-op"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical", "--operator", "t21-op"], sp)

    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)

    v4out = os.path.join(tmpdir, "t21_draft_v4.md")
    run_cli(["draft", "-o", v4out], sp)
    s = read_state(sp)
    md4 = read_file(v4out)
    assert_eq(s["draft_version"], 4, "t21 draft_version == 4 after bulk amend + re-draft")
    assert_in("Draft v4_", md4, "t21 post-bulk Markdown Draft v4")
    assert_eq(s["drafts"][-1]["version"], 4, "t21 post-bulk snapshot.version == 4")

    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    s_reload = read_state(sp)
    assert_eq(s_reload["draft_version"], 4, "t21 after reload draft_version still 4")
    run_cli(["approve"], sp)
    export_out = os.path.join(tmpdir, "t21_final.md")
    run_cli(["export", "-o", export_out], sp)
    s_final = read_state(sp)
    assert_eq(s_final["approved_at_draft_version"], 4,
              "t21 approved_at_draft_version == 4 (matches last draft v4)")
    cleanup_patterns(tmpdir, "state_t21.json")


def test_22_resume_missing_evidence_aborts(tmpdir):
    """Bug regression: resume 时决策证据（conflicts_snapshot）缺失 → 应中止，不得默认 apply 提交"""
    print("\n== Test 22: Resume missing decision evidence MUST abort (no silent apply) ==")
    sp = os.path.join(tmpdir, "state_t22.json")
    cleanup_patterns(tmpdir, "state_t22.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["amend", "CHG-001", "--field", "owner=先改了A", "--operator", "t22-prev"], sp)
    run_cli(["draft"], sp)
    run_cli(["confirm", "changes"], sp)

    json_patch = os.path.join(tmpdir, "patch_t22.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t22-ev",
            "operator": "t22-init",
            "items": [
                {"id": "CHG-001", "owner": "冲突A"},
                {"id": "CHG-002", "owner": "冲突B"},
            ]
        }, f, ensure_ascii=False, indent=2)

    run_cli(["bulk_amend", json_patch, "--operator", "t22-init"], sp, expect_fail=True)

    s = read_state(sp)
    pending = s["pending_bulk_ops"][0]
    original_conflicts = pending["conflicts_snapshot"]
    assert_eq(len(original_conflicts), 2,
              "t22 setup: CHG-001 and CHG-002 both conflict (section_confirmed or already_modified)")

    chg002_before = next(it for it in s["items"] if it["id"] == "CHG-002")
    orig_owner_002 = chg002_before["owner"]

    pending["conflicts_snapshot"] = [
        c for c in original_conflicts if not (c["target_type"] == "item" and c["id"] == "CHG-002")
    ]
    assert_eq(len(pending["conflicts_snapshot"]), 1,
              "t22 simulate corruption: removed CHG-002 from conflicts_snapshot (evidence lost)")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

    res = run_cli(["bulk_amend", "--resume", "0", "--decision", "overwrite",
                   "--operator", "t22-resumer"], sp, expect_fail=True)
    assert_in("missing decision evidence", (res.stdout or "").lower() + " " + (res.stderr or "").lower(),
              "t22 resume aborts with 'missing decision evidence' when CHG-002 has no decision record")

    s_after = read_state(sp)
    chg002_after = next(it for it in s_after["items"] if it["id"] == "CHG-002")
    assert_eq(chg002_after["owner"], orig_owner_002,
              "t22 CHG-002 owner UNCHANGED after evidence-missing abort (not silently applied)")

    pending_after = s_after["pending_bulk_ops"][0]
    assert_eq(pending_after["resolved"], False,
              "t22 pending remains unresolved (no spurious conclusion written)")
    apply_events = [e for e in s_after["audit_log"] if e["action"] == "bulk_amend_applied"]
    assert_eq(len(apply_events), 0,
              "t22 no bulk_amend_applied event written on evidence-missing abort")
    cleanup_patterns(tmpdir, "state_t22.json")


def test_23_export_package_basic(tmpdir):
    """export_package: 基础导出功能，包格式、校验和、元数据正确"""
    print("\n== Test 23: export_package basic ==")
    sp = os.path.join(tmpdir, "state_t23.json")
    cleanup_patterns(tmpdir, "state_t23.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)

    pkg_out = os.path.join(tmpdir, "pkg_t23.json")
    res = run_cli(["export_package", "-o", pkg_out, "--operator", "发布经理A",
                   "--description", "2026Q2交接包，待确认后批准"], sp)

    with open(pkg_out, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    assert_eq(pkg["package_format_version"], "1.0.0", "t23 package_format_version == 1.0.0")
    assert_eq(pkg["exported_by"], "发布经理A", "t23 exported_by matches operator")
    assert_eq(pkg["description"], "2026Q2交接包，待确认后批准", "t23 description stored")
    assert_in("state", pkg, "t23 package has state field")
    assert_in("state_checksum", pkg, "t23 package has state_checksum")
    assert_in("rules_snapshot", pkg, "t23 package has rules_snapshot")
    assert_eq(pkg["metadata"]["version"], "2.1.0", "t23 metadata version")
    assert_eq(pkg["metadata"]["draft_version"], 1, "t23 metadata draft_version")
    assert_eq(pkg["metadata"]["items_count"], 5, "t23 metadata items_count")

    s = read_state(sp)
    actions = [e["action"] for e in s["audit_log"]]
    assert_in("export_package", actions, "t23 audit log has export_package event")

    export_event = [e for e in s["audit_log"] if e["action"] == "export_package"][0]
    assert_in("发布经理A", export_event["detail"], "t23 audit records operator")

    pkg_state = pkg["state"]
    assert_eq(pkg_state["items"], s["items"], "t23 package items match state")
    assert_eq(pkg_state["drafts"], s["drafts"], "t23 package drafts match state")
    assert_eq(pkg_state["confirmations"], s["confirmations"], "t23 package confirmations match state")
    pkg_actions = [e["action"] for e in pkg_state["audit_log"]]
    state_actions = [e["action"] for e in s["audit_log"]]
    assert_in("export_package", state_actions, "t23 state has export_package in audit_log")
    assert_eq("export_package" in pkg_actions, False, "t23 package audit_log does NOT include export_package (captured before audit written)")
    assert_eq(len(pkg_state["audit_log"]) + 1, len(s["audit_log"]), "t23 package audit_log has 1 fewer entry (missing export_package)")
    cleanup_patterns(tmpdir, "state_t23.json")


def test_24_package_validation(tmpdir):
    """import_package: 包校验失败（格式版本不兼容、校验和错误）被拒绝"""
    print("\n== Test 24: import_package validation ==")
    sp = os.path.join(tmpdir, "state_t24.json")
    sp2 = os.path.join(tmpdir, "state_t24_target.json")
    cleanup_patterns(tmpdir, "state_t24.json")
    cleanup_patterns(tmpdir, "state_t24_target.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)

    pkg_out = os.path.join(tmpdir, "pkg_t24.json")
    run_cli(["export_package", "-o", pkg_out, "--operator", "test24"], sp)

    with open(pkg_out, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    bad_version_pkg = os.path.join(tmpdir, "bad_version.json")
    bad = copy.deepcopy(pkg)
    bad["package_format_version"] = "99.0.0"
    with open(bad_version_pkg, "w", encoding="utf-8") as f:
        json.dump(bad, f, ensure_ascii=False, indent=2)

    res = run_cli(["import_package", bad_version_pkg, "--operator", "importer"], sp2, expect_fail=True)
    assert_in("Incompatible package format version", res.stdout + res.stderr,
              "t24 rejected: incompatible format version")

    bad_checksum_pkg = os.path.join(tmpdir, "bad_checksum.json")
    bad2 = copy.deepcopy(pkg)
    bad2["state"]["version"] = "9.9.9"
    with open(bad_checksum_pkg, "w", encoding="utf-8") as f:
        json.dump(bad2, f, ensure_ascii=False, indent=2)

    res2 = run_cli(["import_package", bad_checksum_pkg, "--operator", "importer"], sp2, expect_fail=True)
    assert_in("checksum mismatch", (res2.stdout + res2.stderr).lower(),
              "t24 rejected: checksum mismatch")

    no_state_pkg = os.path.join(tmpdir, "no_state.json")
    with open(no_state_pkg, "w", encoding="utf-8") as f:
        json.dump({"package_format_version": "1.0.0", "hello": "world"}, f, ensure_ascii=False)
    res3 = run_cli(["import_package", no_state_pkg, "--operator", "importer"], sp2, expect_fail=True)
    assert_in("missing 'state' field", res3.stdout + res3.stderr, "t24 rejected: missing state field")
    cleanup_patterns(tmpdir, "state_t24.json")


def test_25_import_reject_target_newer(tmpdir):
    """import_package: 目标状态比包新时默认拒绝，--force 可绕过"""
    print("\n== Test 25: import_package reject target newer ==")
    sp_old = os.path.join(tmpdir, "state_t25_old.json")
    sp_new = os.path.join(tmpdir, "state_t25_new.json")
    cleanup_patterns(tmpdir, "state_t25_old.json")
    cleanup_patterns(tmpdir, "state_t25_new.json")

    run_cli(["import", SAMPLE], sp_old)
    run_cli(["draft"], sp_old)

    pkg = os.path.join(tmpdir, "pkg_t25.json")
    run_cli(["export_package", "-o", pkg, "--operator", "test25-exporter"], sp_old)

    run_cli(["import", SAMPLE], sp_new)
    run_cli(["draft"], sp_new)
    run_cli(["draft"], sp_new)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_new)

    s_new = read_state(sp_new)
    assert_eq(s_new["draft_version"], 2, "t25 setup: target draft_v=2 (newer than package)")

    res = run_cli(["import_package", pkg, "--operator", "test25-importer"], sp_new, expect_fail=True)
    assert_in("Target state is NEWER", res.stdout, "t25 rejected: target is newer")

    s_after = read_state(sp_new)
    actions = [e["action"] for e in s_after["audit_log"]]
    assert_in("import_package_rejected", actions, "t25 audit has import_package_rejected")

    res_force = run_cli(["import_package", pkg, "--operator", "test25-importer",
                         "--mode", "takeover", "--force"], sp_new)
    assert_in("[OK] Package imported", res_force.stdout, "t25 --force allows override")

    s_forced = read_state(sp_new)
    assert_eq(s_forced["draft_version"], 1, "t25 force takeover: draft_v reset to package v=1")
    assert_eq(s_forced["items"][2]["owner"], "", "t25 force takeover: CHG-003 owner reset to package state (empty)")

    takeover_events = [e for e in s_forced["audit_log"] if e["action"] == "import_package_takeover"]
    assert_eq(len(takeover_events), 1, "t25 audit has import_package_takeover event")
    assert_in("force=True", takeover_events[0]["detail"], "t25 takeover audit records force=True")
    cleanup_patterns(tmpdir, "state_t25_old.json")
    cleanup_patterns(tmpdir, "state_t25_new.json")


def test_26_import_mode_takeover(tmpdir):
    """import_package: mode=takeover 完全替换目标状态"""
    print("\n== Test 26: import_package mode=takeover ==")
    sp_source = os.path.join(tmpdir, "state_t26_source.json")
    sp_target = os.path.join(tmpdir, "state_t26_target.json")
    cleanup_patterns(tmpdir, "state_t26_source.json")
    cleanup_patterns(tmpdir, "state_t26_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_source)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t26.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源机负责人"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=目标本地修改"], sp_target)

    s_target_before = read_state(sp_target)
    chg001_before = next(it for it in s_target_before["items"] if it["id"] == "CHG-001")
    assert_eq(chg001_before["owner"], "目标本地修改", "t26 setup: target has local change to CHG-001")

    res = run_cli(["import_package", pkg, "--operator", "接管人B", "--mode", "takeover"], sp_target)
    assert_in("[OK] Package imported successfully (mode=takeover)", res.stdout, "t26 takeover import succeeds")

    s_target_after = read_state(sp_target)
    chg001_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-001")
    assert_eq(chg001_after["owner"], "张三", "t26 takeover: CHG-001 reverted to package state (张三)")

    chg003_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-003")
    assert_eq(chg003_after["owner"], "周七", "t26 takeover: CHG-003 has package value 周七")

    assert_eq(s_target_after["confirmations"]["overview"], True, "t26 takeover: confirmations from package preserved")
    assert_eq(s_target_after["confirmations"]["changes"], True, "t26 takeover: changes confirmed")

    takeover_events = [e for e in s_target_after["audit_log"] if e["action"] == "import_package_takeover"]
    assert_eq(len(takeover_events), 1, "t26 audit has import_package_takeover")
    assert_in("接管人B", takeover_events[0]["detail"], "t26 takeover audit records operator=接管人B")
    assert_in("源机负责人", takeover_events[0]["detail"], "t26 takeover audit records exported_by=源机负责人")
    cleanup_patterns(tmpdir, "state_t26_source.json")
    cleanup_patterns(tmpdir, "state_t26_target.json")


def test_27_import_mode_merge(tmpdir):
    """import_package: mode=merge 保留目标历史，包状态作为新起点"""
    print("\n== Test 27: import_package mode=merge ==")
    sp_source = os.path.join(tmpdir, "state_t27_source.json")
    sp_target = os.path.join(tmpdir, "state_t27_target.json")
    cleanup_patterns(tmpdir, "state_t27_source.json")
    cleanup_patterns(tmpdir, "state_t27_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["draft"], sp_source)

    s_source = read_state(sp_source)
    source_audit_len = len(s_source["audit_log"])
    assert_eq(s_source["draft_version"], 3, "t27 setup: package state draft_v=3 (newer than target)")

    pkg = os.path.join(tmpdir, "pkg_t27.json")
    run_cli(["export_package", "-o", pkg, "--operator", "导出者A"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=目标本地修改"], sp_target)

    s_target_before = read_state(sp_target)
    target_audit_len = len(s_target_before["audit_log"])
    assert_eq(s_target_before["draft_version"], 1, "t27 setup: target state draft_v=1 (older than package)")

    res = run_cli(["import_package", pkg, "--operator", "合并者C", "--mode", "merge",
                   "--keep-target-batches"], sp_target)
    assert_in("[OK] Package imported successfully (mode=merge)", res.stdout, "t27 merge import succeeds")

    s_target_after = read_state(sp_target)

    merge_events = [e for e in s_target_after["audit_log"] if e["action"] == "import_package_merge"]
    assert len(merge_events) >= 1, "t27 audit has import_package_merge event(s)"

    chg003_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-003")
    assert_eq(chg003_after["owner"], "周七", "t27 merge: CHG-003 has package value 周七")

    assert_eq(s_target_after["draft_version"], 3, "t27 merge: draft_v from package (v3)")

    total_audit = len(s_target_after["audit_log"])
    expected_min = target_audit_len + 1 + source_audit_len
    assert total_audit >= expected_min, f"t27 merge: audit log merged (target={target_audit_len} + merge marker + source={source_audit_len} <= total={total_audit})"

    actions = [e["action"] for e in s_target_after["audit_log"]]
    assert_in("import_package_merge", actions, "t27 merge event in audit")

    batches = s_target_after["imported_batches"]
    assert_eq(len(batches), 1, "t27 merge: only 1 unique batch (batch-2026Q2-v2.1.0)")
    cleanup_patterns(tmpdir, "state_t27_source.json")
    cleanup_patterns(tmpdir, "state_t27_target.json")


def test_28_import_reject_approved_target(tmpdir):
    """import_package: 目标已批准时默认拒绝，--force 可绕过"""
    print("\n== Test 28: import_package reject approved target ==")
    sp_source = os.path.join(tmpdir, "state_t28_source.json")
    sp_target = os.path.join(tmpdir, "state_t28_target.json")
    cleanup_patterns(tmpdir, "state_t28_source.json")
    cleanup_patterns(tmpdir, "state_t28_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["draft"], sp_source)

    s_source = read_state(sp_source)
    assert_eq(s_source["draft_version"], 3, "t28 setup: package draft_v=3 (newer than target will be)")

    pkg = os.path.join(tmpdir, "pkg_t28.json")
    run_cli(["export_package", "-o", pkg, "--operator", "test28-exporter"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    _amend_bad_items(sp_target)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["approve"], sp_target)

    s_target = read_state(sp_target)
    assert_eq(s_target["approved"], True, "t28 setup: target is approved")
    assert_eq(s_target["draft_version"], 2, "t28 setup: target draft_v=2 (older than package)")

    res = run_cli(["import_package", pkg, "--operator", "test28-importer"], sp_target, expect_fail=True)
    assert_in("already approved", res.stdout, "t28 rejected: target already approved")

    s_after_reject = read_state(sp_target)
    actions = [e["action"] for e in s_after_reject["audit_log"]]
    assert_in("import_package_rejected", actions, "t28 audit has import_package_rejected")

    res_force = run_cli(["import_package", pkg, "--operator", "test28-importer",
                         "--mode", "takeover", "--force"], sp_target)
    assert_in("[OK] Package imported", res_force.stdout, "t28 --force allows override of approved state")

    s_forced = read_state(sp_target)
    assert_eq(s_forced["approved"], False, "t28 force: approved reset to False")
    assert_eq(s_forced["draft_version"], 3, "t28 force: draft_v from package (v3)")
    cleanup_patterns(tmpdir, "state_t28_source.json")
    cleanup_patterns(tmpdir, "state_t28_target.json")


def test_29_export_package_no_rules(tmpdir):
    """export_package: --no-rules 不包含规则快照"""
    print("\n== Test 29: export_package --no-rules ==")
    sp = os.path.join(tmpdir, "state_t29.json")
    cleanup_patterns(tmpdir, "state_t29.json")

    run_cli(["import", SAMPLE], sp)

    pkg_with_rules = os.path.join(tmpdir, "pkg_t29_with_rules.json")
    run_cli(["export_package", "-o", pkg_with_rules, "--operator", "test29"], sp)

    with open(pkg_with_rules, "r", encoding="utf-8") as f:
        pkg1 = json.load(f)
    assert pkg1.get("rules_snapshot") is not None, "t29 default includes rules_snapshot"

    pkg_no_rules = os.path.join(tmpdir, "pkg_t29_no_rules.json")
    run_cli(["export_package", "-o", pkg_no_rules, "--operator", "test29", "--no-rules"], sp)

    with open(pkg_no_rules, "r", encoding="utf-8") as f:
        pkg2 = json.load(f)
    assert pkg2.get("rules_snapshot") is None, "t29 --no-rules excludes rules_snapshot"
    cleanup_patterns(tmpdir, "state_t29.json")


def test_30_full_e2e_handoff_workflow(tmpdir):
    """完整端到端交接流程：导入→批量修订→导出包→换状态恢复→重新draft→确认→批准→导出Markdown"""
    print("\n== Test 30: Full E2E handoff workflow (import -> bulk -> export_package -> import_package -> re-draft -> confirm -> approve -> export markdown) ==")
    sp_machine1 = os.path.join(tmpdir, "state_machine1.json")
    sp_machine2 = os.path.join(tmpdir, "state_machine2.json")
    cleanup_patterns(tmpdir, "state_machine1.json")
    cleanup_patterns(tmpdir, "state_machine2.json")

    print("\n  [Machine 1] 负责人A开始工作...")
    run_cli(["import", SAMPLE], sp_machine1)
    run_cli(["draft"], sp_machine1)

    print("\n  [Machine 1] 负责人A做批量修订...")
    json_patch = os.path.join(tmpdir, "patch_t30.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t30-e2e-001",
            "operator": "负责人A",
            "reason": "Q2交接前批量修订",
            "items": [
                {"id": "CHG-003", "owner": "周七", "risk_level": "critical", "category": "removal"},
                {"id": "CHG-005", "owner": "赵六", "risk_level": "critical", "category": "security"},
            ],
        }, f, ensure_ascii=False, indent=2)
    run_cli(["bulk_amend", json_patch, "--mode", "overwrite",
             "--operator", "负责人A", "--reason", "Q2交接前批量修订"], sp_machine1)

    s1_after_bulk = read_state(sp_machine1)
    assert_eq(s1_after_bulk["draft_version"], 1, "t30 machine1: draft_v=1 after bulk amend (before re-draft)")

    print("\n  [Machine 1] 负责人A重新生成draft...")
    run_cli(["draft"], sp_machine1)

    print("\n  [Machine 1] 负责人A确认部分章节...")
    run_cli(["confirm", "overview"], sp_machine1)
    run_cli(["confirm", "changes"], sp_machine1)

    s1_before_export = read_state(sp_machine1)
    s1_draft_v = s1_before_export["draft_version"]
    s1_audit_len = len(s1_before_export["audit_log"])
    chg003_s1 = next(it for it in s1_before_export["items"] if it["id"] == "CHG-003")
    chg005_s1 = next(it for it in s1_before_export["items"] if it["id"] == "CHG-005")
    assert_eq(chg003_s1["owner"], "周七", "t30 machine1: CHG-003 owner=周七")
    assert_eq(chg005_s1["risk_level"], "critical", "t30 machine1: CHG-005 risk_level=critical")

    print("\n  [Machine 1] 负责人A导出交接包...")
    pkg = os.path.join(tmpdir, "handoff_pkg_t30.json")
    run_cli(["export_package", "-o", pkg, "--operator", "负责人A",
             "--description", "Q2-v2.1.0 交接包，已完成批量修订和2个章节确认"], sp_machine1)

    print(f"\n  [Handoff] 交接包传递: {pkg}")
    print(f"  [Machine 2] 负责人B导入交接包，采用 takeover 模式完全接管...")
    res_import = run_cli(["import_package", pkg, "--operator", "负责人B", "--mode", "takeover"], sp_machine2)
    assert_in("[OK] Package imported successfully (mode=takeover)", res_import.stdout,
              "t30 machine2: takeover import succeeds")

    s2_after_import = read_state(sp_machine2)
    assert_eq(s2_after_import["draft_version"], s1_draft_v,
              f"t30 machine2: draft_v matches package ({s1_draft_v})")
    assert_eq(s2_after_import["confirmations"]["overview"], True,
              "t30 machine2: overview confirmation preserved from package")
    assert_eq(s2_after_import["confirmations"]["changes"], True,
              "t30 machine2: changes confirmation preserved from package")
    assert_eq(s2_after_import["confirmations"]["migration"], False,
              "t30 machine2: migration NOT confirmed (correct)")
    assert_eq(s2_after_import["confirmations"]["known_issues"], False,
              "t30 machine2: known_issues NOT confirmed (correct)")

    chg003_s2 = next(it for it in s2_after_import["items"] if it["id"] == "CHG-003")
    assert_eq(chg003_s2["owner"], "周七", "t30 machine2: CHG-003 owner=周七 preserved")
    chg005_s2 = next(it for it in s2_after_import["items"] if it["id"] == "CHG-005")
    assert_eq(chg005_s2["risk_level"], "critical", "t30 machine2: CHG-005 risk_level=critical preserved")

    print("\n  [Machine 2] 负责人B查看状态和历史...")
    res_status = run_cli(["status"], sp_machine2)
    assert_in("Draft:         v2", res_status.stdout, "t30 status shows correct draft version")
    assert_in("Items:         5", res_status.stdout, "t30 status shows 5 items")
    assert_in("Approved:      False", res_status.stdout, "t30 status shows not approved")

    res_history = run_cli(["history"], sp_machine2)
    assert_in("import", res_history.stdout, "t30 history shows import event")
    assert_in("bulk_amend_applied", res_history.stdout, "t30 history shows bulk_amend_applied event")
    assert_in("draft_generated", res_history.stdout, "t30 history shows draft_generated events")
    assert_in("confirm", res_history.stdout, "t30 history shows confirm events")
    assert_in("import_package_takeover", res_history.stdout, "t30 history shows import_package_takeover event")

    takeover_event = [e for e in s2_after_import["audit_log"] if e["action"] == "import_package_takeover"][0]
    assert_in("负责人B", takeover_event["detail"], "t30 takeover audit records operator=负责人B")
    assert_in("负责人A", takeover_event["detail"], "t30 takeover audit records exported_by=负责人A")

    print("\n  [Machine 2] 负责人B继续剩余章节确认...")
    run_cli(["confirm", "migration"], sp_machine2)
    run_cli(["confirm", "known_issues"], sp_machine2)

    print("\n  [Machine 2] 负责人B重新生成draft...")
    run_cli(["draft"], sp_machine2)

    s2_after_confirm = read_state(sp_machine2)
    assert_eq(s2_after_confirm["confirmations"]["migration"], True, "t30 machine2: migration confirmed")
    assert_eq(s2_after_confirm["confirmations"]["known_issues"], True, "t30 machine2: known_issues confirmed")

    print("\n  [Machine 2] 负责人B批准...")
    run_cli(["approve"], sp_machine2)

    s2_approved = read_state(sp_machine2)
    assert_eq(s2_approved["approved"], True, "t30 machine2: approved")
    assert_eq(s2_approved["approved_at_version"], "2.1.0", "t30 approved_at_version=2.1.0")

    print("\n  [Machine 2] 负责人B导出最终Markdown...")
    md_out = os.path.join(tmpdir, "t30_final_release_notes.md")
    run_cli(["export", "-o", md_out], sp_machine2)

    md = read_file(md_out)
    assert_in("# Release Notes v2.1.0", md, "t30 final MD: title correct")
    assert_in("owner:周七", md, "t30 final MD: CHG-003 owner=周七")
    assert_in("risk:critical", md, "t30 final MD: critical risks present")
    assert_in("owner:赵六", md, "t30 final MD: CHG-005 owner=赵六")

    print(f"\n  [OK] 交接完成！最终Markdown已生成: {md_out}")

    s2_final = read_state(sp_machine2)
    final_audit = s2_final["audit_log"]
    actions = [e["action"] for e in final_audit]
    expected_sequence = ["import", "draft_generated", "bulk_amend_applied", "draft_generated",
                         "confirm", "confirm", "import_package_takeover",
                         "confirm", "confirm", "draft_generated", "approved", "export"]
    for expected_action in expected_sequence:
        assert_in(expected_action, actions, f"t30 audit has {expected_action}")

    print(f"\n  [OK] 完整链路验证通过: 导入→批量修订→导出方案包→换状态恢复→重新draft→确认→批准→导出Markdown")
    cleanup_patterns(tmpdir, "state_machine1.json")
    cleanup_patterns(tmpdir, "state_machine2.json")


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
        test_10_bulk_no_conflict_json_and_csv(tmpdir)
        test_11_bulk_conflict_mode_abort(tmpdir)
        test_12_bulk_conflict_mode_skip(tmpdir)
        test_13_bulk_conflict_mode_overwrite(tmpdir)
        test_14_section_confirmed_detection(tmpdir)
        test_15_already_modified_conflict(tmpdir)
        test_16_restart_conflict_resume(tmpdir)
        test_17_full_e2e_bulk_workflow(tmpdir)
        test_18_bulk_draft_newer_conflict(tmpdir)
        test_19_bulk_invalid_risk_rejected(tmpdir)
        test_20_resume_abort_per_item(tmpdir)
        test_21_draft_version_alignment(tmpdir)
        test_22_resume_missing_evidence_aborts(tmpdir)
        test_23_export_package_basic(tmpdir)
        test_24_package_validation(tmpdir)
        test_25_import_reject_target_newer(tmpdir)
        test_26_import_mode_takeover(tmpdir)
        test_27_import_mode_merge(tmpdir)
        test_28_import_reject_approved_target(tmpdir)
        test_29_export_package_no_rules(tmpdir)
        test_30_full_e2e_handoff_workflow(tmpdir)

        print(f"\n==== SUMMARY: {PASS} passed, {FAIL} failed ====")
        if FAIL:
            sys.exit(1)
        print("All tests passed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
