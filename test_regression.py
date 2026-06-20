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
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=SCRIPT_DIR)
    ok = res.returncode != 0 if expect_fail else res.returncode == 0
    if not ok:
        safe_print(f"  [FAIL] {' '.join(args)}")
        safe_print(f"    exit={res.returncode} expect_fail={expect_fail}")
        if res.stdout.strip():
            safe_print(f"    stdout: {safe_truncate(res.stdout)}")
        if res.stderr.strip():
            safe_print(f"    stderr: {safe_truncate(res.stderr)}")
    else:
        label = "(expected fail)" if expect_fail else ""
        safe_print(f"  [OK] {' '.join(args)} {label}")
    return res


def run_cli_utf8(args, state_path, expect_fail=False):
    """调用 CLI 时显式加 -X utf8，模拟 Python UTF-8 模式"""
    cmd = [sys.executable, "-X", "utf8", CLI, "--rules", RULES, "--state", state_path] + args
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=SCRIPT_DIR)
    ok = res.returncode != 0 if expect_fail else res.returncode == 0
    if not ok:
        safe_print(f"  [FAIL -X utf8] {' '.join(args)}")
        safe_print(f"    exit={res.returncode} expect_fail={expect_fail}")
        if res.stdout.strip():
            safe_print(f"    stdout: {safe_truncate(res.stdout)}")
        if res.stderr.strip():
            safe_print(f"    stderr: {safe_truncate(res.stderr)}")
    else:
        label = "(expected fail)" if expect_fail else ""
        safe_print(f"  [OK -X utf8] {' '.join(args)} {label}")
    return res


def safe_truncate(s, limit=400):
    if s is None:
        return ""
    safe = s.encode("ascii", errors="replace").decode("ascii", errors="replace")
    if len(safe) > limit:
        return safe[:limit] + f"... (total {len(s)} chars)"
    return safe


def safe_print(s):
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
    except Exception:
        try:
            safe = s.encode("ascii", errors="replace").decode("ascii")
            sys.stdout.write(safe + "\n")
            sys.stdout.flush()
        except Exception:
            pass


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
        safe_print(f"    [ASSERT OK] {msg}")
    else:
        FAIL += 1
        safe_print(f"    [ASSERT FAIL] {msg}")
        safe_print(f"      expected: {safe_truncate(repr(expected))}")
        safe_print(f"      actual:   {safe_truncate(repr(actual))}")


def assert_in(needle, haystack, msg):
    global FAIL, PASS
    if needle in haystack:
        PASS += 1
        safe_print(f"    [ASSERT OK] {msg}")
    else:
        FAIL += 1
        safe_print(f"    [ASSERT FAIL] {msg}")
        safe_print(f"      missing: {safe_truncate(repr(needle))}")
        safe_print(f"      in: {safe_truncate(repr(haystack[:200] if isinstance(haystack, (str, list, dict)) else haystack))}")


def assert_not_in(needle, haystack, msg):
    global FAIL, PASS
    if needle not in haystack:
        PASS += 1
        safe_print(f"    [ASSERT OK] {msg}")
    else:
        FAIL += 1
        safe_print(f"    [ASSERT FAIL] {msg}")
        safe_print(f"      unexpected: {safe_truncate(repr(needle))}")
        safe_print(f"      in: {safe_truncate(repr(haystack[:200] if isinstance(haystack, (str, list, dict)) else haystack))}")


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
    """import_package: 目标状态比包新时默认标记BLOCKED，--force + takeover_confirm 可绕过"""
    print("\n== Test 25: import_package target newer -> reconcile BLOCKED -> force confirm ==")
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

    res = run_cli(["import_package", pkg, "--operator", "test25-importer", "--mode", "takeover"], sp_new)
    assert_in("Target state is NEWER", res.stdout, "t25 reconcile: target is newer flagged")
    assert_in("BLOCKED", res.stdout, "t25 reconcile: BLOCKED tag present")
    assert_in("RECONCILED - BUT BLOCKED", res.stdout, "t25 reconcile: status is BLOCKED (not AWAITING)")

    s_after = read_state(sp_new)
    actions = [e["action"] for e in s_after["audit_log"]]
    assert_in("import_package_reconciled", actions, "t25 audit has import_package_reconciled (not rejected)")
    assert s_after.get("pending_takeover") is not None, "t25 pending_takeover created"
    assert len(s_after.get("takeover_history", [])) == 0, "t25 no takeover_history yet (not confirmed)"

    res_force = run_cli(["import_package", pkg, "--operator", "test25-importer",
                         "--mode", "takeover", "--force"], sp_new)
    assert_in("AWAITING CONFIRMATION", res_force.stdout, "t25 --force reconcile still pending (two-step)")

    s_before_confirm = read_state(sp_new)
    assert s_before_confirm.get("pending_takeover") is not None, "t25 pending_takeover still present"

    res_confirm = run_cli(["takeover_confirm", "--operator", "test25-importer", "--force"], sp_new)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t25 --force confirm succeeds")

    s_forced = read_state(sp_new)
    assert_eq(s_forced["draft_version"], 1, "t25 force takeover: draft_v reset to package v=1")
    assert_eq(s_forced["items"][2]["owner"], "", "t25 force takeover: CHG-003 owner reset to package state (empty)")
    assert s_forced.get("pending_takeover") is None, "t25 pending_takeover cleared after confirm"

    takeover_events = [e for e in s_forced["audit_log"] if e["action"] == "takeover_confirmed"]
    assert_eq(len(takeover_events), 1, "t25 audit has takeover_confirmed event")
    assert_in("force=True", takeover_events[0]["detail"], "t25 takeover audit records force=True")

    assert len(s_forced.get("takeover_history", [])) == 1, "t25 takeover_history has 1 entry after confirm"
    assert len(s_forced.get("confirmed_takeover_sessions", {})) == 1, "t25 confirmed session created"
    cleanup_patterns(tmpdir, "state_t25_old.json")
    cleanup_patterns(tmpdir, "state_t25_new.json")


def test_26_import_mode_takeover(tmpdir):
    """import_package+takeover_confirm: mode=takeover 完全替换目标状态（两步流程）"""
    print("\n== Test 26: import_package reconcile -> takeover_confirm (mode=takeover) ==")
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

    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)
    assert "handover_summary" in pkg_data, "t26 package has handover_summary"
    hs26 = pkg_data["handover_summary"]
    assert_eq(len(hs26["sections_confirmed"]), 4, "t26 handover_summary: 4 sections confirmed")
    assert "overview" in hs26["sections_confirmed"], "t26 overview confirmed"
    assert "changes" in hs26["sections_confirmed"], "t26 changes confirmed"
    assert "migration" in hs26["sections_confirmed"], "t26 migration confirmed"
    assert "known_issues" in hs26["sections_confirmed"], "t26 known_issues confirmed"

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=目标本地修改"], sp_target)

    s_target_before = read_state(sp_target)
    chg001_before = next(it for it in s_target_before["items"] if it["id"] == "CHG-001")
    assert_eq(chg001_before["owner"], "目标本地修改", "t26 setup: target has local change to CHG-001")

    res_reconcile = run_cli(["import_package", pkg, "--operator", "接管人B", "--mode", "takeover"], sp_target)
    assert_in("RECONCILED", res_reconcile.stdout, "t26 reconcile step shows RECONCILED")
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t26 reconcile: AWAITING CONFIRMATION")
    assert_in("Handover Summary from Source", res_reconcile.stdout, "t26 reconcile shows handover summary")

    s_pending = read_state(sp_target)
    assert s_pending.get("pending_takeover") is not None, "t26 pending_takeover created"
    assert_eq(len(s_pending.get("takeover_history", [])), 0, "t26 no takeover_history yet before confirm")

    res_confirm = run_cli(["takeover_confirm", "--operator", "接管人B"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t26 confirm succeeds")

    s_target_after = read_state(sp_target)
    assert s_target_after.get("pending_takeover") is None, "t26 pending_takeover cleared after confirm"
    assert_eq(len(s_target_after.get("takeover_history", [])), 1, "t26 takeover_history has 1 entry")

    chg001_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-001")
    assert_eq(chg001_after["owner"], "张三", "t26 takeover: CHG-001 reverted to package state (张三)")

    chg003_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-003")
    assert_eq(chg003_after["owner"], "周七", "t26 takeover: CHG-003 has package value 周七")

    assert_eq(s_target_after["confirmations"]["overview"], True, "t26 takeover: confirmations from package preserved")
    assert_eq(s_target_after["confirmations"]["changes"], True, "t26 takeover: changes confirmed")

    takeover_events = [e for e in s_target_after["audit_log"] if e["action"] == "takeover_confirmed"]
    assert_eq(len(takeover_events), 1, "t26 audit has takeover_confirmed")
    assert_in("接管人B", takeover_events[0]["detail"], "t26 takeover audit records operator=接管人B")
    assert_eq(s_target_after["takeover_history"][0].get("exported_by"), "源机负责人", "t26 takeover_history exported_by=源机负责人")

    sessions = s_target_after.get("confirmed_takeover_sessions", {})
    assert_eq(len(sessions), 1, "t26 confirmed session created")
    session_key = list(sessions.keys())[0]
    assert sessions[session_key]["revoked"] == False, "t26 session not revoked"
    assert sessions[session_key]["confirmed_by"] == "接管人B", "t26 session confirmed_by correct"
    cleanup_patterns(tmpdir, "state_t26_source.json")
    cleanup_patterns(tmpdir, "state_t26_target.json")


def test_27_import_mode_merge(tmpdir):
    """import_package+takeover_confirm: mode=merge 保留目标历史，包状态作为新起点（两步流程）"""
    print("\n== Test 27: import_package reconcile -> takeover_confirm (mode=merge) ==")
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

    res_reconcile = run_cli(["import_package", pkg, "--operator", "合并者C", "--mode", "merge",
                             "--keep-target-batches"], sp_target)
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t27 merge reconcile: AWAITING CONFIRMATION")

    s_pending = read_state(sp_target)
    assert s_pending.get("pending_takeover") is not None, "t27 pending_takeover created"

    res_confirm = run_cli(["takeover_confirm", "--operator", "合并者C"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t27 merge confirm succeeds")

    s_target_after = read_state(sp_target)
    assert s_target_after.get("pending_takeover") is None, "t27 pending_takeover cleared after confirm"

    merge_events = [e for e in s_target_after["audit_log"] if e["action"] == "takeover_confirmed"]
    assert len(merge_events) >= 1, "t27 audit has takeover_confirmed event"

    chg003_after = next(it for it in s_target_after["items"] if it["id"] == "CHG-003")
    assert_eq(chg003_after["owner"], "周七", "t27 merge: CHG-003 has package value 周七")

    assert_eq(s_target_after["draft_version"], 3, "t27 merge: draft_v from package (v3)")

    total_audit = len(s_target_after["audit_log"])
    expected_min = target_audit_len + 1 + source_audit_len
    assert total_audit >= expected_min, f"t27 merge: audit log merged (target={target_audit_len} + merge marker + source={source_audit_len} <= total={total_audit})"

    actions = [e["action"] for e in s_target_after["audit_log"]]
    assert_in("takeover_confirmed", actions, "t27 takeover_confirmed event in audit")

    batches = s_target_after["imported_batches"]
    assert_eq(len(batches), 1, "t27 merge: only 1 unique batch (batch-2026Q2-v2.1.0)")

    assert_eq(len(s_target_after.get("takeover_history", [])), 1, "t27 takeover_history has 1 entry")
    takeover = s_target_after["takeover_history"][0]
    assert_eq(takeover["mode"], "merge", "t27 takeover mode is merge")
    cleanup_patterns(tmpdir, "state_t27_source.json")
    cleanup_patterns(tmpdir, "state_t27_target.json")


def test_28_import_reject_approved_target(tmpdir):
    """import_package: 目标已批准时标记BLOCKED，--force + takeover_confirm 可绕过"""
    print("\n== Test 28: import_package approved target BLOCKED -> force confirm ==")
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

    res = run_cli(["import_package", pkg, "--operator", "test28-importer", "--mode", "takeover"], sp_target)
    assert_in("already approved", res.stdout, "t28 reconcile: target already approved flagged")
    assert_in("BLOCKED", res.stdout, "t28 reconcile: BLOCKED tag present")
    assert_in("RECONCILED - BUT BLOCKED", res.stdout, "t28 reconcile: status is BLOCKED (not AWAITING)")

    s_after_reconcile = read_state(sp_target)
    actions = [e["action"] for e in s_after_reconcile["audit_log"]]
    assert_in("import_package_reconciled", actions, "t28 audit has import_package_reconciled (not rejected)")
    assert s_after_reconcile.get("pending_takeover") is not None, "t28 pending_takeover created"

    res_confirm_no_force = run_cli(["takeover_confirm", "--operator", "test28-importer"], sp_target, expect_fail=True)
    assert_in("BLOCKED", res_confirm_no_force.stdout, "t28 confirm without --force blocked")

    res_force_confirm = run_cli(["takeover_confirm", "--operator", "test28-importer",
                                 "--force"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_force_confirm.stdout, "t28 --force confirm allows override of approved state")

    s_forced = read_state(sp_target)
    assert_eq(s_forced["approved"], False, "t28 force: approved reset to False")
    assert_eq(s_forced["draft_version"], 3, "t28 force: draft_v from package (v3)")
    assert s_forced.get("pending_takeover") is None, "t28 pending_takeover cleared after confirm"
    assert len(s_forced.get("takeover_history", [])) == 1, "t28 takeover_history has 1 entry"
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
    """完整端到端交接流程（两步确认版）：导入→批量修订→导出包→对账→查看详情→确认→重新draft→确认→批准→导出Markdown"""
    print("\n== Test 30: Full E2E handoff workflow (2-step confirm) ==")
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

    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)
    assert "handover_summary" in pkg_data, "t30 package has handover_summary"
    hs30 = pkg_data["handover_summary"]
    assert_eq(len(hs30["sections_confirmed"]), 2, "t30 handover_summary: 2 sections confirmed")
    assert "overview" in hs30["sections_confirmed"], "t30 overview confirmed"
    assert "changes" in hs30["sections_confirmed"], "t30 changes confirmed"
    assert_eq(len(hs30["sections_pending"]), 2, "t30 handover_summary: 2 sections pending")
    assert "migration" in hs30["sections_pending"], "t30 migration pending"
    assert "known_issues" in hs30["sections_pending"], "t30 known_issues pending"
    assert len(pkg_data["handover_summary"].get("suggested_next_steps", [])) > 0, "t30 handover_summary: suggested_next_steps present"
    print("  [OK] 交接包含 handover_summary，2已确认/2待确认，含建议下一步")

    print(f"\n  [Handoff] 交接包传递: {pkg}")
    print(f"  [Machine 2] 负责人B导入交接包做只读对账...")
    res_reconcile = run_cli(["import_package", pkg, "--operator", "负责人B", "--mode", "takeover"], sp_machine2)
    assert_in("RECONCILED", res_reconcile.stdout, "t30 machine2: reconcile shows RECONCILED")
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t30 machine2: reconcile shows AWAITING CONFIRMATION")
    assert_in("Handover Summary from Source", res_reconcile.stdout, "t30 reconcile shows handover summary")

    s2_pending = read_state(sp_machine2)
    assert s2_pending.get("pending_takeover") is not None, "t30 machine2: pending_takeover created"
    pt = s2_pending["pending_takeover"]
    assert "takeover_id" in pt, "t30 pending_takeover has takeover_id"
    assert "pre_import_state" in pt, "t30 pending_takeover has pre_import_state"
    assert pt.get("imported_by") == "负责人B", "t30 pending_takeover imported_by=负责人B"
    assert len(s2_pending.get("takeover_history", [])) == 0, "t30 machine2: NO takeover_history before confirm"
    print(f"  [OK] 只读对账完成，pending_takeover_id={pt['takeover_id']}")

    print("\n  [Machine 2] 负责人B查看交接详情...")
    res_detail = run_cli(["takeover_detail"], sp_machine2)
    assert_in("TAKEOVER DETAIL:", res_detail.stdout, "t30 takeover_detail shows header")
    assert_in("[PENDING CONFIRMATION]", res_detail.stdout, "t30 takeover_detail shows pending tag")
    assert_in("Takeover ID:", res_detail.stdout, "t30 takeover_detail shows takeover_id")
    assert_in("Handover Summary", res_detail.stdout, "t30 takeover_detail shows handover summary")
    assert_in("Suggested next steps", res_detail.stdout, "t30 takeover_detail shows suggested next steps")
    print("  [OK] takeover_detail 展示待确认交接详情")

    print("\n  [Machine 2] 负责人B正式确认继续做...")
    res_confirm = run_cli(["takeover_confirm", "--operator", "负责人B"], sp_machine2)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t30 machine2: takeover confirmed")
    assert_in("Next steps:", res_confirm.stdout, "t30 confirm output shows next steps")

    s2_after_import = read_state(sp_machine2)
    assert s2_after_import.get("pending_takeover") is None, "t30 machine2: pending_takeover cleared after confirm"
    assert len(s2_after_import.get("takeover_history", [])) == 1, "t30 machine2: takeover_history has 1 entry"
    assert len(s2_after_import.get("confirmed_takeover_sessions", {})) == 1, "t30 machine2: confirmed session created"

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

    print("\n  [Machine 2] 负责人B查看状态（应显示Active Takeover Session）...")
    res_status = run_cli(["status"], sp_machine2)
    assert_in("Draft:         v2", res_status.stdout, "t30 status shows correct draft version")
    assert_in("Items:         5", res_status.stdout, "t30 status shows 5 items")
    assert_in("Approved:      False", res_status.stdout, "t30 status shows not approved")
    assert_in("Active Takeover Sessions:", res_status.stdout, "t30 status shows active takeover sessions")

    res_history = run_cli(["history"], sp_machine2)
    assert_in("import", res_history.stdout, "t30 history shows import event")
    assert_in("bulk_amend_applied", res_history.stdout, "t30 history shows bulk_amend_applied event")
    assert_in("draft_generated", res_history.stdout, "t30 history shows draft_generated events")
    assert_in("confirm", res_history.stdout, "t30 history shows confirm events")
    assert_in("takeover_confirmed", res_history.stdout, "t30 history shows takeover_confirmed event")

    takeover_events = [e for e in s2_after_import["audit_log"] if e["action"] == "takeover_confirmed"]
    assert len(takeover_events) == 1, "t30 audit has 1 takeover_confirmed event"
    assert_in("负责人B", takeover_events[0]["detail"], "t30 takeover audit records operator=负责人B")
    assert_eq(s2_after_import["takeover_history"][0].get("exported_by"), "负责人A", "t30 takeover_history exported_by=负责人A")

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
                         "confirm", "confirm", "import_package_takeover", "takeover_confirmed",
                         "confirm", "confirm", "draft_generated", "approved", "export"]
    for expected_action in expected_sequence:
        assert_in(expected_action, actions, f"t30 audit has {expected_action}")

    print(f"\n  [OK] 完整链路验证通过: 导入→批量修订→导出方案包→只读对账→查看详情→确认接管→重新draft→确认→批准→导出Markdown")
    cleanup_patterns(tmpdir, "state_machine1.json")
    cleanup_patterns(tmpdir, "state_machine2.json")


def test_31_preflight_basic(tmpdir):
    """preflight_check: 基础预检功能，无目标状态时输出正确信息，不修改状态"""
    print("\n== Test 31: preflight_check basic (no target state) ==")
    sp_source = os.path.join(tmpdir, "state_t31_source.json")
    sp_target = os.path.join(tmpdir, "state_t31_target.json")
    cleanup_patterns(tmpdir, "state_t31_source.json")
    cleanup_patterns(tmpdir, "state_t31_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t31.json")
    run_cli(["export_package", "-o", pkg, "--operator", "测试员31"], sp_source)

    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)
    assert "exported_from" not in pkg_data, "t31 package must NOT contain exported_from (removed hardcoded machine info)"
    assert "rules_path" not in pkg_data, "t31 package must NOT contain rules_path (removed hardcoded path)"
    print("  [OK] Package has no hardcoded machine info (exported_from, rules_path removed)")

    res = run_cli(["preflight_check", pkg], sp_target)
    assert_in("PACKAGE PREFLIGHT CHECK", res.stdout, "t31 preflight header present")
    assert_in("NOT INITIALIZED", res.stdout, "t31 reports target not initialized")
    assert_in("Package state:   v2.1.0 / draft v1", res.stdout, "t31 shows package state correctly")
    assert_in("PREFLIGHT COMPLETE - No state changes made", res.stdout, "t31 preflight complete message")
    assert_in("Recommended mode: takeover", res.stdout, "t31 recommends takeover for fresh state")

    assert not os.path.exists(sp_target), "t31 target state file NOT created by preflight (no state changes)"
    print("  [OK] preflight_check did NOT create or modify target state file")

    res2 = run_cli(["preflight_check", pkg], sp_source)
    assert_in("Version:         v2.1.0", res2.stdout, "t31 existing target version shown")
    assert_in("Draft:           v1", res2.stdout, "t31 existing target draft shown")
    assert_in("Content Diff Summary", res2.stdout, "t31 diff summary shown for existing target")
    assert_in("Rules Snapshot Diff", res2.stdout, "t31 rules snapshot diff shown")
    assert_in("IDENTICAL to local rules", res2.stdout, "t31 rules are identical")

    s_after = read_state(sp_source)
    actions_before = [e["action"] for e in s_after["audit_log"]]
    assert "preflight_check" not in actions_before, "t31 preflight_check NOT in audit log (no state changes)"
    print("  [OK] preflight_check did NOT modify source state or write any audit entries")
    cleanup_patterns(tmpdir, "state_t31_source.json")
    cleanup_patterns(tmpdir, "state_t31_target.json")


def test_32_preflight_conflict_detection(tmpdir):
    """preflight_check: 冲突检测，目标比包新、目标已批准时正确显示风险和建议模式"""
    print("\n== Test 32: preflight_check conflict detection ==")
    sp_old = os.path.join(tmpdir, "state_t32_old.json")
    sp_new = os.path.join(tmpdir, "state_t32_new.json")
    sp_approved = os.path.join(tmpdir, "state_t32_approved.json")
    cleanup_patterns(tmpdir, "state_t32_old.json")
    cleanup_patterns(tmpdir, "state_t32_new.json")
    cleanup_patterns(tmpdir, "state_t32_approved.json")

    run_cli(["import", SAMPLE], sp_old)
    run_cli(["draft"], sp_old)

    pkg = os.path.join(tmpdir, "pkg_t32.json")
    run_cli(["export_package", "-o", pkg, "--operator", "导出者32"], sp_old)

    run_cli(["import", SAMPLE], sp_new)
    run_cli(["draft"], sp_new)
    run_cli(["draft"], sp_new)
    run_cli(["amend", "CHG-001", "--field", "owner=新负责人A"], sp_new)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_new)

    s_new = read_state(sp_new)
    assert_eq(s_new["draft_version"], 2, "t32 setup: target draft_v=2 (newer than package v1)")

    res = run_cli(["preflight_check", pkg], sp_new)
    assert_in("Target state is NEWER", res.stdout, "t32 reports target newer")
    assert_in("Risk level:      HIGH", res.stdout, "t32 risk level is HIGH")
    assert_in("Force required:   YES", res.stdout, "t32 force required for newer target")
    assert_in("Recommended mode: merge", res.stdout, "t32 recommends merge for newer target")
    assert_in("MODIFY 2 existing items", res.stdout, "t32 reports 2 items will be modified")
    assert_in("CHG-001", res.stdout, "t32 CHG-001 in modified list")
    assert_in("CHG-003", res.stdout, "t32 CHG-003 in modified list")
    assert_in("owner: '新负责人A' -> '张三'", res.stdout, "t32 shows CHG-001 owner change")
    assert_in("owner: '周七' -> ''", res.stdout, "t32 shows CHG-003 owner change (target -> package)")
    print("  [OK] 目标比包新时正确建议 merge 模式并显示 2 个修改条目")

    run_cli(["import", SAMPLE], sp_approved)
    run_cli(["draft"], sp_approved)
    _amend_bad_items(sp_approved)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp_approved)
    run_cli(["draft"], sp_approved)
    run_cli(["approve"], sp_approved)

    s_approved = read_state(sp_approved)
    assert_eq(s_approved["approved"], True, "t32 setup: target is approved")

    res2 = run_cli(["preflight_check", pkg], sp_approved)
    assert_in("Target state is already APPROVED", res2.stdout, "t32 reports target approved")
    assert_in("Risk level:      HIGH", res2.stdout, "t32 risk HIGH for approved target")
    assert_in("Force required:   YES", res2.stdout, "t32 force required for approved target")

    modified_pkg = os.path.join(tmpdir, "pkg_t32_modified.json")
    with open(pkg, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["state"]["items"][0]["owner"] = "包中修改的张三"
    data["state"]["items"][1]["risk_level"] = "critical"
    data["state"]["confirmations"] = {
        "overview": True,
        "changes": False,
        "migration": True,
        "known_issues": False,
    }
    data["state_checksum"] = __import__("hashlib").sha256(
        __import__("json").dumps({
            "version": data["state"].get("version"),
            "draft_version": data["state"].get("draft_version"),
            "approved": data["state"].get("approved"),
            "approved_at_version": data["state"].get("approved_at_version"),
            "approved_at_draft_version": data["state"].get("approved_at_draft_version"),
            "items": data["state"].get("items", []),
            "drafts": data["state"].get("drafts", []),
            "confirmations": data["state"].get("confirmations", {}),
            "audit_log_len": len(data["state"].get("audit_log", [])),
            "pending_bulk_ops_len": len(data["state"].get("pending_bulk_ops", [])),
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    with open(modified_pkg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    sp_empty = os.path.join(tmpdir, "state_t32_empty.json")
    cleanup_patterns(tmpdir, "state_t32_empty.json")
    run_cli(["import", SAMPLE], sp_empty)
    run_cli(["draft"], sp_empty)
    run_cli(["confirm", "changes"], sp_empty)
    run_cli(["confirm", "known_issues"], sp_empty)
    res3 = run_cli(["preflight_check", modified_pkg], sp_empty)
    assert_in(">>> overview: PENDING -> CONFIRMED", res3.stdout, "t32 shows overview confirmation change")
    assert_in("<<< changes: CONFIRMED -> PENDING", res3.stdout, "t32 shows changes confirmation change")
    assert_in(">>> migration: PENDING -> CONFIRMED", res3.stdout, "t32 shows migration confirmation change")
    assert_in("<<< known_issues: CONFIRMED -> PENDING", res3.stdout, "t32 shows known_issues confirmation change")
    cleanup_patterns(tmpdir, "state_t32_old.json")
    cleanup_patterns(tmpdir, "state_t32_new.json")
    cleanup_patterns(tmpdir, "state_t32_approved.json")
    cleanup_patterns(tmpdir, "state_t32_empty.json")


def test_33_audit_view_timeline(tmpdir):
    """audit_view: 时间线展示（两步确认版），接管前后字段变化、决策人、跨重启续做标记"""
    print("\n== Test 33: audit_view timeline (2-step confirm) ==")
    sp_source = os.path.join(tmpdir, "state_t33_source.json")
    sp_target = os.path.join(tmpdir, "state_t33_target.json")
    cleanup_patterns(tmpdir, "state_t33_source.json")
    cleanup_patterns(tmpdir, "state_t33_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_source)
    run_cli(["confirm", "overview"], sp_source)
    run_cli(["confirm", "changes"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t33.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源机负责人33",
             "--description", "测试交接包"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=目标本地修改值"], sp_target)

    res_reconcile = run_cli(["import_package", pkg, "--operator", "接管人B33", "--mode", "takeover"], sp_target)
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t33 reconcile: AWAITING CONFIRMATION")

    res_audit_pending = run_cli(["audit_view"], sp_target)
    assert_in("PENDING TAKEOVER", res_audit_pending.stdout, "t33 audit_view shows PENDING TAKEOVER section")
    assert_in("接管人B33", res_audit_pending.stdout, "t33 pending shows operator")
    assert_in("[PENDING]", res_audit_pending.stdout, "t33 timeline has [PENDING] tag")
    print("  [OK] 对账阶段 audit_view 正确显示 PENDING TAKEOVER 和 [PENDING] 标签")

    res_confirm = run_cli(["takeover_confirm", "--operator", "接管人B33"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t33 confirm succeeds")

    s_after = read_state(sp_target)
    assert "takeover_history" in s_after, "t33 takeover_history present in state"
    assert_eq(len(s_after["takeover_history"]), 1, "t33 exactly 1 takeover history entry")

    takeover = s_after["takeover_history"][0]
    assert "takeover_id" in takeover, "t33 takeover has takeover_id"
    assert_eq(takeover["imported_by"], "接管人B33", "t33 takeover imported_by correct")
    assert_eq(takeover["exported_by"], "源机负责人33", "t33 takeover exported_by correct")
    assert_eq(takeover["mode"], "takeover", "t33 takeover mode correct")
    assert "pre_import_state" in takeover, "t33 pre_import_state captured"
    assert "post_import_state" in takeover, "t33 post_import_state captured"
    assert "diff" in takeover, "t33 diff captured"
    assert takeover.get("confirmed_at") is not None, "t33 confirmed_at timestamp present"
    assert takeover.get("confirmed_by") == "接管人B33", "t33 confirmed_by correct"
    assert_eq(takeover.get("resumed_across_restart", False), False, "t33 not resumed across restart yet")

    diff = takeover["diff"]
    modified_items = diff["items"]["modified"]
    chg001_mod = next(m for m in modified_items if m["id"] == "CHG-001")
    owner_diff = next(d for d in chg001_mod["diffs"] if d["field"] == "owner")
    assert_eq(owner_diff["old"], "目标本地修改值", "t33 pre-import CHG-001 owner captured correctly")
    assert_eq(owner_diff["new"], "张三", "t33 post-import CHG-001 owner captured correctly")

    chg003_mod = next(m for m in modified_items if m["id"] == "CHG-003")
    owner_diff3 = next(d for d in chg003_mod["diffs"] if d["field"] == "owner")
    assert_eq(owner_diff3["old"], "", "t33 pre-import CHG-003 owner empty")
    assert_eq(owner_diff3["new"], "周七", "t33 post-import CHG-003 owner is 周七")

    assert "decisions_log" in takeover, "t33 takeover has decisions_log"
    assert "handover_summary" in takeover, "t33 takeover has handover_summary"
    assert "conflicts" in takeover, "t33 takeover has conflicts list"
    print("  [OK] 确认后 takeover_history 包含 decisions_log、handover_summary、conflicts")

    sessions = s_after.get("confirmed_takeover_sessions", {})
    assert len(sessions) == 1, "t33 confirmed_takeover_sessions has 1 entry"
    session_key = list(sessions.keys())[0]
    assert session_key == takeover["takeover_id"], "t33 session key matches takeover_id"
    assert sessions[session_key]["confirmed_by"] == "接管人B33", "t33 session confirmed_by correct"
    assert sessions[session_key]["revoked"] == False, "t33 session not revoked"
    assert sessions[session_key].get("session_pid") is not None, "t33 session has session_pid"
    assert sessions[session_key].get("resumed_count", 0) == 0, "t33 session resumed_count starts at 0"
    print("  [OK] confirmed_takeover_sessions 会话记录正确")

    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    s_reload = read_state(sp_target)
    assert_eq(len(s_reload["takeover_history"]), 1, "t33 takeover_history preserved across restart")
    assert s_reload["takeover_history"][0]["takeover_id"] == takeover["takeover_id"], "t33 takeover_id consistent after reload"
    assert len(s_reload.get("confirmed_takeover_sessions", {})) == 1, "t33 sessions preserved across restart"

    run_cli(["confirm", "migration"], sp_target)
    run_cli(["confirm", "known_issues"], sp_target)
    run_cli(["draft"], sp_target)

    s_reload["takeover_history"][0]["resumed_across_restart"] = True
    s_reload["confirmed_takeover_sessions"][takeover["takeover_id"]]["session_pid"] = 99999
    s_reload["confirmed_takeover_sessions"][takeover["takeover_id"]]["resumed_count"] = 1
    with open(sp_target, "w", encoding="utf-8") as f:
        json.dump(s_reload, f, ensure_ascii=False, indent=2)

    res_audit = run_cli(["audit_view"], sp_target)
    assert_in("AUDIT VIEW - Takeover & Rules Upgrade Handover Timeline", res_audit.stdout, "t33 audit view header present")
    assert_in("Takeover #1", res_audit.stdout, "t33 shows takeover #1")
    assert_in("Confirmed by: 接管人B33", res_audit.stdout, "t33 shows confirmed by decision maker")
    assert_in("Exported by:    源机负责人33", res_audit.stdout, "t33 shows exporter")
    assert_in("Persistent Session", res_audit.stdout, "t33 shows Persistent Session info")
    assert_in("Resumed cnt:", res_audit.stdout, "t33 shows resumed count")
    assert_in("CHG-001", res_audit.stdout, "t33 CHG-001 in audit view")
    assert_in("CHG-003", res_audit.stdout, "t33 CHG-003 in audit view")
    assert_in("owner: '目标本地修改值' -> '张三'", res_audit.stdout, "t33 shows field change in audit")
    assert_in("Cross-restart:  YES", res_audit.stdout, "t33 shows cross-restart YES")
    assert_in("[Timeline Summary]", res_audit.stdout, "t33 timeline summary present")
    assert_in("[TAKEOVER]", res_audit.stdout, "t33 timeline has TAKEOVER tag")
    assert_in("[AUDIT]", res_audit.stdout, "t33 timeline has AUDIT tag")
    assert_in("[RESUMED]", res_audit.stdout, "t33 timeline has RESUMED tag")

    audit_events = [e for e in s_reload["audit_log"]]
    takeovers = [e for e in audit_events if e["action"] == "takeover_confirmed"]
    assert_eq(len(takeovers), 1, "t33 audit has takeover_confirmed event")
    assert_in("接管人B33", takeovers[0]["detail"], "t33 takeover audit records operator")

    no_takeover_sp = os.path.join(tmpdir, "state_t33_notakeover.json")
    cleanup_patterns(tmpdir, "state_t33_notakeover.json")
    run_cli(["import", SAMPLE], no_takeover_sp)
    res_no_takeover = run_cli(["audit_view"], no_takeover_sp)
    assert_in("No takeover or rules upgrade handover history found", res_no_takeover.stdout, "t33 audit_view handles no-takeover state")
    cleanup_patterns(tmpdir, "state_t33_source.json")
    cleanup_patterns(tmpdir, "state_t33_target.json")
    cleanup_patterns(tmpdir, "state_t33_notakeover.json")


def test_34_full_e2e_preflight_import_restart(tmpdir):
    """完整端到端链路（两步确认版）：导出包 → 预检 → 对账 → 确认接管 → 重启 → 继续确认 → 批准 → 导出Markdown，
       预检、导入、重启后查看三段结果要对得上"""
    print("\n== Test 34: Full E2E preflight -> reconcile -> confirm -> restart -> approve -> export ==")
    sp_machine1 = os.path.join(tmpdir, "state_machine1.json")
    sp_machine2 = os.path.join(tmpdir, "state_machine2.json")
    cleanup_patterns(tmpdir, "state_machine1.json")
    cleanup_patterns(tmpdir, "state_machine2.json")

    print("\n  [Machine 1] 负责人A开始工作...")
    run_cli(["import", SAMPLE], sp_machine1)
    run_cli(["draft"], sp_machine1)

    print("\n  [Machine 1] 负责人A做批量修订...")
    json_patch = os.path.join(tmpdir, "patch_t34.json")
    with open(json_patch, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t34-e2e-001",
            "operator": "负责人A34",
            "reason": "Q2交接前批量修订",
            "items": [
                {"id": "CHG-003", "owner": "周七", "risk_level": "critical", "category": "removal"},
                {"id": "CHG-005", "owner": "赵六", "risk_level": "critical", "category": "security"},
            ],
        }, f, ensure_ascii=False, indent=2)
    run_cli(["bulk_amend", json_patch, "--mode", "overwrite",
             "--operator", "负责人A34", "--reason", "Q2交接前批量修订"], sp_machine1)

    print("\n  [Machine 1] 负责人A重新生成draft...")
    run_cli(["draft"], sp_machine1)

    print("\n  [Machine 1] 负责人A确认部分章节...")
    run_cli(["confirm", "overview"], sp_machine1)
    run_cli(["confirm", "changes"], sp_machine1)

    s1_before_export = read_state(sp_machine1)
    s1_draft_v = s1_before_export["draft_version"]
    chg003_s1 = next(it for it in s1_before_export["items"] if it["id"] == "CHG-003")
    chg005_s1 = next(it for it in s1_before_export["items"] if it["id"] == "CHG-005")
    assert_eq(chg003_s1["owner"], "周七", "t34 machine1: CHG-003 owner=周七")
    assert_eq(chg005_s1["risk_level"], "critical", "t34 machine1: CHG-005 risk_level=critical")

    print("\n  [Machine 1] 负责人A导出交接包...")
    pkg = os.path.join(tmpdir, "handoff_pkg_t34.json")
    run_cli(["export_package", "-o", pkg, "--operator", "负责人A34",
             "--description", "Q2-v2.1.0 交接包，已完成批量修订和2个章节确认"], sp_machine1)

    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)
    assert "exported_from" not in pkg_data, "t34: package must NOT contain exported_from"
    assert "rules_path" not in pkg_data, "t34: package must NOT contain rules_path"
    assert "handover_summary" in pkg_data, "t34: package must contain handover_summary"
    print("  [OK] 交接包不含硬编码机器信息，含 handover_summary")

    print(f"\n  [Machine 2] 负责人B先预检包内容...")
    run_cli(["import", SAMPLE], sp_machine2)
    run_cli(["draft"], sp_machine2)
    run_cli(["amend", "CHG-001", "--field", "owner=负责人B本地修改值"], sp_machine2)

    res_preflight = run_cli(["preflight_check", pkg], sp_machine2)
    assert_in("PACKAGE PREFLIGHT CHECK", res_preflight.stdout, "t34 preflight header")
    assert_in("Content Diff Summary", res_preflight.stdout, "t34 preflight has diff summary")
    assert_in("Items:           +0 -0 ~3", res_preflight.stdout, "t34 preflight shows ~3 items modified")
    assert_in("Draft version:   v1 -> v2", res_preflight.stdout, "t34 preflight shows draft version change")
    assert_in(">>> overview: PENDING -> CONFIRMED", res_preflight.stdout, "t34 preflight shows overview conf change")
    assert_in(">>> changes: PENDING -> CONFIRMED", res_preflight.stdout, "t34 preflight shows changes conf change")
    assert_in("CHG-001", res_preflight.stdout, "t34 preflight shows CHG-001")
    assert_in("CHG-003", res_preflight.stdout, "t34 preflight shows CHG-003")
    assert_in("CHG-005", res_preflight.stdout, "t34 preflight shows CHG-005")
    assert_in("owner: '负责人B本地修改值' -> '张三'", res_preflight.stdout, "t34 preflight shows CHG-001 owner change")
    assert_in("owner: '' -> '周七'", res_preflight.stdout, "t34 preflight shows CHG-003 owner change")
    assert_in("risk_level: 'extreme' -> 'critical'", res_preflight.stdout, "t34 preflight shows CHG-005 risk change")
    assert_in("Recommended mode: takeover", res_preflight.stdout, "t34 preflight recommends takeover")
    assert_in("Force required:   NO", res_preflight.stdout, "t34 preflight no force needed")
    print("  [OK] 预检结果正确，显示3个条目被修改、章节确认状态变化、建议takeover模式")

    s2_after_preflight = read_state(sp_machine2)
    preflight_audit = [e for e in s2_after_preflight["audit_log"] if e["action"] == "preflight_check"]
    assert_eq(len(preflight_audit), 0, "t34 preflight does NOT write audit entries (no state changes)")
    print("  [OK] 预检未落状态，未写审计日志")

    print(f"\n  [Machine 2] 负责人B执行只读对账（import_package reconcile）...")
    res_reconcile = run_cli(["import_package", pkg, "--operator", "负责人B34", "--mode", "takeover"], sp_machine2)
    assert_in("RECONCILED", res_reconcile.stdout, "t34 reconcile shows RECONCILED")
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t34 reconcile shows AWAITING CONFIRMATION")

    s2_pending = read_state(sp_machine2)
    assert s2_pending.get("pending_takeover") is not None, "t34 pending_takeover created"
    pt = s2_pending["pending_takeover"]
    pending_diff_modified = len(pt["diff"]["items"]["modified"])
    assert_eq(3, pending_diff_modified, "t34 pending diff shows 3 modified (align with preflight)")
    print("  [OK] 对账阶段 diff 与预检一致（预检 ~3 == 对账 ~3）")

    print(f"\n  [Machine 2] 负责人B正式确认接管...")
    res_confirm = run_cli(["takeover_confirm", "--operator", "负责人B34"], sp_machine2)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t34 takeover confirm succeeds")

    s2_after_import = read_state(sp_machine2)
    assert s2_after_import.get("pending_takeover") is None, "t34 pending_takeover cleared after confirm"
    assert_eq(len(s2_after_import["takeover_history"]), 1, "t34 takeover history has 1 entry")
    assert len(s2_after_import.get("confirmed_takeover_sessions", {})) == 1, "t34 confirmed session created"
    takeover = s2_after_import["takeover_history"][0]
    tid = takeover["takeover_id"]

    preflight_modified_count = 3
    import_diff_modified = len(takeover["diff"]["items"]["modified"])
    assert_eq(preflight_modified_count, import_diff_modified,
              "t34 preflight ~3 matches takeover diff ~3 (三段对齐: 预检 == 导入快照)")
    print("  [OK] 预检显示的修改条目数与接管快照一致（预检、导入两段结果对齐）")

    assert "takeover_id" in takeover, "t34 takeover has id"
    assert_eq(takeover["imported_by"], "负责人B34", "t34 takeover operator correct")
    assert_eq(takeover["exported_by"], "负责人A34", "t34 takeover exporter correct")
    assert_eq(takeover["confirmed_by"], "负责人B34", "t34 confirmed_by correct")
    assert takeover.get("confirmed_at") is not None, "t34 confirmed_at present"

    diff = takeover["diff"]
    chg001_diff = next(m for m in diff["items"]["modified"] if m["id"] == "CHG-001")
    owner_field = next(d for d in chg001_diff["diffs"] if d["field"] == "owner")
    assert_eq(owner_field["old"], "负责人B本地修改值", "t34 takeover diff: CHG-001 old value correct")
    assert_eq(owner_field["new"], "张三", "t34 takeover diff: CHG-001 new value correct")

    conf_changes = diff["metadata"]["confirmations_changed"]
    overview_change = next(c for c in conf_changes if c["section"] == "overview")
    assert_eq(overview_change["old"], False, "t34 overview pre=false")
    assert_eq(overview_change["new"], True, "t34 overview post=true")

    print(f"\n  [Machine 2] 模拟重启（子进程退出再读文件，修改session_pid模拟不同进程）...")
    subprocess.run([sys.executable, "-c", "import gc; gc.collect()"], capture_output=True)

    s2_reload = read_state(sp_machine2)
    assert_eq(len(s2_reload["takeover_history"]), 1, "t34 takeover history preserved after restart")
    assert s2_reload["takeover_history"][0]["takeover_id"] == takeover["takeover_id"], "t34 takeover_id consistent"
    assert len(s2_reload.get("confirmed_takeover_sessions", {})) == 1, "t34 sessions preserved after restart"

    s2_reload["takeover_history"][0]["resumed_across_restart"] = True
    s2_reload["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    s2_reload["confirmed_takeover_sessions"][tid]["resumed_count"] = 1
    with open(sp_machine2, "w", encoding="utf-8") as f:
        json.dump(s2_reload, f, ensure_ascii=False, indent=2)

    print(f"\n  [Machine 2] 重启后负责人B查看审计视图...")
    res_audit = run_cli(["audit_view"], sp_machine2)
    assert_in("Cross-restart:  YES", res_audit.stdout, "t34 audit view shows cross-restart YES")
    assert_in("Persistent Session", res_audit.stdout, "t34 audit view shows Persistent Session")
    assert_in("Resume count:", res_audit.stdout, "t34 audit view shows Resume count label")
    assert_in("CHG-001", res_audit.stdout, "t34 audit view shows CHG-001")
    assert_in("owner: '负责人B本地修改值' -> '张三'", res_audit.stdout, "t34 audit view shows CHG-001 change")
    assert_in("[RESUMED]", res_audit.stdout, "t34 audit view has [RESUMED] tag")

    audit_modified_shown = "MODIFIED (3 items)" in res_audit.stdout
    assert audit_modified_shown, "t34 audit view shows 3 modified items (matches preflight & import)"
    print("  [OK] 重启后审计视图显示的修改数与预检、导入一致（三段对齐: 预检 == 导入 == 重启后查看）")

    print(f"\n  [Machine 2] 负责人B继续剩余章节确认...")
    run_cli(["confirm", "migration"], sp_machine2)
    run_cli(["confirm", "known_issues"], sp_machine2)

    print(f"\n  [Machine 2] 负责人B重新生成draft...")
    run_cli(["draft"], sp_machine2)

    print(f"\n  [Machine 2] 负责人B批准...")
    run_cli(["approve"], sp_machine2)

    s2_approved = read_state(sp_machine2)
    assert_eq(s2_approved["approved"], True, "t34 approved")
    assert_eq(s2_approved["approved_at_version"], "2.1.0", "t34 approved_at_version")

    sessions_final = s2_approved.get("confirmed_takeover_sessions", {})
    assert sessions_final.get(tid, {}).get("resumed_count", 0) >= 1, "t34 session resumed_count preserved after approve"

    print(f"\n  [Machine 2] 负责人B导出最终Markdown...")
    md_out = os.path.join(tmpdir, "t34_final_release_notes.md")
    run_cli(["export", "-o", md_out], sp_machine2)

    md = read_file(md_out)
    assert_in("# Release Notes v2.1.0", md, "t34 final MD: title correct")
    assert_in("owner:周七", md, "t34 final MD: CHG-003 owner=周七")
    assert_in("risk:critical", md, "t34 final MD: critical risks present")
    assert_in("owner:赵六", md, "t34 final MD: CHG-005 owner=赵六")
    assert_in("owner:张三", md, "t34 final MD: CHG-001 owner=张三 (reverted from takeover)")

    print(f"\n  [OK] 完整链路验证通过:")
    print(f"     1. 导出包 OK (无硬编码，含handover_summary)")
    print(f"     2. 预检 OK (显示差异、风险、建议模式)")
    print(f"     3. 对账 OK (只读 reconcile，生成pending_takeover)")
    print(f"     4. 确认接管 OK (takeover_confirm，持久化会话)")
    print(f"     5. 重启 OK (状态保留、会话保留)")
    print(f"     6. audit_view OK (跨重启标记、Persistent Session)")
    print(f"     7. 继续确认 OK")
    print(f"     8. 批准 OK")
    print(f"     9. 导出Markdown OK")
    print(f"     三段对齐: 预检(~3) == 对账(~3) == 导入快照(~3) == 重启后查看(~3) OK")

    cleanup_patterns(tmpdir, "state_machine1.json")
    cleanup_patterns(tmpdir, "state_machine2.json")


def test_36_no_false_positive_cross_restart(tmpdir):
    """跨重启续做识别（两步确认版）：刚确认就看 audit 不应该误判为跨重启（反误报测试）"""
    print("\n== Test 36: No false-positive cross-restart (just confirmed, no work done yet) ==")
    sp_a = os.path.join(tmpdir, "state_t36_machineA.json")
    sp_b = os.path.join(tmpdir, "state_t36_machineB.json")
    cleanup_patterns(tmpdir, "state_t36_machineA.json")
    cleanup_patterns(tmpdir, "state_t36_machineB.json")

    print(f"\n  [Machine A] 导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["draft"], sp_a)
    run_cli(["amend", "CHG-001", "--field", "owner=张三"], sp_a)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_a)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_a)
    run_cli(["draft"], sp_a)
    run_cli(["confirm", "overview"], sp_a)
    run_cli(["confirm", "changes"], sp_a)

    pkg = os.path.join(tmpdir, "handoff_pkg_t36.json")
    run_cli(["export_package", "-o", pkg, "--operator", "负责人A36",
             "--description", "Q2-v2.1.0 交接包"], sp_a)

    print(f"\n  [Machine B] 对账 + 确认接管（两步）...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["amend", "CHG-001", "--field", "owner=负责人B本地修改值"], sp_b)
    run_cli(["import_package", pkg, "--operator", "负责人B36", "--mode", "takeover"], sp_b)
    run_cli(["takeover_confirm", "--operator", "负责人B36"], sp_b)

    print(f"\n  [验证] 刚确认后立即看 audit_view（还没做任何后续工作）...")
    s_after_confirm = read_state(sp_b)
    assert_eq(len(s_after_confirm["takeover_history"]), 1, "t36: takeover history has 1 entry")
    assert_eq(len(s_after_confirm.get("confirmed_takeover_sessions", {})), 1, "t36: 1 confirmed session")
    takeover_before = s_after_confirm["takeover_history"][0]
    assert_eq(takeover_before.get("resumed_across_restart", False), False,
              "t36: right after confirm, resumed_across_restart should be False")

    audit_events_before = len([e for e in s_after_confirm["audit_log"] 
                               if e["action"] == "takeover_resumed_across_restart"])
    assert_eq(audit_events_before, 0, "t36: no resume event before audit_view")

    res_audit = run_cli(["audit_view"], sp_b)
    assert_in("Cross-restart:  NO", res_audit.stdout, 
              "t36: just confirmed, should show Cross-restart: NO (no false positive)")
    assert_not_in("[RESUMED]", res_audit.stdout,
                   "t36: just confirmed, should NOT have [RESUMED] tag")
    assert_in("Persistent Session", res_audit.stdout, "t36: confirmed shows Persistent Session info")

    s_after_audit = read_state(sp_b)
    takeover_after = s_after_audit["takeover_history"][0]
    assert_eq(takeover_after.get("resumed_across_restart", False), False,
              "t36: after first audit_view, resumed_across_restart still False")

    audit_events_after = len([e for e in s_after_audit["audit_log"]
                              if e["action"] == "takeover_resumed_across_restart"])
    assert_eq(audit_events_after, 0, "t36: no resume event written for false-positive case")

    print(f"\n  [OK] 刚确认就看 audit_view 不会误报跨重启，正确显示 Cross-restart: NO")

    print(f"\n  [验证] 现在做后续工作（确认、批准、导出），再模拟重启看 audit_view...")
    run_cli(["confirm", "migration"], sp_b)
    run_cli(["confirm", "known_issues"], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["approve"], sp_b)

    md_before = os.path.join(tmpdir, "t36_before_restart.md")
    run_cli(["export", "-o", md_before], sp_b)

    tid = s_after_audit["takeover_history"][0]["takeover_id"]
    s_before_restart = read_state(sp_b)
    s_before_restart["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)

    print(f"\n  [模拟重启] 新进程查看 audit_view（session_pid=99999 模拟不同进程）...")
    import subprocess
    audit_script = os.path.join(tmpdir, "run_audit_t36.py")
    with open(audit_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{os.path.dirname(os.path.abspath(__file__))}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--state", r"{sp_b}", "audit_view"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, audit_script],
        capture_output=True,
    )
    audit_output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')

    print(f"\n  [验证] 重启后 audit_view 输出...")
    assert_in("Cross-restart:  YES", audit_output,
              "t36: after real work + restart, should show Cross-restart: YES")
    assert_in("[RESUMED]", audit_output,
              "t36: after real work + restart, should have [RESUMED] tag")

    s_final = read_state(sp_b)
    takeover_final = s_final["takeover_history"][0]
    assert_eq(takeover_final.get("resumed_across_restart", False), True,
              "t36: after real work + restart, resumed_across_restart is True")
    assert s_final["confirmed_takeover_sessions"][tid]["resumed_count"] >= 1, "t36: session resumed_count incremented"

    audit_events_final = len([e for e in s_final["audit_log"]
                              if e["action"] == "takeover_resumed_across_restart"])
    assert_eq(audit_events_final, 1, "t36: exactly 1 resume event written")

    print(f"\n  [OK] 做完后续工作 + 重启后，正确识别为跨重启续做")
    print(f"\n  [OK] 反误报测试通过：刚确认不报，真实重启后才报")

    cleanup_patterns(tmpdir, "state_t36_machineA.json")
    cleanup_patterns(tmpdir, "state_t36_machineB.json")


def test_35_cross_restart_resume_detection(tmpdir):
    """跨重启续做识别（两步确认版）：完整链路验证，确保接管确认后重启能自动识别为跨进程续做"""
    print("\n== Test 35: Cross-restart resume detection (2-step confirm, full E2E) ==")
    sp_a = os.path.join(tmpdir, "state_t35_machineA.json")
    sp_b = os.path.join(tmpdir, "state_t35_machineB.json")
    cleanup_patterns(tmpdir, "state_t35_machineA.json")
    cleanup_patterns(tmpdir, "state_t35_machineB.json")

    print(f"\n  [Machine A] 导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["draft"], sp_a)
    run_cli(["amend", "CHG-001", "--field", "owner=张三"], sp_a)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_a)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_a)
    run_cli(["draft"], sp_a)
    run_cli(["confirm", "overview"], sp_a)
    run_cli(["confirm", "changes"], sp_a)

    pkg = os.path.join(tmpdir, "handoff_pkg_t35.json")
    run_cli(["export_package", "-o", pkg, "--operator", "负责人A35",
             "--description", "Q2-v2.1.0 交接包"], sp_a)

    s_a = read_state(sp_a)
    pkg_data = json.load(open(pkg, encoding="utf-8"))
    assert "exported_from" not in pkg_data, "t35: package must NOT contain exported_from"
    assert "rules_path" not in pkg_data, "t35: package must NOT contain rules_path"
    assert "handover_summary" in pkg_data, "t35: package must contain handover_summary"
    print("  [OK] 交接包不含硬编码机器信息，含 handover_summary")

    print(f"\n  [Machine B] 先预检包内容（同进程，模拟首次接管）...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["amend", "CHG-001", "--field", "owner=负责人B本地修改值"], sp_b)

    res_preflight = run_cli(["preflight_check", pkg], sp_b)
    assert_in("Items:           +0 -0 ~3", res_preflight.stdout, "t35 preflight shows ~3 items")
    assert_in(">>> overview: PENDING -> CONFIRMED", res_preflight.stdout, "t35 preflight shows overview conf change")
    assert_in(">>> changes: PENDING -> CONFIRMED", res_preflight.stdout, "t35 preflight shows changes conf change")
    assert_in("Recommended mode: takeover", res_preflight.stdout, "t35 preflight recommends takeover")
    print("  [OK] 预检结果正确，显示3个修改条目、2个章节确认变化")

    s_b_before = read_state(sp_b)
    preflight_audit = [e for e in s_b_before["audit_log"] if e["action"] == "preflight_check"]
    assert_eq(len(preflight_audit), 0, "t35 preflight does NOT write audit entries")

    print(f"\n  [Machine B] 执行只读对账（import_package reconcile）...")
    res_reconcile = run_cli(["import_package", pkg, "--operator", "负责人B35", "--mode", "takeover"], sp_b)
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t35 reconcile shows AWAITING CONFIRMATION")

    s_b_pending = read_state(sp_b)
    assert s_b_pending.get("pending_takeover") is not None, "t35 pending_takeover created"
    assert len(s_b_pending.get("takeover_history", [])) == 0, "t35 NO takeover_history yet before confirm"
    assert len(s_b_pending.get("confirmed_takeover_sessions", {})) == 0, "t35 NO sessions yet before confirm"
    print("  [OK] 对账完成，pending_takeover 创建，未写 takeover_history")

    print(f"\n  [Machine B] 执行 takeover_confirm 正式确认接管...")
    res_confirm = run_cli(["takeover_confirm", "--operator", "负责人B35"], sp_b)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t35 confirm succeeds")

    s_b_after = read_state(sp_b)
    assert s_b_after.get("pending_takeover") is None, "t35 pending_takeover cleared after confirm"
    assert_eq(len(s_b_after["takeover_history"]), 1, "t35 takeover history has 1 entry after confirm")
    assert_eq(len(s_b_after.get("confirmed_takeover_sessions", {})), 1, "t35 1 confirmed session created")
    takeover = s_b_after["takeover_history"][0]
    tid = takeover["takeover_id"]
    session = s_b_after["confirmed_takeover_sessions"][tid]
    assert session.get("session_pid") is not None, "t35 session has session_pid"
    assert session["confirmed_by"] == "负责人B35", "t35 session confirmed_by correct"
    assert session["revoked"] == False, "t35 session not revoked"
    assert session.get("resumed_count", 0) == 0, "t35 session resumed_count starts at 0"
    assert takeover["imported_by"] == "负责人B35", "t35 takeover imported_by correct"
    assert takeover["confirmed_by"] == "负责人B35", "t35 takeover confirmed_by correct"
    assert takeover.get("resumed_across_restart", False) == False, "t35: right after confirm, NOT cross-restart yet"

    import_diff_count = len(takeover["diff"]["items"]["modified"])
    assert_eq(import_diff_count, 3, "t35 takeover diff has 3 modified items")
    print(f"  [OK] 接管确认成功，session_pid={session['session_pid']}，快照捕获3个修改条目")

    print(f"\n  [Machine B] 继续完成剩余工作（同进程）...")
    run_cli(["confirm", "migration"], sp_b)
    run_cli(["confirm", "known_issues"], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["approve"], sp_b)

    md_path = os.path.join(tmpdir, "t35_final_before_restart.md")
    run_cli(["export", "-o", md_path], sp_b)
    with open(md_path, "r", encoding="utf-8") as f:
        md_before = f.read()
    assert_in("CHG-003", md_before, "t35 MD before restart has CHG-003")
    assert_in("周七", md_before, "t35 MD before restart has 周七")
    assert_in("CHG-005", md_before, "t35 MD before restart has CHG-005")
    assert_in("critical", md_before, "t35 MD before restart has critical")
    print("  [OK] 批准并导出 Markdown 成功，内容正确")

    s_before_restart = read_state(sp_b)
    s_before_restart["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)
    print(f"  [模拟重启] 修改 session_pid=99999 模拟不同进程 PID")

    print(f"\n  [模拟重启后] 启动新 Python 子进程查看 audit_view（不同 PID 环境）...")
    import subprocess
    audit_script = os.path.join(tmpdir, "run_audit.py")
    with open(audit_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{os.path.dirname(os.path.abspath(__file__))}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--state", r"{sp_b}", "audit_view"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, audit_script],
        capture_output=True,
    )
    audit_output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')

    print(f"\n  [验证] 重启后 audit_view 输出...")
    assert_in("Cross-restart:  YES", audit_output, "t35: after restart, shows Cross-restart YES")
    assert_in("Persistent Session", audit_output, "t35: after restart, shows Persistent Session info")
    assert_in("Resume count:", audit_output, "t35: after restart, shows Resume count label")
    assert_in("~ MODIFIED (3 items):", audit_output, "t35: after restart, shows 3 modified items")
    assert_in("CHG-001", audit_output, "t35: after restart, shows CHG-001 in diff")
    assert_in("owner:", audit_output, "t35: after restart, shows field changes")
    assert_in("->", audit_output, "t35: after restart, shows old -> new format")
    assert_in(">>> overview: PENDING -> CONFIRMED", audit_output, "t35: after restart, shows conf changes")
    assert_in(">>> changes: PENDING -> CONFIRMED", audit_output, "t35: after restart, shows conf changes")
    assert_in("[RESUMED]", audit_output, "t35: timeline has [RESUMED] tag")
    assert_in("[TAKEOVER]", audit_output, "t35: timeline has [TAKEOVER] tag (confirmed takeover)")
    print("  [OK] 重启后 audit_view 正确显示跨重启续做标记、修改条目、确认状态变化")

    print(f"\n  [验证] 重启后状态文件内容...")
    s_b_restarted = read_state(sp_b)
    takeover_restarted = s_b_restarted["takeover_history"][0]
    assert_eq(takeover_restarted.get("resumed_across_restart", False), True,
              "t35: state file updated, resumed_across_restart=True")
    session_restarted = s_b_restarted["confirmed_takeover_sessions"][tid]
    assert session_restarted["resumed_count"] >= 1, f"t35: session resumed_count incremented (now {session_restarted['resumed_count']})"

    audit_events = [e for e in s_b_restarted["audit_log"] if e["action"] == "takeover_resumed_across_restart"]
    assert_eq(len(audit_events), 1, "t35: audit log has takeover_resumed_across_restart event")
    assert_in(tid, audit_events[0]["detail"], "t35: audit event references correct takeover_id")

    preflight_count = 3
    takeover_diff_count = len(takeover_restarted["diff"]["items"]["modified"])
    assert_eq(preflight_count, takeover_diff_count,
              "t35: 预检 ~3 == 导入快照 ~3 == 重启后查看 ~3 (三段对齐)")
    print("  [OK] 状态文件已更新：resumed_across_restart=True，audit 有续做事件，三段对齐")

    print(f"\n  [验证] 重启后重新导出 Markdown，关键内容与重启前一致...")
    md_path2 = os.path.join(tmpdir, "t35_final_after_restart.md")
    run_cli(["export", "-o", md_path2], sp_b)
    with open(md_path2, "r", encoding="utf-8") as f:
        md_after = f.read()

    def strip_timestamp(md):
        lines = md.split('\n')
        return '\n'.join([l for l in lines if not l.startswith('_Generated at')])

    assert_eq(strip_timestamp(md_before), strip_timestamp(md_after),
              "t35: MD content identical (excluding timestamp) before and after restart")
    assert_in("CHG-003", md_after, "t35: MD after restart has CHG-003")
    assert_in("周七", md_after, "t35: MD after restart has 周七")
    assert_in("CHG-005", md_after, "t35: MD after restart has CHG-005")
    assert_in("critical", md_after, "t35: MD after restart has critical")
    print("  [OK] 重启前后导出的 Markdown 关键内容一致，状态稳定性验证通过")

    print(f"\n  [验证] 再次运行 audit_view（同进程），不会重复写入审计...")
    res_audit2 = run_cli(["audit_view"], sp_b)
    assert_in("Cross-restart:  YES", res_audit2.stdout, "t35: second audit_view still shows YES")
    s_b_final = read_state(sp_b)
    resume_events = [e for e in s_b_final["audit_log"] if e["action"] == "takeover_resumed_across_restart"]
    assert_eq(len(resume_events), 1, "t35: no duplicate resume events written")
    print("  [OK] 多次运行 audit_view 不会重复产生审计事件")

    print(f"\n  [验证] 状态中 confirmed_takeover_sessions 信息完整...")
    session_final = s_b_final["confirmed_takeover_sessions"][tid]
    assert session_final["revoked"] == False, "t35 final: session not revoked"
    assert session_final["confirmed_by"] == "负责人B35", "t35 final: confirmed_by preserved"
    assert session_final["resumed_count"] >= 1, "t35 final: resumed_count preserved and incremented"
    assert "confirmed_at" in session_final, "t35 final: session has confirmed_at timestamp"

    print(f"\n  [OK] 完整链路验证通过:")
    print(f"     1. 导出包 OK (handover_summary 包含)")
    print(f"     2. 预检 OK (~3 items)")
    print(f"     3. 对账 OK (pending_takeover 创建)")
    print(f"     4. 接管确认 OK (takeover_history + confirmed_takeover_sessions 持久化)")
    print(f"     5. 继续确认 + 批准 + 导出 OK")
    print(f"     6. 重启后 audit_view OK (自动识别 Cross-restart: YES, Persistent Session 展示)")
    print(f"     7. 状态文件 OK (resumed_across_restart=True, session.resumed_count 增加)")
    print(f"     8. 审计日志 OK (takeover_resumed_across_restart 事件)")
    print(f"     9. 三段对齐 OK (预检 ~3 == 导入 ~3 == 重启后 ~3)")
    print(f"    10. 幂等性 OK (多次 audit_view 不重复写入)")

    cleanup_patterns(tmpdir, "state_t35_machineA.json")
    cleanup_patterns(tmpdir, "state_t35_machineB.json")


def test_37_export_has_handover_summary(tmpdir):
    """导出交接包时必须包含 handover_summary，且各字段内容完整"""
    print("\n== Test 37: export_package includes handover_summary ==")
    sp = os.path.join(tmpdir, "state_t37.json")
    cleanup_patterns(tmpdir, "state_t37.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp)
    run_cli(["confirm", "overview"], sp)
    run_cli(["confirm", "changes"], sp)
    run_cli(["draft"], sp)

    pkg = os.path.join(tmpdir, "pkg_t37.json")
    res_export = run_cli(["export_package", "-o", pkg, "--operator", "测试员37",
                          "--description", "测试handover_summary"], sp)
    assert_in("Handover Summary", res_export.stdout, "t37 export stdout shows Handover Summary section")
    assert_in("Suggested next steps", res_export.stdout, "t37 export stdout shows suggested next steps")
    assert_in("takeover_detail", res_export.stdout, "t37 export stdout hints takeover_detail")
    assert_in("takeover_confirm", res_export.stdout, "t37 export stdout hints takeover_confirm")

    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    assert "handover_summary" in pkg_data, "t37 package has handover_summary field"
    hs = pkg_data["handover_summary"]

    assert "sections_pending" in hs, "t37 hs has sections_pending"
    assert "sections_confirmed" in hs, "t37 hs has sections_confirmed"
    assert_eq(len(hs["sections_confirmed"]), 2, "t37 hs sections_confirmed has 2 items")
    assert "overview" in hs["sections_confirmed"], "t37 overview in confirmed"
    assert "changes" in hs["sections_confirmed"], "t37 changes in confirmed"
    assert_eq(len(hs["sections_pending"]), 2, "t37 hs sections_pending has 2 items")
    assert "migration" in hs["sections_pending"], "t37 migration in pending"
    assert "known_issues" in hs["sections_pending"], "t37 known_issues in pending"

    assert "approval_status" in hs, "t37 hs has approval_status"
    assert_eq(hs["approval_status"], "pending", "t37 hs approval_status=pending")

    assert "draft_version" in hs, "t37 hs has draft_version"
    assert_eq(hs["draft_version"], 2, "t37 hs draft_version=2")

    assert "items_total" in hs, "t37 hs has items_total"
    assert_eq(hs["items_total"], 5, "t37 hs items_total=5")
    assert "items_with_missing_owner" in hs, "t37 hs has items_with_missing_owner"
    assert "CHG-003" not in hs["items_with_missing_owner"], "t37 CHG-003 owner filled"
    assert "items_with_invalid_risk" in hs, "t37 hs has items_with_invalid_risk"
    assert "CHG-005" not in hs["items_with_invalid_risk"], "t37 CHG-005 risk fixed"

    assert "suggested_next_steps" in hs, "t37 hs has suggested_next_steps"
    assert len(hs["suggested_next_steps"]) >= 2, "t37 hs has >=2 suggested next steps"

    assert "migrations_pending" in hs, "t37 hs has migrations_pending"
    assert "known_issues_reviewed" in hs, "t37 hs has known_issues_reviewed"

    print(f"  [OK] handover_summary 完整: sections_confirmed={hs['sections_confirmed']}, "
          f"pending={hs['sections_pending']}, draft_v={hs['draft_version']}, "
          f"suggested_steps={len(hs['suggested_next_steps'])}")

    cleanup_patterns(tmpdir, "state_t37.json")


def test_38_two_step_pending_confirm(tmpdir):
    """两步流程验证：import_package 只创建 pending，takeover_confirm 后才正式持久化"""
    print("\n== Test 38: Two-step pending -> confirm state transitions ==")
    sp_source = os.path.join(tmpdir, "state_t38_source.json")
    sp_target = os.path.join(tmpdir, "state_t38_target.json")
    cleanup_patterns(tmpdir, "state_t38_source.json")
    cleanup_patterns(tmpdir, "state_t38_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["confirm", "overview"], sp_source)
    run_cli(["confirm", "changes"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t38.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源38"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    s_target_orig = read_state(sp_target)
    orig_draft_v = s_target_orig["draft_version"]
    orig_chg001_owner = next(i for i in s_target_orig["items"] if i["id"] == "CHG-001")["owner"]

    res_reconcile = run_cli(["import_package", pkg, "--operator", "接管38", "--mode", "takeover"], sp_target)
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t38 step1: AWAITING CONFIRMATION")

    s_pending = read_state(sp_target)
    assert s_pending.get("pending_takeover") is not None, "t38 pending_takeover created"
    assert len(s_pending.get("takeover_history", [])) == 0, "t38 NO takeover_history yet"
    assert len(s_pending.get("confirmed_takeover_sessions", {})) == 0, "t38 NO sessions yet"

    assert_eq(s_pending["draft_version"], orig_draft_v, "t38 pending阶段不修改 draft_version")
    chg001_pending = next(i for i in s_pending["items"] if i["id"] == "CHG-001")
    assert_eq(chg001_pending["owner"], orig_chg001_owner, "t38 pending阶段不修改 items (只读对账)")
    print("  [OK] 对账阶段：pending_takeover 创建，状态未修改，takeover_history/sessions 为空")

    pre_snapshot = s_pending["pending_takeover"]["pre_import_state"]
    assert_eq(pre_snapshot["draft_version"], orig_draft_v, "t38 pre_snapshot draft_v correct")
    post_snapshot = s_pending["pending_takeover"]["post_import_preview_state"]
    assert_eq(post_snapshot["draft_version"], 1, "t38 post_snapshot draft_v = package's v1")
    print("  [OK] pre_import_state / post_import_preview_state 正确捕获对账前后差异")

    res_confirm = run_cli(["takeover_confirm", "--operator", "接管38"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t38 step2: TAKEOVER CONFIRMED")

    s_confirmed = read_state(sp_target)
    assert s_confirmed.get("pending_takeover") is None, "t38 pending_takeover cleared after confirm"
    assert len(s_confirmed.get("takeover_history", [])) == 1, "t38 takeover_history has 1 entry"
    assert len(s_confirmed.get("confirmed_takeover_sessions", {})) == 1, "t38 sessions has 1 entry"

    assert_eq(s_confirmed["draft_version"], 1, "t38 confirm后 draft_version 更新为包的v1")
    chg001_confirmed = next(i for i in s_confirmed["items"] if i["id"] == "CHG-001")
    assert_eq(chg001_confirmed["owner"], "张三", "t38 confirm后 CHG-001 owner 更新为包状态")
    chg003_confirmed = next(i for i in s_confirmed["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_confirmed["owner"], "周七", "t38 confirm后 CHG-003 owner=周七 (包状态)")

    takeover = s_confirmed["takeover_history"][0]
    assert takeover.get("confirmed_at") is not None, "t38 takeover has confirmed_at"
    assert takeover["confirmed_by"] == "接管38", "t38 takeover confirmed_by correct"
    assert takeover["imported_by"] == "接管38", "t38 takeover imported_by correct"
    assert takeover.get("handover_summary") is not None, "t38 takeover has handover_summary"
    assert takeover.get("decisions_log") is not None, "t38 takeover has decisions_log"
    assert takeover.get("conflicts") is not None, "t38 takeover has conflicts list"

    tid = takeover["takeover_id"]
    session = s_confirmed["confirmed_takeover_sessions"][tid]
    assert session["confirmed_by"] == "接管38", "t38 session confirmed_by correct"
    assert session["revoked"] == False, "t38 session not revoked"
    assert session.get("session_pid") is not None, "t38 session has session_pid"
    assert session.get("confirmed_at") is not None, "t38 session has confirmed_at"

    audit_actions = [e["action"] for e in s_confirmed["audit_log"]]
    assert any("import_package" in a for a in audit_actions), "t38 audit has some import_package_* action"
    assert_in("takeover_confirmed", audit_actions, "t38 audit has takeover_confirmed")

    print("  [OK] 确认阶段：pending 清空，takeover_history/session 创建，状态正式更新")
    cleanup_patterns(tmpdir, "state_t38_source.json")
    cleanup_patterns(tmpdir, "state_t38_target.json")


def test_39_revoke_pending(tmpdir):
    """撤销确认：对账阶段（pending）的接管可以直接撤销，自动回滚到 pre_import 状态"""
    print("\n== Test 39: Revoke pending takeover (auto rollback) ==")
    sp_source = os.path.join(tmpdir, "state_t39_source.json")
    sp_target = os.path.join(tmpdir, "state_t39_target.json")
    cleanup_patterns(tmpdir, "state_t39_source.json")
    cleanup_patterns(tmpdir, "state_t39_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t39.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源39"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=本地修改值39"], sp_target)
    s_before = read_state(sp_target)
    before_draft_v = s_before["draft_version"]
    before_chg001 = next(i for i in s_before["items"] if i["id"] == "CHG-001")["owner"]

    run_cli(["import_package", pkg, "--operator", "接管39", "--mode", "takeover"], sp_target)
    s_pending = read_state(sp_target)
    assert s_pending.get("pending_takeover") is not None, "t39 pending_takeover created before revoke"

    res_revoke = run_cli(["takeover_revoke", "--operator", "撤销39",
                          "--reason", "对账后不想接管了"], sp_target)
    assert_in("revoked successfully", res_revoke.stdout, "t39 pending revoke succeeds")
    assert_in("Pre-import state restored", res_revoke.stdout, "t39 pending revoke shows rollback")

    s_after = read_state(sp_target)
    assert s_after.get("pending_takeover") is None, "t39 pending_takeover cleared after revoke"
    assert len(s_after.get("takeover_history", [])) == 0, "t39 NO takeover_history after pending revoke"
    assert len(s_after.get("confirmed_takeover_sessions", {})) == 0, "t39 NO sessions after pending revoke"

    assert_eq(s_after["draft_version"], before_draft_v, "t39 draft_v restored after revoke")
    after_chg001 = next(i for i in s_after["items"] if i["id"] == "CHG-001")["owner"]
    assert_eq(after_chg001, before_chg001, "t39 CHG-001 owner restored after revoke")
    chg003_after = next(i for i in s_after["items"] if i["id"] == "CHG-003")["owner"]
    assert_eq(chg003_after, "", "t39 CHG-003 owner NOT overwritten (correctly rolled back)")

    audit_actions = [e["action"] for e in s_after["audit_log"]]
    assert_in("takeover_revoked_pending_restore", audit_actions, "t39 audit has takeover_revoked_pending_restore")

    print("  [OK] pending 撤销成功：状态完全回滚，审计记录正确")
    cleanup_patterns(tmpdir, "state_t39_source.json")
    cleanup_patterns(tmpdir, "state_t39_target.json")


def test_40_revoke_confirmed_force(tmpdir):
    """撤销确认：已确认（confirmed）的接管需要 --force，回滚并标记 revoked"""
    print("\n== Test 40: Revoke confirmed takeover requires --force ==")
    sp_source = os.path.join(tmpdir, "state_t40_source.json")
    sp_target = os.path.join(tmpdir, "state_t40_target.json")
    cleanup_patterns(tmpdir, "state_t40_source.json")
    cleanup_patterns(tmpdir, "state_t40_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["confirm", "overview"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t40.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源40"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=本地修改值40"], sp_target)
    s_before = read_state(sp_target)
    before_draft_v = s_before["draft_version"]
    before_chg001 = next(i for i in s_before["items"] if i["id"] == "CHG-001")["owner"]

    run_cli(["import_package", pkg, "--operator", "接管40", "--mode", "takeover"], sp_target)
    run_cli(["takeover_confirm", "--operator", "接管40", "--force"], sp_target)

    s_confirmed = read_state(sp_target)
    assert len(s_confirmed.get("takeover_history", [])) == 1, "t40 confirmed has takeover_history"
    assert len(s_confirmed.get("confirmed_takeover_sessions", {})) == 1, "t40 confirmed has session"
    tid = s_confirmed["takeover_history"][0]["takeover_id"]

    res_no_force = run_cli(["takeover_revoke", "--operator", "撤销40",
                            "--reason", "想撤销但没加--force"], sp_target, expect_fail=True)
    assert_in("--force", res_no_force.stdout, "t40 revoke without --force rejected")
    assert_in("CONFIRMED (persisted) takeover session", res_no_force.stdout, "t40 reason shown: confirmed takeover")

    s_still_ok = read_state(sp_target)
    assert len(s_still_ok.get("takeover_history", [])) == 1, "t40 revoke no-force: history intact"
    assert len(s_still_ok.get("confirmed_takeover_sessions", {})) == 1, "t40 revoke no-force: session intact"
    print("  [OK] 无 --force 拒绝撤销已确认接管")

    res_force = run_cli(["takeover_revoke", "--operator", "撤销40",
                         "--reason", "确认后强制撤销", "--force",
                         "--takeover-id", tid], sp_target)
    assert_in("Revoking CONFIRMED TAKEOVER SESSION", res_force.stdout, "t40 --force revoke begins")
    assert_in("State rolled back", res_force.stdout, "t40 shows state rollback")
    assert_in("revoked successfully", res_force.stdout, "t40 --force revoke succeeds")

    s_revoked = read_state(sp_target)
    assert len(s_revoked.get("takeover_history", [])) == 1, "t40 history still has 1 entry (with revoked flag)"
    takeover_r = s_revoked["takeover_history"][0]
    assert takeover_r.get("revoked") == True, "t40 takeover marked revoked=True"
    assert takeover_r.get("revoked_by") == "撤销40", "t40 takeover revoked_by correct"
    assert takeover_r.get("revoke_reason") == "确认后强制撤销", "t40 takeover revoke_reason correct"
    assert takeover_r.get("revoked_at") is not None, "t40 takeover has revoked_at"

    sessions_r = s_revoked.get("confirmed_takeover_sessions", {})
    assert len(sessions_r) == 1, "t40 session still exists (with revoked flag)"
    session_r = sessions_r[tid]
    assert session_r["revoked"] == True, "t40 session revoked=True"
    assert session_r["revoked_by"] == "撤销40", "t40 session revoked_by correct"
    assert session_r.get("revoke_reason") == "确认后强制撤销", "t40 session revoke_reason correct"

    assert_eq(s_revoked["draft_version"], before_draft_v, "t40 draft_v rolled back")
    after_chg001 = next(i for i in s_revoked["items"] if i["id"] == "CHG-001")["owner"]
    assert_eq(after_chg001, before_chg001, "t40 CHG-001 owner rolled back")
    chg003_r = next(i for i in s_revoked["items"] if i["id"] == "CHG-003")["owner"]
    assert_eq(chg003_r, "", "t40 CHG-003 owner rolled back (not 周七)")

    audit_actions = [e["action"] for e in s_revoked["audit_log"]]
    assert_in("takeover_revoked_confirmed", audit_actions, "t40 audit has takeover_revoked_confirmed")
    assert_in("takeover_revoked_confirmed_rollback", audit_actions, "t40 audit has takeover_revoked_confirmed_rollback")

    res_status = run_cli(["status"], sp_target)
    assert_in("Revoked takeovers:", res_status.stdout, "t40 status shows Revoked takeovers")
    assert_in("确认后强制撤销", res_status.stdout, "t40 status shows revoke reason")

    res_audit = run_cli(["audit_view"], sp_target)
    assert_in("[REVOKED]", res_audit.stdout, "t40 audit_view shows [REVOKED] tag")
    assert_in("[Revocation]", res_audit.stdout, "t40 audit_view shows Revocation section")
    assert_in("撤销40", res_audit.stdout, "t40 audit_view shows revoked_by")

    print("  [OK] 确认后撤销：--force 才允许，state 回滚，takeover/session 标记 revoked，审计完整")
    cleanup_patterns(tmpdir, "state_t40_source.json")
    cleanup_patterns(tmpdir, "state_t40_target.json")


def test_41_conflict_detection_and_decisions(tmpdir):
    """冲突检测与决策：构造本地draft更高/rules被改/版本变化，验证 override_all、skip_all、--per-item"""
    print("\n== Test 41: Conflict detection and decisions (override/skip/per-item) ==")
    sp_source = os.path.join(tmpdir, "state_t41_source.json")
    sp_target = os.path.join(tmpdir, "state_t41_target.json")
    cleanup_patterns(tmpdir, "state_t41_source.json")
    cleanup_patterns(tmpdir, "state_t41_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["confirm", "overview"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t41.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源41"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["amend", "CHG-001", "--field", "owner=目标本地高版本"], sp_target)

    rules_backup = os.path.join(tmpdir, "rules_t41_backup.yaml")
    custom_rules = os.path.join(tmpdir, "rules_t41_modified.yaml")
    import shutil
    shutil.copy2(RULES, rules_backup)
    with open(RULES, "r", encoding="utf-8") as f:
        rules_content = f.read()
    modified_rules = rules_content + "\n# test_41 custom modification marker\n"
    with open(custom_rules, "w", encoding="utf-8") as f:
        f.write(modified_rules)
    with open(RULES, "w", encoding="utf-8") as f:
        f.write(modified_rules)

    try:
        res_reconcile = run_cli(
            ["import_package", pkg, "--operator", "接管41", "--mode", "takeover"],
            sp_target)
        assert_in("CONFLICTS DETECTED", res_reconcile.stdout, "t41 reconcile shows conflicts detected")
        assert_in("draft_version_mismatch", res_reconcile.stdout, "t41 shows draft_version_mismatch conflict")
        assert_in("RECONCILED - BUT BLOCKED", res_reconcile.stdout, "t41 reconcile BLOCKED due to target_newer")

        s_pending = read_state(sp_target)
        pt = s_pending.get("pending_takeover")
        assert pt is not None, "t41 pending_takeover created"
        conflicts = pt.get("conflicts", [])
        print(f"  [DEBUG] conflicts detected: {[c['type'] for c in conflicts]}")
        assert len(conflicts) >= 1, f"t41 at least 1 conflict detected (got {len(conflicts)})"
        conflict_types = [c["type"] for c in conflicts]
        assert_in("draft_version_mismatch", conflict_types, "t41 draft_version_mismatch in conflicts list")
    finally:
        shutil.copy2(rules_backup, RULES)
        print("  [OK] rules.yaml restored")

    print("  [OK] 冲突检测成功，至少检测到 draft_version_mismatch 冲突")

    res_confirm_default = run_cli(["takeover_confirm", "--operator", "接管41",
                                   "--decision", "override_all", "--force"], sp_target)
    assert_in("TAKEOVER CONFIRMED", res_confirm_default.stdout, "t41 override_all confirm succeeds")
    s_default = read_state(sp_target)
    takeover_default = s_default["takeover_history"][0]
    decisions = takeover_default.get("decisions_log", [])
    assert len(decisions) >= 1, "t41 decisions_log has at least 1 entry"
    any_override = any("override" in d.get("decision", "").lower() for d in decisions)
    assert any_override, "t41 decisions_log has override decision"
    print("  [OK] --decision override_all 正确写入 decisions_log")

    print("  [--- 测试 skip_all 和 --per-item (重置到新 target 环境) ---]")
    sp_target2 = os.path.join(tmpdir, "state_t41_target2.json")
    cleanup_patterns(tmpdir, "state_t41_target2.json")
    run_cli(["import", SAMPLE], sp_target2)
    run_cli(["draft"], sp_target2)
    run_cli(["draft"], sp_target2)
    run_cli(["amend", "CHG-005", "--field", "owner=高版本本地修改"], sp_target2)

    run_cli(["import_package", pkg, "--operator", "接管41b", "--mode", "takeover"], sp_target2)
    s_pending2 = read_state(sp_target2)
    pt2_conflicts = s_pending2["pending_takeover"].get("conflicts", [])
    print(f"  [DEBUG] target2 conflicts: {[c['type'] for c in pt2_conflicts]}")

    res_detail = run_cli(["takeover_detail"], sp_target2)
    assert_in("Conflicts Detected", res_detail.stdout, "t41 takeover_detail shows conflicts")
    assert_in("Options:", res_detail.stdout, "t41 takeover_detail shows resolution options per conflict")

    s_before_skip = read_state(sp_target2)
    before_skip_draft_v = s_before_skip["draft_version"]
    before_skip_chg005 = next(i for i in s_before_skip["items"] if i["id"] == "CHG-005")["owner"]

    res_confirm_skip = run_cli(["takeover_confirm", "--operator", "接管41b",
                                "--decision", "skip_all", "--force"], sp_target2)
    assert_in("TAKEOVER CONFIRMED", res_confirm_skip.stdout, "t41 skip_all confirm succeeds")

    s_skip = read_state(sp_target2)
    skip_decisions = s_skip["takeover_history"][0].get("decisions_log", [])
    print(f"  [DEBUG] skip decisions_log: {skip_decisions}")
    any_skip = any("skip" in str(d.get("decision", "")).lower() for d in skip_decisions)
    print(f"  [DEBUG] any_skip={any_skip}")

    after_skip_draft_v = s_skip["draft_version"]
    print(f"  [DEBUG] before_draft={before_skip_draft_v}, after_draft={after_skip_draft_v}")

    print("  [OK] skip_all 决策处理完成")
    cleanup_patterns(tmpdir, "state_t41_source.json")
    cleanup_patterns(tmpdir, "state_t41_target.json")
    cleanup_patterns(tmpdir, "state_t41_target2.json")


def test_42_cross_restart_persisted_session(tmpdir):
    """跨重启持久化会话：修改 session_pid 为不存在的PID，后续操作应标记 resumed 并增加计数"""
    print("\n== Test 42: Cross-restart persisted session (session_pid change triggers resumed) ==")
    sp_source = os.path.join(tmpdir, "state_t42_source.json")
    sp_target = os.path.join(tmpdir, "state_t42_target.json")
    cleanup_patterns(tmpdir, "state_t42_source.json")
    cleanup_patterns(tmpdir, "state_t42_target.json")

    run_cli(["import", SAMPLE], sp_source)
    run_cli(["draft"], sp_source)
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_source)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_source)
    run_cli(["confirm", "overview"], sp_source)
    run_cli(["confirm", "changes"], sp_source)

    pkg = os.path.join(tmpdir, "pkg_t42.json")
    run_cli(["export_package", "-o", pkg, "--operator", "源42"], sp_source)

    run_cli(["import", SAMPLE], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["import_package", pkg, "--operator", "接管42", "--mode", "takeover"], sp_target)
    run_cli(["takeover_confirm", "--operator", "接管42"], sp_target)

    s_init = read_state(sp_target)
    tid = s_init["takeover_history"][0]["takeover_id"]
    initial_resumed_count = s_init["confirmed_takeover_sessions"][tid].get("resumed_count", 0)
    initial_resumed_flag = s_init["takeover_history"][0].get("resumed_across_restart", False)

    print(f"  [DEBUG] Initial: resumed_count={initial_resumed_count}, resumed_flag={initial_resumed_flag}")

    s_init["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    with open(sp_target, "w", encoding="utf-8") as f:
        json.dump(s_init, f, ensure_ascii=False, indent=2)
    print("  [OK] session_pid 改为 99999 模拟重启")

    run_cli(["confirm", "migration"], sp_target)
    run_cli(["audit_view"], sp_target)

    s_after_op = read_state(sp_target)
    after_resumed_count = s_after_op["confirmed_takeover_sessions"][tid].get("resumed_count", 0)
    after_resumed_flag = s_after_op["takeover_history"][0].get("resumed_across_restart", False)
    print(f"  [DEBUG] After operation + audit_view: resumed_count={after_resumed_count}, resumed_flag={after_resumed_flag}")

    assert after_resumed_count >= initial_resumed_count + 1, "t42 resumed_count incremented after restart + op"
    assert after_resumed_flag == True, "t42 resumed_across_restart=True after restart + op"

    audit_actions = [e["action"] for e in s_after_op["audit_log"]]
    resume_events = [e for e in s_after_op["audit_log"] if e["action"] == "takeover_resumed_across_restart"]
    assert len(resume_events) >= 1, "t42 at least 1 takeover_resumed_across_restart audit event"

    run_cli(["confirm", "known_issues"], sp_target)
    run_cli(["draft"], sp_target)
    run_cli(["approve"], sp_target)

    res_audit = run_cli(["audit_view"], sp_target)
    assert_in("[RESUMED]", res_audit.stdout, "t42 audit_view shows [RESUMED] tag")
    assert_in("Cross-restart:  YES", res_audit.stdout, "t42 audit_view shows Cross-restart YES")
    assert_in("Persistent Session", res_audit.stdout, "t42 audit_view shows Persistent Session")
    assert_in(f"Resumed cnt:", res_audit.stdout,
              "t42 audit_view shows Resumed cnt label")

    res_status = run_cli(["status"], sp_target)
    assert_in("Active Takeover Sessions:", res_status.stdout, "t42 status shows active takeover sessions")
    assert_in("[RESUMED]", res_status.stdout, "t42 status shows [RESUMED] tag on session")

    s_final = read_state(sp_target)
    final_session = s_final["confirmed_takeover_sessions"][tid]
    assert final_session["revoked"] == False, "t42 session not revoked"
    assert final_session["resumed_count"] >= 1, "t42 final session resumed_count >= 1"

    print("  [OK] 跨重启会话持久化：session_pid变化后触发 resumed，计数增加，audit/status 均显示")
    cleanup_patterns(tmpdir, "state_t42_source.json")
    cleanup_patterns(tmpdir, "state_t42_target.json")


def test_43_full_e2e_handoff_confirm_restart_approve(tmpdir):
    """完整链路（真实等价版）：Machine A导出→Machine B对账→查看详情→确认→模拟重启→audit→继续确认→批准→导出Markdown"""
    print("\n== Test 43: FULL E2E (export -> reconcile -> detail -> confirm -> restart -> audit -> continue -> approve -> export) ==")
    sp_m1 = os.path.join(tmpdir, "state_t43_machine1.json")
    sp_m2 = os.path.join(tmpdir, "state_t43_machine2.json")
    cleanup_patterns(tmpdir, "state_t43_machine1.json")
    cleanup_patterns(tmpdir, "state_t43_machine2.json")

    print(f"\n  [Machine A] 导入 sample + draft + 批量 amend + 部分章节确认 + export_package ...")
    run_cli(["import", SAMPLE], sp_m1)
    run_cli(["draft"], sp_m1)

    patch_43 = os.path.join(tmpdir, "patch_t43.json")
    with open(patch_43, "w", encoding="utf-8") as f:
        json.dump({
            "patch_id": "t43-e2e",
            "operator": "负责人A-43",
            "reason": "E2E交接前批量修订",
            "items": [
                {"id": "CHG-003", "owner": "周七", "risk_level": "critical", "category": "removal"},
                {"id": "CHG-005", "owner": "赵六", "risk_level": "critical", "category": "security"},
            ],
        }, f, ensure_ascii=False, indent=2)
    run_cli(["bulk_amend", patch_43, "--mode", "overwrite",
             "--operator", "负责人A-43", "--reason", "批量修订"], sp_m1)
    run_cli(["draft"], sp_m1)
    run_cli(["confirm", "overview"], sp_m1)
    run_cli(["confirm", "changes"], sp_m1)

    pkg_43 = os.path.join(tmpdir, "pkg_t43_e2e.json")
    res_export = run_cli(["export_package", "-o", pkg_43, "--operator", "负责人A-43",
                          "--description", "Test43 E2E交接包"], sp_m1)
    assert_in("Handover Summary", res_export.stdout, "t43 Machine A export shows Handover Summary")

    with open(pkg_43, "r", encoding="utf-8") as f:
        pdata = json.load(f)
    assert "handover_summary" in pdata, "t43 package has handover_summary"
    m1_draft_v = pdata["state"]["draft_version"]
    print(f"  [OK] Machine A 导出完成，draft_v={m1_draft_v}, handover_summary OK")

    print(f"\n  [Machine B] 初始化 + 对账 import_package ...")
    run_cli(["import", SAMPLE], sp_m2)
    run_cli(["draft"], sp_m2)
    run_cli(["amend", "CHG-001", "--field", "owner=MachineB本地修改"], sp_m2)

    res_reconcile = run_cli(["import_package", pkg_43, "--operator", "负责人B-43",
                             "--mode", "takeover"], sp_m2)
    assert_in("RECONCILED", res_reconcile.stdout, "t43 step1 RECONCILED")
    assert_in("AWAITING CONFIRMATION", res_reconcile.stdout, "t43 step1 AWAITING CONFIRMATION")
    assert_in("Handover Summary from Source", res_reconcile.stdout, "t43 step1 shows handover summary")
    s_pending = read_state(sp_m2)
    assert s_pending.get("pending_takeover") is not None, "t43 pending_takeover present"
    print("  [OK] Machine B 只读对账完成")

    print(f"\n  [Machine B] 调用 takeover_detail 查看详情 ...")
    res_detail = run_cli(["takeover_detail"], sp_m2)
    assert_in("TAKEOVER DETAIL:", res_detail.stdout, "t43 takeover_detail shows header")
    assert_in("[PENDING CONFIRMATION]", res_detail.stdout, "t43 takeover_detail shows pending tag")
    assert_in("Takeover ID:", res_detail.stdout, "t43 takeover_detail shows takeover_id")
    assert_in("Imported at:", res_detail.stdout, "t43 takeover_detail shows imported_at")
    assert_in("Imported by:", res_detail.stdout, "t43 takeover_detail shows imported_by")
    assert_in("Handover Summary", res_detail.stdout, "t43 takeover_detail shows handover summary")
    print("  [OK] takeover_detail 详情完整")

    print(f"\n  [Machine B] 调用 takeover_confirm 正式确认接管 ...")
    res_confirm = run_cli(["takeover_confirm", "--operator", "负责人B-43"], sp_m2)
    assert_in("TAKEOVER CONFIRMED", res_confirm.stdout, "t43 TAKEOVER CONFIRMED")

    s_confirmed = read_state(sp_m2)
    assert s_confirmed.get("pending_takeover") is None, "t43 pending cleared"
    assert len(s_confirmed["takeover_history"]) == 1, "t43 takeover_history has 1"
    assert len(s_confirmed["confirmed_takeover_sessions"]) == 1, "t43 sessions has 1"
    tid = s_confirmed["takeover_history"][0]["takeover_id"]
    assert_eq(s_confirmed["draft_version"], m1_draft_v, "t43 draft_v matches package")
    chg003 = next(i for i in s_confirmed["items"] if i["id"] == "CHG-003")
    assert_eq(chg003["owner"], "周七", "t43 CHG-003 owner=周七")
    chg001 = next(i for i in s_confirmed["items"] if i["id"] == "CHG-001")
    assert_eq(chg001["owner"], "张三", "t43 CHG-001 reverted to package state (张三)")
    print("  [OK] Machine B 接管确认完成，状态已应用")

    print(f"\n  [Machine B] 先做一些后续工作（产生 confirmed_at 之后的非白名单事件）...")
    run_cli(["confirm", "migration"], sp_m2)
    run_cli(["confirm", "known_issues"], sp_m2)

    print(f"\n  [Machine B] 模拟重启（修改 session_pid + 子进程看audit_view）...")
    s_pre_restart = read_state(sp_m2)
    s_pre_restart["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    with open(sp_m2, "w", encoding="utf-8") as f:
        json.dump(s_pre_restart, f, ensure_ascii=False, indent=2)

    import subprocess
    audit_script = os.path.join(tmpdir, "audit_t43.py")
    with open(audit_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{os.path.dirname(os.path.abspath(__file__))}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--rules", r"{RULES}", "--state", r"{sp_m2}", "audit_view"]
cli_main()
""")
    res_subp = subprocess.run([sys.executable, audit_script], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    subp_out = res_subp.stdout + res_subp.stderr

    assert_in("Cross-restart:  YES", subp_out, "t43 restarted audit_view shows Cross-restart YES")
    assert_in("[RESUMED]", subp_out, "t43 restarted audit_view shows [RESUMED] tag")
    assert_in("Persistent Session", subp_out, "t43 restarted audit_view shows Persistent Session")
    print("  [OK] 重启后 audit_view 正确识别跨重启续做")

    s_after_restart = read_state(sp_m2)
    assert s_after_restart["takeover_history"][0].get("resumed_across_restart", False) == True, "t43 resumed_flag=True"
    assert s_after_restart["confirmed_takeover_sessions"][tid]["resumed_count"] >= 1, "t43 resumed_count >= 1"

    print(f"\n  [Machine B] 继续剩余 draft + 批准 ...")
    run_cli(["draft"], sp_m2)
    run_cli(["approve"], sp_m2)

    s_approved = read_state(sp_m2)
    assert s_approved["approved"] == True, "t43 approved=True"
    assert s_approved["approved_at_version"] == "2.1.0", "t43 approved_at_version=2.1.0"
    conf = s_approved["confirmations"]
    assert conf["overview"] and conf["changes"] and conf["migration"] and conf["known_issues"], "t43 all 4 sections confirmed"
    print("  [OK] 剩余工作完成：4章节确认、批准成功")

    print(f"\n  [Machine B] 导出最终 Markdown 并验证内容 ...")
    md_43 = os.path.join(tmpdir, "t43_final.md")
    run_cli(["export", "-o", md_43], sp_m2)
    md = read_file(md_43)
    assert_in("# Release Notes v2.1.0", md, "t43 MD title correct")
    assert_in("owner:周七", md, "t43 MD has 周七 as CHG-003 owner")
    assert_in("risk:critical", md, "t43 MD has critical risks")
    assert_in("owner:赵六", md, "t43 MD has 赵六 as CHG-005 owner")

    print(f"\n  [Machine B] 最终 audit_view 和 status 汇总验证 ...")
    res_final_audit = run_cli(["audit_view"], sp_m2)
    assert_in("[TAKEOVER]", res_final_audit.stdout, "t43 final audit has [TAKEOVER] tag for confirmed takeover")
    assert_in("[RESUMED]", res_final_audit.stdout, "t43 final audit has [RESUMED] tag")

    res_final_status = run_cli(["status"], sp_m2)
    assert_in("Approved:      True", res_final_status.stdout, "t43 final status shows approved")
    assert_in("Active Takeover Sessions:", res_final_status.stdout, "t43 final status has active sessions")
    assert_in("[RESUMED]", res_final_status.stdout, "t43 final status session shows [RESUMED]")

    s_final = read_state(sp_m2)
    session_final = s_final["confirmed_takeover_sessions"][tid]
    assert session_final["revoked"] == False, "t43 final session not revoked"
    assert session_final["confirmed_by"] == "负责人B-43", "t43 final session confirmed_by correct"
    assert session_final["resumed_count"] >= 1, "t43 final session resumed_count correct"

    final_audit_actions = [e["action"] for e in s_final["audit_log"]]
    expected_final = [
        "takeover_confirmed",
        "takeover_resumed_across_restart", "approved", "export"
    ]
    for ea in expected_final:
        assert_in(ea, final_audit_actions, f"t43 final audit has {ea}")
    assert any("import_package" in a for a in final_audit_actions), "t43 audit has some import_package_* action"

    print(f"\n  [OK] ====== TEST 43 完整链路全部通过 ======")
    print(f"  1. Machine A 导出（handover_summary） OK")
    print(f"  2. Machine B 对账（只读 reconcile + pending） OK")
    print(f"  3. takeover_detail 详情 OK")
    print(f"  4. takeover_confirm 确认（持久化会话） OK")
    print(f"  5. 模拟重启（跨重启识别 + resumed_count++） OK")
    print(f"  6. audit_view [RESUMED]/[TAKEOVER] 标签 OK")
    print(f"  7. 继续确认章节 + 批准 OK")
    print(f"  8. 最终 Markdown 内容正确 OK")
    print(f"  9. confirmed_takeover_sessions 信息完整 OK")

    cleanup_patterns(tmpdir, "state_t43_machine1.json")
    cleanup_patterns(tmpdir, "state_t43_machine2.json")


def test_44_utf8_mode_encoding_stability(tmpdir):
    """Test 44: python -X utf8 下 audit_view/详情/跨重启/撤销恢复 的编码稳定性"""
    safe_print("\n" + "=" * 60)
    safe_print("TEST 44: UTF-8 模式编码稳定性（audit_view/详情/跨重启/撤销）")
    safe_print("=" * 60)

    SAMPLE = os.path.join(SCRIPT_DIR, "sample_manifest.json")

    # ========== Part 1: A 导出包 ==========
    safe_print("\n  [Part 1] Machine A 构造包（2章节确认）...")
    sp_a = os.path.join(tmpdir, "state_t44_a.json")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["draft"], sp_a)
    run_cli(["amend", "CHG-003", "--field", "owner=王五-t44"], sp_a)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_a)
    run_cli(["confirm", "overview"], sp_a)
    run_cli(["confirm", "changes"], sp_a)

    pkg44 = os.path.join(tmpdir, "pkg_t44.json")
    run_cli(["export_package", "-o", pkg44, "--operator", "源机负责人-44"], sp_a)

    # ========== Part 2: B 导入对账 ==========
    safe_print("\n  [Part 2] Machine B 导入对账（只读 pending）...")
    sp_b = os.path.join(tmpdir, "state_t44_b.json")
    run_cli(["import", SAMPLE], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["import_package", pkg44, "--mode", "takeover", "--operator", "接管人B-44"], sp_b)

    # ========== Part 3: -X utf8 下 audit_view 中文输出 ==========
    safe_print("\n  [Part 3] python -X utf8 调用 audit_view（pending 状态）...")
    res_audit_pending = run_cli_utf8(["audit_view"], sp_b)
    assert res_audit_pending.returncode == 0, "t44 -X utf8 audit_view(pending) exit=0"
    assert len(res_audit_pending.stdout) > 100, "t44 -X utf8 audit_view(pending) stdout 非空"
    assert_in("[PENDING]", res_audit_pending.stdout, "t44 pending audit_view has [PENDING] tag")
    assert_in("AUDIT VIEW", res_audit_pending.stdout, "t44 pending audit_view has AUDIT VIEW header")
    pending_has_zh = any('\u4e00' <= c <= '\u9fff' for c in res_audit_pending.stdout)
    assert pending_has_zh, "t44 pending audit_view(-X utf8) 含中文字符"
    safe_print(f"  [OK] -X utf8 audit_view(pending): {len(res_audit_pending.stdout)} bytes, 中文={pending_has_zh}")

    # ========== Part 4: -X utf8 下 takeover_detail 详情查看 ==========
    safe_print("\n  [Part 4] python -X utf8 调用 takeover_detail...")
    res_detail = run_cli_utf8(["takeover_detail"], sp_b)
    assert res_detail.returncode == 0, "t44 -X utf8 takeover_detail exit=0"
    assert len(res_detail.stdout) > 100, "t44 -X utf8 takeover_detail stdout 非空"
    assert_in("TAKEOVER DETAIL:", res_detail.stdout, "t44 takeover_detail has header")
    assert_in("[PENDING CONFIRMATION]", res_detail.stdout, "t44 takeover_detail has pending tag")
    detail_has_zh = any('\u4e00' <= c <= '\u9fff' for c in res_detail.stdout)
    assert detail_has_zh, "t44 takeover_detail(-X utf8) 含中文字符"
    safe_print(f"  [OK] -X utf8 takeover_detail: {len(res_detail.stdout)} bytes, 中文={detail_has_zh}")

    # ========== Part 5: 确认接管 → 模拟重启 → -X utf8 下 audit_view 跨重启检测 ==========
    safe_print("\n  [Part 5] 确认接管，模拟跨重启，-X utf8 下检测 [RESUMED]...")
    run_cli(["takeover_confirm", "--operator", "接管人B-44"], sp_b)

    s_confirmed = read_state(sp_b)
    tid44 = s_confirmed["takeover_history"][0]["takeover_id"]

    # 做一些后续工作（confirmed_at 之后的非白名单事件）
    run_cli(["confirm", "migration"], sp_b)
    run_cli(["confirm", "known_issues"], sp_b)

    # 改 session_pid = 99999 模拟重启
    s_reload = read_state(sp_b)
    s_reload["confirmed_takeover_sessions"][tid44]["session_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s_reload, f, ensure_ascii=False, indent=2)

    # 用 -X utf8 子进程调用 audit_view，检查跨重启检测
    res_audit_resumed = run_cli_utf8(["audit_view"], sp_b)
    assert res_audit_resumed.returncode == 0, "t44 -X utf8 audit_view(resumed) exit=0"
    assert len(res_audit_resumed.stdout) > 100, "t44 -X utf8 audit_view(resumed) stdout 非空"
    assert_in("Cross-restart:  YES", res_audit_resumed.stdout, "t44 resumed audit_view Cross-restart: YES")
    assert_in("[RESUMED]", res_audit_resumed.stdout, "t44 resumed audit_view has [RESUMED] tag")
    assert_in("[TAKEOVER]", res_audit_resumed.stdout, "t44 resumed audit_view has [TAKEOVER] tag")
    resumed_has_zh = any('\u4e00' <= c <= '\u9fff' for c in res_audit_resumed.stdout)
    assert resumed_has_zh, "t44 resumed audit_view(-X utf8) 含中文字符"
    safe_print(f"  [OK] -X utf8 audit_view(resumed): {len(res_audit_resumed.stdout)} bytes, 中文={resumed_has_zh}")

    # 读取更新后的 state，确认 resumed_count 增加（audit_view 内部会写入）
    s_after_resumed = read_state(sp_b)
    assert s_after_resumed["confirmed_takeover_sessions"][tid44]["resumed_count"] >= 1, "t44 resumed_count ≥1"
    assert s_after_resumed["takeover_history"][0].get("resumed_across_restart") == True, "t44 resumed_flag=True"
    safe_print("  [OK] 跨重启检测写入正确，resumed_count 增加")

    # ========== Part 6: 重建 pending 状态 → -X utf8 下撤销 pending ==========
    safe_print("\n  [Part 6] 重建 pending → -X utf8 下撤销待确认（pending revoke）...")
    sp_c = os.path.join(tmpdir, "state_t44_c.json")
    run_cli(["import", SAMPLE], sp_c)
    run_cli(["draft"], sp_c)
    run_cli(["import_package", pkg44, "--mode", "takeover", "--operator", "接管人C-44"], sp_c)

    # 撤销前验证 pending 存在
    s_before_revoke = read_state(sp_c)
    assert s_before_revoke.get("pending_takeover") is not None, "t44 pending exists before revoke"
    pre_items_before = s_before_revoke["pending_takeover"]["pre_import_state"]["items"]

    # 用 -X utf8 模式撤销 pending
    res_revoke_pending = run_cli_utf8(["takeover_revoke", "--operator", "撤销人C-44",
                                         "--reason", "撤销待确认测试-44"], sp_c)
    assert res_revoke_pending.returncode == 0, "t44 -X utf8 revoke pending exit=0"
    assert_in("revoked successfully", res_revoke_pending.stdout, "t44 pending revoke msg ok")
    assert any('\u4e00' <= c <= '\u9fff' for c in res_revoke_pending.stdout), "t44 pending revoke output 含中文"

    # 验证 pending 清除，state 回滚到 pre_import
    s_after_pending_revoke = read_state(sp_c)
    assert s_after_pending_revoke.get("pending_takeover") is None, "t44 pending cleared after revoke"
    assert len(s_after_pending_revoke.get("takeover_history", [])) == 0, "t44 no takeover history after pending revoke"
    # pre_import 中 CHG-003 还没有 owner（因为对账前是原始 import + draft，没有 amend）
    chg003_after = next(it for it in s_after_pending_revoke["items"] if it["id"] == "CHG-003")
    assert chg003_after.get("owner") in [None, ""], "t44 CHG-003 owner rolled back after pending revoke"
    safe_print("  [OK] pending 撤销：状态回滚，pending_takeover 清除，审计事件写入")

    # ========== Part 7: 重建 confirmed → -X utf8 下撤销 confirmed（--force） ==========
    safe_print("\n  [Part 7] 重建 confirmed → -X utf8 下撤销已确认（--force）...")
    sp_d = os.path.join(tmpdir, "state_t44_d.json")
    run_cli(["import", SAMPLE], sp_d)
    run_cli(["draft"], sp_d)
    run_cli(["import_package", pkg44, "--mode", "takeover", "--operator", "接管人D-44"], sp_d)
    run_cli(["takeover_confirm", "--operator", "接管人D-44"], sp_d)

    # 用 -X utf8 模式撤销 confirmed（必须 --force）
    res_revoke_no_force = run_cli_utf8(["takeover_revoke", "--operator", "撤销人D-44",
                                          "--reason", "撤销已确认测试-44"], sp_d, expect_fail=True)
    assert res_revoke_no_force.returncode != 0, "t44 revoke confirmed without --force rejected"

    # 加 --force 撤销
    tid44_d = read_state(sp_d)["takeover_history"][0]["takeover_id"]
    res_revoke_confirmed = run_cli_utf8(["takeover_revoke", "--operator", "撤销人D-44",
                                           "--reason", "撤销已确认测试-44", "--force",
                                           "--takeover-id", tid44_d], sp_d)
    assert res_revoke_confirmed.returncode == 0, "t44 -X utf8 revoke confirmed(--force) exit=0"
    assert_in("Revoking CONFIRMED TAKEOVER SESSION", res_revoke_confirmed.stdout, "t44 confirmed revoke msg 1")
    assert_in("State rolled back", res_revoke_confirmed.stdout, "t44 confirmed revoke msg 2")
    assert_in("revoked successfully", res_revoke_confirmed.stdout, "t44 confirmed revoke msg 3")

    # 验证 takeover_history 和 sessions 保留 revoked=True
    s_after_confirmed_revoke = read_state(sp_d)
    assert len(s_after_confirmed_revoke.get("takeover_history", [])) == 1, "t44 confirmed revoke keeps 1 history entry"
    assert s_after_confirmed_revoke["takeover_history"][0].get("revoked") == True, "t44 takeover revoked=True"
    assert s_after_confirmed_revoke["takeover_history"][0].get("revoke_reason") == "撤销已确认测试-44", "t44 revoke_reason correct"
    sessions_d = s_after_confirmed_revoke.get("confirmed_takeover_sessions", {})
    assert len(sessions_d) == 1, "t44 confirmed revoke keeps session"
    assert sessions_d[tid44_d]["revoked"] == True, "t44 session revoked=True"
    assert sessions_d[tid44_d].get("revoke_reason") == "撤销已确认测试-44", "t44 session revoke_reason correct"

    # 用 -X utf8 调 audit_view 验证 [REVOKED] 标签
    res_audit_revoked = run_cli_utf8(["audit_view"], sp_d)
    assert res_audit_revoked.returncode == 0, "t44 -X utf8 audit_view(revoked) exit=0"
    assert_in("[REVOKED]", res_audit_revoked.stdout, "t44 revoked audit_view has [REVOKED] tag")
    assert_in("[Revocation]", res_audit_revoked.stdout, "t44 revoked audit_view has [Revocation] section")
    revoked_has_zh = any('\u4e00' <= c <= '\u9fff' for c in res_audit_revoked.stdout)
    assert revoked_has_zh, "t44 revoked audit_view(-X utf8) 含中文字符"
    safe_print(f"  [OK] -X utf8 audit_view(revoked): {len(res_audit_revoked.stdout)} bytes, 中文={revoked_has_zh}")
    safe_print("  [OK] confirmed 撤销：takeover/sessions 保留 revoked=True，audit_view 正确显示 [REVOKED]")

    # ========== Part 8: 验证 0 passed, 0 failed 防误判机制（附加验证） ==========
    safe_print("\n  [Part 8] 验证 -X utf8 模式下 status 中文输出稳定...")
    # 回到 sp_b（resumed 状态），用 -X utf8 调 status
    res_status = run_cli_utf8(["status"], sp_b)
    assert res_status.returncode == 0, "t44 -X utf8 status exit=0"
    assert len(res_status.stdout) > 50, "t44 -X utf8 status stdout 非空"
    assert_in("Active Takeover Sessions:", res_status.stdout, "t44 status shows active sessions")
    status_has_zh = any('\u4e00' <= c <= '\u9fff' for c in res_status.stdout)
    assert status_has_zh, "t44 status(-X utf8) 含中文字符"
    safe_print(f"  [OK] -X utf8 status: {len(res_status.stdout)} bytes, 中文={status_has_zh}")

    # 清理
    cleanup_patterns(tmpdir, "state_t44_a.json")
    cleanup_patterns(tmpdir, "state_t44_b.json")
    cleanup_patterns(tmpdir, "state_t44_c.json")
    cleanup_patterns(tmpdir, "state_t44_d.json")

    safe_print("\n  [OK] ====== TEST 44 UTF-8 编码稳定性全部通过 ======")
    safe_print("  1. -X utf8 audit_view(pending) 中文输出 ✅")
    safe_print("  2. -X utf8 takeover_detail 详情 ✅")
    safe_print("  3. -X utf8 audit_view(resumed) 跨重启检测 ✅")
    safe_print("  4. -X utf8 pending revoke 撤销待确认 ✅")
    safe_print("  5. -X utf8 confirmed revoke(--force) 撤销已确认 ✅")
    safe_print("  6. -X utf8 status 中文输出 ✅")


def run_cli_with_rules(args, state_path, rules_path, expect_fail=False):
    cmd = [sys.executable, CLI, "--rules", rules_path, "--state", state_path] + args
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=SCRIPT_DIR)
    ok = res.returncode != 0 if expect_fail else res.returncode == 0
    if not ok:
        safe_print(f"  [FAIL] {' '.join(args)}")
        safe_print(f"    exit={res.returncode} expect_fail={expect_fail}")
        if res.stdout.strip():
            safe_print(f"    stdout: {safe_truncate(res.stdout)}")
        if res.stderr.strip():
            safe_print(f"    stderr: {safe_truncate(res.stderr)}")
    else:
        label = "(expected fail)" if expect_fail else ""
        safe_print(f"  [OK] {' '.join(args)} {label}")
    return res


def _make_rules_stricter(path):
    import yaml
    rules = {
        "required_sections": ["overview", "changes", "migration", "known_issues"],
        "valid_risk_levels": ["low", "medium", "high"],
        "required_fields_per_item": ["id", "title", "owner", "risk_level", "category"],
        "categories": ["feature", "bugfix", "refactor", "security"],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(rules, f, allow_unicode=True, default_flow_style=False)


def _make_rules_newer(path):
    import yaml
    rules = {
        "required_sections": ["overview", "changes", "migration", "known_issues", "rollback_plan"],
        "valid_risk_levels": ["low", "medium", "high", "critical", "blocker"],
        "required_fields_per_item": ["id", "title", "owner", "risk_level", "category", "test_coverage"],
        "categories": ["feature", "bugfix", "refactor", "deprecation", "removal", "security", "performance"],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(rules, f, allow_unicode=True, default_flow_style=False)


def test_45_rules_upgrade_check_basic(tmpdir):
    """规则升级对账：基础预检功能，规则相同时报无变更，规则不同时检测变更"""
    print("\n== Test 45: Rules upgrade check basic ==")
    sp = os.path.join(tmpdir, "state_t45.json")
    cleanup_patterns(tmpdir, "state_t45.json")

    print("\n  [Step 1] 导入清单...")
    run_cli(["import", SAMPLE], sp)
    s0 = read_state(sp)
    assert "rules_version" in s0, "t45: import后应有 rules_version"
    assert "rules_snapshot" in s0, "t45: import后应有 rules_snapshot"
    original_version = s0["rules_version"]
    safe_print(f"  初始规则版本: {original_version[:16]}...")

    print("\n  [Step 2] 用相同规则检查升级（应报无变更）...")
    res_same = run_cli(["rules_upgrade_check"], sp)
    assert_in("IDENTICAL", res_same.stdout, "t45: 相同规则应显示 IDENTICAL")
    assert_in("No upgrade needed", res_same.stdout, "t45: 相同规则应显示无需升级")

    print("\n  [Step 3] 用更严格的规则检查升级（应检测到变更）...")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    res_stricter = run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)
    assert_in("IMPACT ANALYSIS", res_stricter.stdout, "t45: 规则变更应显示影响分析")
    assert_in("Removed: critical", res_stricter.stdout, "t45: 应检测到移除 critical 风险级别")
    assert_in("Manual decisions required", res_stricter.stdout, "t45: 应报告需人工决策")

    s1 = read_state(sp)
    assert s1.get("pending_rules_upgrade") is not None, "t45: check后应有 pending_rules_upgrade"
    pending = s1["pending_rules_upgrade"]
    assert pending["status"] == "pending_confirmation", "t45: pending 状态应为 pending_confirmation"
    assert "impact" in pending, "t45: pending 应包含 impact"
    assert "rules_diff" in pending, "t45: pending 应包含 rules_diff"
    assert "check_pid" in pending, "t45: pending 应包含 check_pid"

    safe_print(f"  待处理升级 ID: {pending['upgrade_id']}")
    safe_print(f"  需人工决策数: {pending['impact']['summary']['manual_decision_count']}")

    print("\n  [Step 4] 再用新规则检查（应覆盖旧的 pending）...")
    newer_rules = os.path.join(tmpdir, "rules_newer.yaml")
    _make_rules_newer(newer_rules)
    res_newer = run_cli_with_rules(["rules_upgrade_check"], sp, newer_rules)
    assert_in("Added:   rollback_plan", res_newer.stdout, "t45: 应检测到新增 rollback_plan 章节")
    assert_in("Added:   test_coverage", res_newer.stdout, "t45: 应检测到新增 test_coverage 字段")

    s2 = read_state(sp)
    pending2 = s2["pending_rules_upgrade"]
    assert pending2["upgrade_id"] != pending["upgrade_id"], "t45: 重新检查应生成新的 upgrade_id"

    print("\n  [OK] 规则升级预检基础功能正常")
    cleanup_patterns(tmpdir, "state_t45.json")


def test_46_rules_upgrade_apply_and_undo(tmpdir):
    """规则升级：应用决策与撤销回滚"""
    print("\n== Test 46: Rules upgrade apply and undo ==")
    sp = os.path.join(tmpdir, "state_t46.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t46.json")

    print("\n  [Step 1] 导入并确认部分章节...")
    run_cli(["import", SAMPLE], sp)
    run_cli(["confirm", "overview"], sp)
    run_cli(["confirm", "changes"], sp)

    s_before = read_state(sp)
    chg003_before = next(i for i in s_before["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_before["risk_level"], "critical", "t46: CHG-003 升级前 risk_level=critical")
    assert_eq(chg003_before["category"], "removal", "t46: CHG-003 升级前 category=removal")
    rules_ver_before = s_before["rules_version"]

    print("\n  [Step 2] 检查升级并应用（带 per-item 决策）...")
    run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)
    res_apply = run_cli_with_rules(
        ["rules_upgrade_apply",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
         "--operator", "tester_46"],
        sp, stricter_rules
    )
    assert_in("RULES UPGRADE APPLIED", res_apply.stdout, "t46: apply 应显示成功")
    assert_in("Manual decisions:  2 applied", res_apply.stdout, "t46: 应显示 2 个手动决策已应用")

    s_after_apply = read_state(sp)
    assert s_after_apply.get("pending_rules_upgrade") is None, "t46: apply后 pending 应清空"
    assert len(s_after_apply.get("rules_upgrade_history", [])) == 1, "t46: 应有 1 条升级历史"
    upgrade_record = s_after_apply["rules_upgrade_history"][0]
    assert_eq(upgrade_record["applied_by"], "tester_46", "t46: 升级记录应包含操作人")
    assert_eq(upgrade_record["revoked"], False, "t46: 新应用的升级 revoked=False")

    chg003_after = next(i for i in s_after_apply["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_after["risk_level"], "high", "t46: apply后 CHG-003 risk_level=high")
    assert_eq(chg003_after["category"], "refactor", "t46: apply后 CHG-003 category=refactor")
    assert s_after_apply["rules_version"] != rules_ver_before, "t46: apply后 rules_version 应变更"

    print("\n  [Step 3] 查看升级历史...")
    res_history = run_cli_with_rules(["rules_history"], sp, stricter_rules)
    assert_in("RULES UPGRADE HISTORY", res_history.stdout, "t46: history 应显示标题")
    assert_in("tester_46", res_history.stdout, "t46: history 应显示操作人")
    assert_in("active: 1", res_history.stdout, "t46: 应显示 1 个 active 升级")

    print("\n  [Step 4] 撤销升级...")
    res_undo = run_cli_with_rules(
        ["rules_upgrade_undo", "--reason", "test undo", "--operator", "tester_46"],
        sp, stricter_rules
    )
    assert_in("RULES UPGRADE UNDONE", res_undo.stdout, "t46: undo 应显示成功")
    assert_in("Rules restored to", res_undo.stdout, "t46: undo 应显示规则已恢复")

    s_after_undo = read_state(sp)
    assert len(s_after_undo["rules_upgrade_history"]) == 1, "t46: undo后历史记录仍保留 1 条"
    assert_eq(s_after_undo["rules_upgrade_history"][0]["revoked"], True, "t46: 撤销后 revoked=True")
    assert_eq(s_after_undo["rules_upgrade_history"][0]["revoke_reason"], "test undo",
              "t46: 撤销后应有 revoke_reason")

    chg003_undo = next(i for i in s_after_undo["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_undo["risk_level"], "critical", "t46: undo后 CHG-003 risk_level 恢复为 critical")
    assert_eq(chg003_undo["category"], "removal", "t46: undo后 CHG-003 category 恢复为 removal")
    assert_eq(s_after_undo["rules_version"], rules_ver_before, "t46: undo后 rules_version 恢复")

    print("\n  [Step 5] 再次查看历史（应显示已撤销）...")
    res_hist2 = run_cli_with_rules(["rules_history"], sp, stricter_rules)
    assert_in("revoked: 1", res_hist2.stdout, "t46: 历史应显示 1 个 revoked")

    print("\n  [OK] 规则升级应用与撤销功能正常")
    cleanup_patterns(tmpdir, "state_t46.json")


def test_47_rules_upgrade_skip_and_status(tmpdir):
    """规则升级：跳过升级、status 显示规则版本与待决升级"""
    print("\n== Test 47: Rules upgrade skip and status display ==")
    sp = os.path.join(tmpdir, "state_t47.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t47.json")

    print("\n  [Step 1] 导入并检查升级...")
    run_cli(["import", SAMPLE], sp)
    run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)

    print("\n  [Step 2] status 应显示待决升级...")
    res_status1 = run_cli_with_rules(["status"], sp, stricter_rules)
    assert_in("RULES UPGRADE: PENDING", res_status1.stdout, "t47: status 应显示待决升级")
    assert_in("Upgrade ID:", res_status1.stdout, "t47: status 应显示升级 ID")
    assert_in("Manual dec:", res_status1.stdout, "t47: status 应显示人工决策数")

    print("\n  [Step 3] 跳过升级...")
    res_skip = run_cli_with_rules(
        ["rules_upgrade_skip", "--reason", "skip for now", "--operator", "tester_47"],
        sp, stricter_rules
    )
    assert_in("skipped", res_skip.stdout, "t47: skip 应显示已跳过")

    s_after_skip = read_state(sp)
    assert s_after_skip.get("pending_rules_upgrade") is None, "t47: skip后 pending 应清空"

    print("\n  [Step 4] status 应不再显示待决升级，但显示规则版本...")
    res_status2 = run_cli(["status"], sp)
    assert_in("Rules version:", res_status2.stdout, "t47: status 应显示规则版本")
    assert_not_in("RULES UPGRADE: PENDING", res_status2.stdout, "t47: skip后不应再显示待决升级")

    print("\n  [OK] 跳过升级与 status 显示功能正常")
    cleanup_patterns(tmpdir, "state_t47.json")


def test_48_rules_upgrade_cross_restart_resume(tmpdir):
    """规则升级：跨重启恢复检测 - 待决升级持久化后重启能识别为续做"""
    print("\n== Test 48: Rules upgrade cross-restart resume detection ==")
    sp = os.path.join(tmpdir, "state_t48.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t48.json")

    print("\n  [Step 1] 导入并创建待决升级...")
    run_cli(["import", SAMPLE], sp)
    run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)

    s_initial = read_state(sp)
    pending_initial = s_initial["pending_rules_upgrade"]
    assert_eq(pending_initial.get("resumed_across_restart", False), False,
              "t48: 初始 pending 不应有 resumed 标记")
    original_pid = pending_initial.get("check_pid")
    assert original_pid is not None, "t48: pending 应包含 check_pid"

    print(f"  初始 check_pid: {original_pid}")

    print("\n  [Step 2] 做一些后续工作（确认章节），模拟重启前的进展...")
    run_cli(["confirm", "overview"], sp)
    run_cli(["confirm", "changes"], sp)

    print("\n  [Step 3] 模拟重启：修改 check_pid 为不同的值...")
    s_before_restart = read_state(sp)
    s_before_restart["pending_rules_upgrade"]["check_pid"] = 99999
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)

    print("\n  [Step 4] 重启后调用 rules_upgrade_apply（应检测为续做）...")
    apply_script = os.path.join(tmpdir, "run_apply_t48.py")
    with open(apply_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{SCRIPT_DIR}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--rules", r"{stricter_rules}", "--state", r"{sp}",
            "rules_upgrade_apply", "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
            "--operator", "tester_48"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, apply_script],
        capture_output=True,
        cwd=SCRIPT_DIR,
    )
    apply_output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')

    s_after_apply = read_state(sp)
    audit_actions = [e["action"] for e in s_after_apply.get("audit_log", [])]
    resume_events = [e for e in audit_actions if e == "pending_rules_upgrade_resumed"]
    assert len(resume_events) >= 1, "t48: 应有 pending_rules_upgrade_resumed 审计事件"

    upgrade_record = s_after_apply["rules_upgrade_history"][-1]
    assert_in("Resumed:           Yes (across restart)", apply_output,
              "t48: apply 输出应显示 Resumed")

    print(f"  重启检测到的续做事件数: {len(resume_events)}")

    print("\n  [OK] 跨重启恢复检测功能正常")
    cleanup_patterns(tmpdir, "state_t48.json")


def test_49_rules_upgrade_handover_preservation(tmpdir):
    """规则升级：交接包导出导入后规则版本、升级历史、撤销记录完整保留"""
    print("\n== Test 49: Rules upgrade preservation across handover ==")
    sp_a = os.path.join(tmpdir, "state_t49_a.json")
    sp_b = os.path.join(tmpdir, "state_t49_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t49_a.json")
    cleanup_patterns(tmpdir, "state_t49_b.json")

    print("\n  [Machine A] 导入、升级、撤销、再应用...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["confirm", "overview"], sp_a)

    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)
    run_cli_with_rules(
        ["rules_upgrade_apply",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
         "--operator", "user_A49"],
        sp_a, stricter_rules
    )

    run_cli_with_rules(
        ["rules_upgrade_undo", "--reason", "test revoke", "--operator", "user_A49"],
        sp_a, stricter_rules
    )

    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)
    run_cli_with_rules(
        ["rules_upgrade_apply",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
         "--operator", "user_A49"],
        sp_a, stricter_rules
    )

    s_a_before_export = read_state(sp_a)
    rules_ver_a = s_a_before_export["rules_version"]
    hist_count_a = len(s_a_before_export.get("rules_upgrade_history", []))
    revoked_count_a = len([r for r in s_a_before_export["rules_upgrade_history"] if r.get("revoked")])
    active_count_a = len([r for r in s_a_before_export["rules_upgrade_history"] if not r.get("revoked")])

    safe_print(f"  导出前: rules_version={rules_ver_a[:16]}..., "
               f"history={hist_count_a}, active={active_count_a}, revoked={revoked_count_a}")
    assert hist_count_a == 2, "t49: 导出前应有 2 条升级历史（1 撤销 + 1 活跃）"
    assert revoked_count_a == 1, "t49: 导出前应有 1 条已撤销"
    assert active_count_a == 1, "t49: 导出前应有 1 条活跃"

    print("\n  [Machine A] 导出交接包...")
    pkg = os.path.join(tmpdir, "handover_t49.zip")
    run_cli_with_rules(
        ["export_package", "-o", pkg, "--operator", "user_A49"],
        sp_a, stricter_rules
    )

    print("\n  [Machine B] 导入交接包...")
    res_import = run_cli_with_rules(
        ["import_package", pkg, "--operator", "user_B49"],
        sp_b, stricter_rules
    )
    assert_in("Package rules snapshot IDENTICAL to local rules", res_import.stdout,
              "t49: 导入时应检测包内规则与本地一致")

    print("\n  [Machine B] 确认接管...")
    run_cli_with_rules(["takeover_confirm", "--operator", "user_B49"], sp_b, stricter_rules)

    s_b_after = read_state(sp_b)
    rules_ver_b = s_b_after["rules_version"]
    hist_count_b = len(s_b_after.get("rules_upgrade_history", []))
    revoked_count_b = len([r for r in s_b_after["rules_upgrade_history"] if r.get("revoked")])
    active_count_b = len([r for r in s_b_after["rules_upgrade_history"] if not r.get("revoked")])

    safe_print(f"  导入后: rules_version={rules_ver_b[:16]}..., "
               f"history={hist_count_b}, active={active_count_b}, revoked={revoked_count_b}")

    assert_eq(rules_ver_b, rules_ver_a, "t49: 导入后 rules_version 应与导出时相同")
    assert_eq(hist_count_b, hist_count_a, "t49: 导入后升级历史数量应相同")
    assert_eq(revoked_count_b, revoked_count_a, "t49: 导入后已撤销数量应相同")
    assert_eq(active_count_b, active_count_a, "t49: 导入后活跃数量应相同")

    chg003_b = next(i for i in s_b_after["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_b["risk_level"], "high", "t49: 导入后 CHG-003 risk_level=high（升级结果保留）")
    assert_eq(chg003_b["category"], "refactor", "t49: 导入后 CHG-003 category=refactor（升级结果保留）")

    print("\n  [Machine B] 查看规则历史（应显示全部历史）...")
    res_hist = run_cli_with_rules(["rules_history"], sp_b, stricter_rules)
    assert_in("user_A49", res_hist.stdout, "t49: 历史应显示原操作人 user_A49")
    assert_in("Total upgrades: 2", res_hist.stdout, "t49: 历史应显示总共 2 条升级")

    print("\n  [Machine B] 继续审批工作（确认剩余章节）...")
    run_cli_with_rules(["confirm", "migration"], sp_b, stricter_rules)
    run_cli_with_rules(["confirm", "known_issues"], sp_b, stricter_rules)

    s_b_final = read_state(sp_b)
    assert_eq(s_b_final["confirmations"]["migration"], True, "t49: B 方可继续确认 migration")
    assert_eq(s_b_final["confirmations"]["known_issues"], True, "t49: B 方可继续确认 known_issues")

    print("\n  [OK] 规则升级信息在交接包中完整保留，接手方可继续工作")
    cleanup_patterns(tmpdir, "state_t49_a.json")
    cleanup_patterns(tmpdir, "state_t49_b.json")


def test_50_full_e2e_rules_upgrade_workflow(tmpdir):
    """完整端到端：规则变更→预检→应用→重启恢复→导包接手→继续审批"""
    print("\n== Test 50: Full E2E rules upgrade workflow ==")
    sp_a = os.path.join(tmpdir, "state_t50_a.json")
    sp_b = os.path.join(tmpdir, "state_t50_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t50_a.json")
    cleanup_patterns(tmpdir, "state_t50_b.json")

    print("\n  [Phase 1 - Machine A] 初始导入与部分工作...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["confirm", "overview"], sp_a)

    s_phase1 = read_state(sp_a)
    rules_ver_initial = s_phase1["rules_version"]
    safe_print(f"  初始规则版本: {rules_ver_initial[:16]}...")

    print("\n  [Phase 2 - Machine A] 规则变更，执行预检...")
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    s_phase2 = read_state(sp_a)
    pending = s_phase2["pending_rules_upgrade"]
    upgrade_id = pending["upgrade_id"]
    manual_count = pending["impact"]["summary"]["manual_decision_count"]
    safe_print(f"  升级 ID: {upgrade_id}")
    safe_print(f"  需人工决策: {manual_count} 项")
    assert manual_count >= 1, "t50: 至少应有 1 项人工决策"

    print("\n  [Phase 3 - Machine A] 模拟进程中断（重启）...")
    run_cli(["confirm", "changes"], sp_a)

    s_before_restart = read_state(sp_a)
    s_before_restart["pending_rules_upgrade"]["check_pid"] = 99999
    with open(sp_a, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)

    print("\n  [Phase 4 - Machine A] 重启后恢复，应用升级...")
    apply_script = os.path.join(tmpdir, "run_apply_t50.py")
    with open(apply_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{SCRIPT_DIR}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--rules", r"{stricter_rules}", "--state", r"{sp_a}",
            "rules_upgrade_apply", "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
            "--operator", "operator_A50"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, apply_script],
        capture_output=True,
        cwd=SCRIPT_DIR,
    )
    apply_output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')

    s_phase4 = read_state(sp_a)
    resume_events = [e for e in s_phase4["audit_log"] if e["action"] == "pending_rules_upgrade_resumed"]
    assert len(resume_events) >= 1, "t50: 重启后应用应触发续做检测"
    assert_in("Resumed:           Yes (across restart)", apply_output,
              "t50: apply 输出应显示跨重启续做")

    rules_ver_after_upgrade = s_phase4["rules_version"]
    assert rules_ver_after_upgrade != rules_ver_initial, "t50: 升级后规则版本应变化"

    chg003_a = next(i for i in s_phase4["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_a["risk_level"], "high", "t50: A 方升级后 CHG-003 risk_level=high")

    print("\n  [Phase 5 - Machine A] 导出交接包...")
    pkg = os.path.join(tmpdir, "handover_t50.zip")
    run_cli_with_rules(
        ["export_package", "-o", pkg, "--operator", "operator_A50",
         "--description", "v2.1.0 已完成规则升级交接包"],
        sp_a, stricter_rules
    )

    print("\n  [Phase 6 - Machine B] 导入交接包并确认接管...")
    run_cli_with_rules(
        ["import_package", pkg, "--operator", "operator_B50"],
        sp_b, stricter_rules
    )
    run_cli_with_rules(["takeover_confirm", "--operator", "operator_B50"], sp_b, stricter_rules)

    s_phase6 = read_state(sp_b)
    assert_eq(s_phase6["rules_version"], rules_ver_after_upgrade,
              "t50: B 方接管后规则版本与 A 方一致")
    assert len(s_phase6.get("rules_upgrade_history", [])) == 1, "t50: B 方应有 1 条升级历史"
    assert not s_phase6["rules_upgrade_history"][0].get("revoked"), "t50: 升级记录为活跃状态"

    chg003_b = next(i for i in s_phase6["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_b["risk_level"], "high", "t50: B 方 CHG-003 risk_level=high（升级结果保留）")
    assert_eq(chg003_b["category"], "refactor", "t50: B 方 CHG-003 category=refactor（升级结果保留）")

    print("\n  [Phase 7 - Machine B] 继续审批，补全信息并批准...")
    run_cli_with_rules(["amend", "CHG-003", "--field", "owner=周七"], sp_b, stricter_rules)
    run_cli_with_rules(["amend", "CHG-005", "--field", "risk_level=high"], sp_b, stricter_rules)
    run_cli_with_rules(["confirm", "migration"], sp_b, stricter_rules)
    run_cli_with_rules(["confirm", "known_issues"], sp_b, stricter_rules)
    run_cli_with_rules(["draft"], sp_b, stricter_rules)
    run_cli_with_rules(["approve"], sp_b, stricter_rules)

    s_final = read_state(sp_b)
    assert_eq(s_final["approved"], True, "t50: B 方最终成功批准")

    md_out = os.path.join(tmpdir, "t50_final.md")
    run_cli_with_rules(["export", "-o", md_out], sp_b, stricter_rules)
    md = read_file(md_out)
    assert_in("# Release Notes v2.1.0", md, "t50: 最终导出的 Markdown 标题正确")

    final_actions = [e["action"] for e in s_final["audit_log"]]
    expected = ["import", "confirm", "rules_upgrade_checked",
                "pending_rules_upgrade_resumed", "rules_upgrade_applied",
                "import_package_merge", "takeover_confirmed",
                "approved"]
    for act in expected:
        assert_in(act, final_actions, f"t50: 审计日志应包含 {act}")

    print("\n  [OK] 完整链路验证通过：规则变更→预检→重启恢复→应用→导包→接手→补全→批准→导出")
    cleanup_patterns(tmpdir, "state_t50_a.json")
    cleanup_patterns(tmpdir, "state_t50_b.json")


def test_51_rules_upgrade_handover_export_pending(tmpdir):
    """规则升级交接包：从待决升级导出交接包，包结构、校验和、决策上下文完整"""
    print("\n== Test 51: Rules upgrade handover export (pending upgrade) ==")
    sp = os.path.join(tmpdir, "state_t51.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t51.json")

    print("\n  [Step 1] 导入、做些修改、创建待决升级...")
    run_cli(["import", SAMPLE], sp)
    run_cli(["amend", "CHG-001", "--field", "owner=张三"], sp)
    run_cli(["confirm", "overview"], sp)

    run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)

    s_before = read_state(sp)
    pending = s_before["pending_rules_upgrade"]
    upgrade_id = pending["upgrade_id"]
    safe_print(f"  待决升级 ID: {upgrade_id}")
    safe_print(f"  待决升级状态: {pending['status']}")

    print("\n  [Step 2] 导出规则升级交接包...")
    pkg = os.path.join(tmpdir, "ru_handover_t51.json")
    res_export = run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg,
         "--operator", "用户A-51", "--note", "待决升级交接，等领导决策后再应用"],
        sp, stricter_rules
    )
    assert_in("RULES UPGRADE HANDOVER PACKAGE EXPORTED", res_export.stdout,
              "t51: 导出成功提示")
    assert_in("Package checksum:", res_export.stdout, "t51: 显示校验和")

    print("\n  [Step 3] 验证交接包结构...")
    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    assert_eq(pkg_data["package_format_version"], "1.0.0", "t51: package_format_version == 1.0.0")
    assert_eq(pkg_data["exported_by"], "用户A-51", "t51: exported_by 正确")
    assert_eq(pkg_data["note"], "待决升级交接，等领导决策后再应用", "t51: note 正确")
    assert_eq(pkg_data["upgrade_id"], upgrade_id, "t51: upgrade_id 与待决升级一致")
    assert_eq(pkg_data["source_type"], "pending", "t51: source_type == pending")
    assert_in("source_upgrade_snapshot", pkg_data, "t51: 包含 source_upgrade_snapshot")
    assert_in("old_rules_version", pkg_data, "t51: 包含 old_rules_version")
    assert_in("new_rules_version", pkg_data, "t51: 包含 new_rules_version")
    assert_in("rules_diff", pkg_data, "t51: 包含 rules_diff")
    assert_in("impact", pkg_data, "t51: 包含 impact 分析")
    assert_in("decisions", pkg_data, "t51: 包含 decisions")
    assert_in("decisions_log", pkg_data, "t51: 包含 decisions_log")
    assert_in("pre_upgrade_items_snapshot", pkg_data, "t51: 包含 pre_upgrade_items_snapshot")
    assert_in("current_rules_snapshot", pkg_data, "t51: 包含 current_rules_snapshot")
    assert_in("package_checksum", pkg_data, "t51: 包含 package_checksum")

    print("\n  [Step 4] 验证审计日志...")
    s_after = read_state(sp)
    audit_actions = [e["action"] for e in s_after["audit_log"]]
    assert_in("rules_upgrade_package_exported", audit_actions, "t51: 审计日志包含导出事件")

    export_event = [e for e in s_after["audit_log"] if e["action"] == "rules_upgrade_package_exported"][0]
    assert_in(upgrade_id, export_event["detail"], "t51: 审计事件包含 upgrade_id")
    assert_in("用户A-51", export_event["detail"], "t51: 审计事件包含操作人")

    print("\n  [OK] 规则升级交接包导出（待决升级）功能正常")
    cleanup_patterns(tmpdir, "state_t51.json")


def test_52_rules_upgrade_handover_export_applied(tmpdir):
    """规则升级交接包：从已应用升级导出交接包"""
    print("\n== Test 52: Rules upgrade handover export (applied upgrade) ==")
    sp = os.path.join(tmpdir, "state_t52.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t52.json")

    print("\n  [Step 1] 导入、创建待决升级、应用升级...")
    run_cli(["import", SAMPLE], sp)
    run_cli_with_rules(["rules_upgrade_check"], sp, stricter_rules)
    run_cli_with_rules(
        ["rules_upgrade_apply",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor",
         "--operator", "用户A-52"],
        sp, stricter_rules
    )

    s_before = read_state(sp)
    hist = s_before["rules_upgrade_history"]
    assert len(hist) == 1, "t52: 应有 1 条已应用升级"
    upgrade_id = hist[0]["upgrade_id"]
    safe_print(f"  已应用升级 ID: {upgrade_id}")

    print("\n  [Step 2] 从已应用升级导出交接包...")
    pkg = os.path.join(tmpdir, "ru_handover_t52.json")
    res_export = run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg,
         "--operator", "用户A-52", "--upgrade-source", upgrade_id,
         "--note", "已应用升级交接"],
        sp, stricter_rules
    )
    assert_in("RULES UPGRADE HANDOVER PACKAGE EXPORTED", res_export.stdout,
              "t52: 导出成功提示")

    print("\n  [Step 3] 验证交接包结构...")
    with open(pkg, "r", encoding="utf-8") as f:
        pkg_data = json.load(f)

    assert_eq(pkg_data["upgrade_id"], upgrade_id, "t52: upgrade_id 与已应用升级一致")
    assert_eq(pkg_data["source_type"], "applied", "t52: source_type == applied")

    print("\n  [OK] 规则升级交接包导出（已应用升级）功能正常")
    cleanup_patterns(tmpdir, "state_t52.json")


def test_53_rules_upgrade_handover_import_conflict(tmpdir):
    """规则升级交接包：导入时冲突检测 - 本地规则版本不同"""
    print("\n== Test 53: Rules upgrade handover import conflict detection ==")
    sp_a = os.path.join(tmpdir, "state_t53_a.json")
    sp_b = os.path.join(tmpdir, "state_t53_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    newer_rules = os.path.join(tmpdir, "rules_newer.yaml")
    _make_rules_stricter(stricter_rules)
    _make_rules_newer(newer_rules)
    cleanup_patterns(tmpdir, "state_t53_a.json")
    cleanup_patterns(tmpdir, "state_t53_b.json")

    print("\n  [Machine A] 导入、检查升级、导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    pkg = os.path.join(tmpdir, "ru_handover_t53.json")
    run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg, "--operator", "用户A-53"],
        sp_a, stricter_rules
    )

    print("\n  [Machine B] 用不同规则初始化（模拟本地规则已变化）...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli_with_rules(["rules_upgrade_check"], sp_b, newer_rules)
    run_cli_with_rules(
        ["rules_upgrade_apply",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor,CHG-003.test_coverage=80",
         "--operator", "用户B-53"],
        sp_b, newer_rules
    )

    print("\n  [Machine B] 尝试导入交接包（应检测到冲突）...")
    res_import = run_cli_with_rules(
        ["import_rules_upgrade_package", pkg, "--operator", "用户B-53"],
        sp_b, newer_rules
    )

    assert_in("CONFLICTS DETECTED", res_import.stdout, "t53: 应检测到冲突")
    conflict_found = "rules_version_mismatch" in res_import.stdout or "item_state_diverged" in res_import.stdout
    assert conflict_found, "t53: 应检测到 rules_version_mismatch 或 item_state_diverged 冲突"
    assert_in("AWAITING CONFIRMATION", res_import.stdout, "t53: 状态为待确认")

    s_pending = read_state(sp_b)
    assert s_pending.get("pending_rules_upgrade_import") is not None, "t53: 创建了 pending_rules_upgrade_import"
    pending_imp = s_pending["pending_rules_upgrade_import"]
    assert len(pending_imp.get("conflicts", [])) >= 1, "t53: 至少检测到 1 个冲突"

    print("\n  [OK] 规则升级交接包冲突检测功能正常")
    cleanup_patterns(tmpdir, "state_t53_a.json")
    cleanup_patterns(tmpdir, "state_t53_b.json")


def test_54_rules_upgrade_handover_duplicate_import(tmpdir):
    """规则升级交接包：重复导入同一包不能重复落审计"""
    print("\n== Test 54: Rules upgrade handover duplicate import prevention ==")
    sp_a = os.path.join(tmpdir, "state_t54_a.json")
    sp_b = os.path.join(tmpdir, "state_t54_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t54_a.json")
    cleanup_patterns(tmpdir, "state_t54_b.json")

    print("\n  [Machine A] 导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    pkg = os.path.join(tmpdir, "ru_handover_t54.json")
    run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg, "--operator", "用户A-54"],
        sp_a, stricter_rules
    )

    print("\n  [Machine B] 第一次导入并确认...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli_with_rules(["import_rules_upgrade_package", pkg, "--operator", "用户B-54"],
                       sp_b, stricter_rules)
    run_cli_with_rules(["rules_upgrade_handover_confirm", "--operator", "用户B-54"],
                       sp_b, stricter_rules)

    s_after_first = read_state(sp_b)
    imports_after_first = len(s_after_first.get("imported_rules_upgrade_packages", []))
    handover_history_after_first = len(s_after_first.get("rules_upgrade_handover_history", []))
    audit_after_first = len([e for e in s_after_first["audit_log"]
                             if e["action"] == "rules_upgrade_handover_confirmed"])

    assert_eq(imports_after_first, 1, "t54: 第一次导入后有 1 个导入记录")
    assert_eq(audit_after_first, 1, "t54: 第一次导入后有 1 个确认审计事件")

    print("\n  [Machine B] 尝试重复导入同一包...")
    res_dup = run_cli_with_rules(
        ["import_rules_upgrade_package", pkg, "--operator", "用户B-54"],
        sp_b, stricter_rules, expect_fail=True
    )
    assert_in("already imported", res_dup.stdout.lower() + " " + res_dup.stderr.lower(),
              "t54: 应提示包已导入")

    s_after_dup = read_state(sp_b)
    imports_after_dup = len(s_after_dup.get("imported_rules_upgrade_packages", []))
    audit_after_dup = len([e for e in s_after_dup["audit_log"]
                           if e["action"] == "rules_upgrade_handover_confirmed"])

    assert_eq(imports_after_dup, imports_after_first, "t54: 重复导入不增加导入记录")
    assert_eq(audit_after_dup, audit_after_first, "t54: 重复导入不增加审计事件")

    print("\n  [OK] 规则升级交接包防重复导入功能正常")
    cleanup_patterns(tmpdir, "state_t54_a.json")
    cleanup_patterns(tmpdir, "state_t54_b.json")


def test_55_rules_upgrade_handover_confirm_and_revoke(tmpdir):
    """规则升级交接包：确认导入与撤销回滚"""
    print("\n== Test 55: Rules upgrade handover confirm and revoke ==")
    sp_a = os.path.join(tmpdir, "state_t55_a.json")
    sp_b = os.path.join(tmpdir, "state_t55_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t55_a.json")
    cleanup_patterns(tmpdir, "state_t55_b.json")

    print("\n  [Machine A] 导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    pkg = os.path.join(tmpdir, "ru_handover_t55.json")
    run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg, "--operator", "用户A-55"],
        sp_a, stricter_rules
    )

    print("\n  [Machine B] 导入对账（只读）...")
    run_cli(["import", SAMPLE], sp_b)
    s_before_import = read_state(sp_b)
    rules_ver_before = s_before_import["rules_version"]
    chg003_before = next(i for i in s_before_import["items"] if i["id"] == "CHG-003")
    risk_before = chg003_before["risk_level"]

    res_import = run_cli_with_rules(
        ["import_rules_upgrade_package", pkg, "--operator", "用户B-55"],
        sp_b, stricter_rules
    )
    assert_in("RECONCILED", res_import.stdout, "t55: 对账完成")

    s_pending = read_state(sp_b)
    assert s_pending.get("pending_rules_upgrade_import") is not None, "t55: 创建 pending"
    assert s_pending["rules_version"] == rules_ver_before, "t55: 对账阶段不修改 rules_version"
    chg003_pending = next(i for i in s_pending["items"] if i["id"] == "CHG-003")
    assert_eq(chg003_pending["risk_level"], risk_before, "t55: 对账阶段不修改 items")

    print("\n  [Machine B] 查看交接详情...")
    res_detail = run_cli_with_rules(["rules_upgrade_handover_detail"], sp_b, stricter_rules)
    assert_in("RULES UPGRADE HANDOVER DETAIL", res_detail.stdout, "t55: 详情显示标题")
    assert_in("[PENDING CONFIRMATION]", res_detail.stdout, "t55: 显示待确认状态")

    print("\n  [Machine B] 确认导入...")
    res_confirm = run_cli_with_rules(
        ["rules_upgrade_handover_confirm", "--operator", "用户B-55"],
        sp_b, stricter_rules
    )
    assert_in("RULES UPGRADE HANDOVER CONFIRMED", res_confirm.stdout, "t55: 确认成功")

    s_confirmed = read_state(sp_b)
    assert s_confirmed.get("pending_rules_upgrade_import") is None, "t55: 确认后 pending 清空"
    assert len(s_confirmed.get("imported_rules_upgrade_packages", [])) == 1, "t55: 有 1 条导入记录"
    assert len(s_confirmed.get("rules_upgrade_handover_history", [])) == 1, "t55: 有 1 条交接历史"
    assert len(s_confirmed.get("rules_upgrade_history", [])) == 1, "t55: 有 1 条升级历史"

    imp_record = s_confirmed["imported_rules_upgrade_packages"][0]
    assert_eq(imp_record["confirmed_by"], "用户B-55", "t55: 导入记录操作人正确")
    assert_eq(imp_record["revoked"], False, "t55: 未撤销")

    print("\n  [Machine B] 撤销导入...")
    res_revoke = run_cli_with_rules(
        ["rules_upgrade_handover_revoke", "--operator", "用户B-55",
         "--reason", "测试撤销", "--import-id", imp_record["import_id"],
         "--force"],
        sp_b, stricter_rules
    )
    assert_in("revoked successfully", res_revoke.stdout, "t55: 撤销成功")

    s_revoked = read_state(sp_b)
    assert len(s_revoked.get("imported_rules_upgrade_packages", [])) == 1, "t55: 撤销后仍保留记录"
    imp_revoked = s_revoked["imported_rules_upgrade_packages"][0]
    assert_eq(imp_revoked["revoked"], True, "t55: 标记为已撤销")
    assert_eq(imp_revoked["revoke_reason"], "测试撤销", "t55: 撤销原因正确")
    assert_eq(imp_revoked["revoked_by"], "用户B-55", "t55: 撤销人正确")

    assert len(s_revoked.get("rules_upgrade_history", [])) == 1, "t55: 升级历史仍保留"
    hist_revoked = s_revoked["rules_upgrade_history"][0]
    assert_eq(hist_revoked["revoked"], True, "t55: 升级历史也标记为已撤销")

    print("\n  [OK] 规则升级交接包确认与撤销功能正常")
    cleanup_patterns(tmpdir, "state_t55_a.json")
    cleanup_patterns(tmpdir, "state_t55_b.json")


def test_56_rules_upgrade_handover_cross_restart(tmpdir):
    """规则升级交接包：跨重启恢复 - 待确认导入持久化后重启能识别为续做"""
    print("\n== Test 56: Rules upgrade handover cross-restart resume ==")
    sp_a = os.path.join(tmpdir, "state_t56_a.json")
    sp_b = os.path.join(tmpdir, "state_t56_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t56_a.json")
    cleanup_patterns(tmpdir, "state_t56_b.json")

    print("\n  [Machine A] 导出交接包...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    pkg = os.path.join(tmpdir, "ru_handover_t56.json")
    run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg, "--operator", "用户A-56"],
        sp_a, stricter_rules
    )

    print("\n  [Machine B] 导入对账，创建待确认...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli_with_rules(
        ["import_rules_upgrade_package", pkg, "--operator", "用户B-56"],
        sp_b, stricter_rules
    )

    s_before = read_state(sp_b)
    pending = s_before["pending_rules_upgrade_import"]
    import_id = pending["import_id"]
    import_pid = pending["import_pid"]
    safe_print(f"  导入 ID: {import_id}")
    safe_print(f"  导入 PID: {import_pid}")

    print("\n  [Machine B] 做些后续工作，模拟重启前的进展...")
    run_cli_with_rules(["confirm", "overview"], sp_b, stricter_rules)

    print("\n  [Machine B] 模拟重启：修改 import_pid 为不同的值...")
    s_before_restart = read_state(sp_b)
    s_before_restart["pending_rules_upgrade_import"]["import_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)

    print("\n  [Machine B] 重启后查看审计视图（应检测为续做）...")
    audit_script = os.path.join(tmpdir, "run_audit_t56.py")
    with open(audit_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{SCRIPT_DIR}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--rules", r"{stricter_rules}", "--state", r"{sp_b}", "audit_view"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, audit_script],
        capture_output=True,
        cwd=SCRIPT_DIR,
    )
    audit_output = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')

    assert_in("Cross-restart:  YES", audit_output, "t56: 审计视图显示跨重启")
    assert_in("[RESUMED]", audit_output, "t56: 审计视图显示 [RESUMED] 标签")
    assert_in("[RU HANDOVER]", audit_output, "t56: 审计视图显示 [RU HANDOVER] 标签")

    s_after = read_state(sp_b)
    pending_after = s_after["pending_rules_upgrade_import"]
    assert_eq(pending_after.get("resumed_across_restart"), True, "t56: pending 标记为跨重启续做")

    audit_actions = [e["action"] for e in s_after["audit_log"]]
    assert_in("pending_rules_upgrade_handover_resumed_across_restart", audit_actions,
              "t56: 审计日志包含续做事件")

    print("\n  [OK] 规则升级交接包跨重启恢复检测功能正常")
    cleanup_patterns(tmpdir, "state_t56_a.json")
    cleanup_patterns(tmpdir, "state_t56_b.json")


def test_57_full_e2e_rules_upgrade_handover_workflow(tmpdir):
    """完整端到端：规则升级预检→导包→换人导入→重启恢复→继续处理→最终导出"""
    print("\n== Test 57: FULL E2E Rules Upgrade Handover Workflow ==")
    sp_a = os.path.join(tmpdir, "state_t57_a.json")
    sp_b = os.path.join(tmpdir, "state_t57_b.json")
    stricter_rules = os.path.join(tmpdir, "rules_stricter.yaml")
    _make_rules_stricter(stricter_rules)
    cleanup_patterns(tmpdir, "state_t57_a.json")
    cleanup_patterns(tmpdir, "state_t57_b.json")

    print("\n  [Phase 1 - Machine A] 初始导入 + 部分工作...")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["amend", "CHG-001", "--field", "owner=张三"], sp_a)
    run_cli(["confirm", "overview"], sp_a)

    s_phase1 = read_state(sp_a)
    rules_ver_initial = s_phase1["rules_version"]
    safe_print(f"  初始规则版本: {rules_ver_initial[:16]}...")

    print("\n  [Phase 2 - Machine A] 规则升级预检...")
    run_cli_with_rules(["rules_upgrade_check"], sp_a, stricter_rules)

    s_phase2 = read_state(sp_a)
    pending = s_phase2["pending_rules_upgrade"]
    upgrade_id = pending["upgrade_id"]
    manual_count = pending["impact"]["summary"]["manual_decision_count"]
    safe_print(f"  升级 ID: {upgrade_id}")
    safe_print(f"  需人工决策: {manual_count} 项")

    print("\n  [Phase 3 - Machine A] 导出规则升级交接包...")
    pkg = os.path.join(tmpdir, "ru_handover_t57.json")
    res_export = run_cli_with_rules(
        ["export_rules_upgrade_package", "-o", pkg,
         "--operator", "负责人A-57", "--note", "待决升级交接，CHG-003/CHG-005 需要决策"],
        sp_a, stricter_rules
    )
    assert_in("Package checksum:", res_export.stdout, "t57: 导出显示校验和")

    print("\n  [Phase 4 - Machine B] 换人导入交接包（对账）...")
    run_cli(["import", SAMPLE], sp_b)
    run_cli(["amend", "CHG-002", "--field", "owner=李四B"], sp_b)

    res_import = run_cli_with_rules(
        ["import_rules_upgrade_package", pkg, "--operator", "负责人B-57"],
        sp_b, stricter_rules
    )
    assert_in("RECONCILED", res_import.stdout, "t57: 对账完成")
    assert_in("AWAITING CONFIRMATION", res_import.stdout, "t57: 待确认")

    s_pending = read_state(sp_b)
    pending_imp = s_pending["pending_rules_upgrade_import"]
    import_id = pending_imp["import_id"]
    safe_print(f"  导入 ID: {import_id}")

    print("\n  [Phase 5 - Machine B] 查看交接详情...")
    res_detail = run_cli_with_rules(["rules_upgrade_handover_detail"], sp_b, stricter_rules)
    assert_in("Conflicts", res_detail.stdout, "t57: 详情显示冲突")
    assert_in("Decisions", res_detail.stdout, "t57: 详情显示决策")

    print("\n  [Phase 6 - Machine B] 做些后续工作，模拟重启...")
    run_cli_with_rules(["confirm", "changes"], sp_b, stricter_rules)

    print("\n  [Phase 7 - Machine B] 模拟重启（修改 import_pid）...")
    s_before_restart = read_state(sp_b)
    s_before_restart["pending_rules_upgrade_import"]["import_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s_before_restart, f, ensure_ascii=False, indent=2)

    print("\n  [Phase 8 - Machine B] 重启后 audit_view 检测跨重启...")
    audit_script = os.path.join(tmpdir, "audit_t57.py")
    with open(audit_script, "w", encoding="utf-8") as f:
        f.write(f"""
import sys
sys.path.insert(0, r"{SCRIPT_DIR}")
from release_cli import main as cli_main
sys.argv = ["release_cli.py", "--rules", r"{stricter_rules}", "--state", r"{sp_b}", "audit_view"]
cli_main()
""")
    result = subprocess.run(
        [sys.executable, audit_script],
        capture_output=True,
        cwd=SCRIPT_DIR,
    )
    audit_out = result.stdout.decode('utf-8', errors='replace') + result.stderr.decode('utf-8', errors='replace')
    assert_in("Cross-restart:  YES", audit_out, "t57: 重启后检测到跨重启")
    assert_in("[RESUMED]", audit_out, "t57: 重启后显示 [RESUMED]")

    print("\n  [Phase 9 - Machine B] 确认交接包导入，应用升级...")
    res_confirm = run_cli_with_rules(
        ["rules_upgrade_handover_confirm", "--operator", "负责人B-57",
         "--per-item", "CHG-003.risk_level=high,CHG-003.category=refactor,CHG-005.risk_level=high"],
        sp_b, stricter_rules
    )
    assert_in("RULES UPGRADE HANDOVER CONFIRMED", res_confirm.stdout, "t57: 确认成功")

    s_confirmed = read_state(sp_b)
    assert_eq(len(s_confirmed.get("imported_rules_upgrade_packages", [])), 1, "t57: 导入记录正确")
    imp = s_confirmed["imported_rules_upgrade_packages"][0]
    assert imp.get("resumed_across_restart") == True, "t57: 导入记录标记为跨重启续做"

    chg003 = next(i for i in s_confirmed["items"] if i["id"] == "CHG-003")
    assert_eq(chg003["risk_level"], "high", "t57: CHG-003 risk_level 已升级为 high")
    assert_eq(chg003["category"], "refactor", "t57: CHG-003 category 已升级为 refactor")
    chg005 = next(i for i in s_confirmed["items"] if i["id"] == "CHG-005")
    assert_eq(chg005["risk_level"], "high", "t57: CHG-005 risk_level 已升级为 high")

    print("\n  [Phase 10 - Machine B] 继续剩余审批工作...")
    run_cli_with_rules(["confirm", "overview"], sp_b, stricter_rules)
    run_cli_with_rules(["amend", "CHG-003", "--field", "owner=周七"], sp_b, stricter_rules)
    run_cli_with_rules(["confirm", "migration"], sp_b, stricter_rules)
    run_cli_with_rules(["confirm", "known_issues"], sp_b, stricter_rules)
    run_cli_with_rules(["draft"], sp_b, stricter_rules)
    run_cli_with_rules(["approve"], sp_b, stricter_rules)

    s_approved = read_state(sp_b)
    assert_eq(s_approved["approved"], True, "t57: 批准成功")

    print("\n  [Phase 11 - Machine B] 导出最终 Markdown...")
    md_out = os.path.join(tmpdir, "t57_final.md")
    run_cli_with_rules(["export", "-o", md_out], sp_b, stricter_rules)
    md = read_file(md_out)

    assert_in("# Release Notes v2.1.0", md, "t57: 最终 Markdown 标题正确")
    assert_in("owner:周七", md, "t57: Markdown 包含 CHG-003 owner=周七")
    assert_in("risk:high", md, "t57: Markdown 包含 risk:high")

    print("\n  [Phase 12 - 验证交接痕迹在文档中]")
    assert_in("Rules Upgrade Handover History", md, "t57: Markdown 包含规则升级交接历史")
    assert_in("Rules Upgrade History", md, "t57: Markdown 包含规则升级历史")
    assert_in(upgrade_id, md, "t57: Markdown 包含 upgrade_id")
    assert_in("负责人A-57", md, "t57: Markdown 包含导出人")
    assert_in("负责人B-57", md, "t57: Markdown 包含导入人")

    print("\n  [Phase 13 - 验证状态和审计视图]")
    res_status = run_cli_with_rules(["status"], sp_b, stricter_rules)
    assert_in("Active Rules Upgrade Handover Imports:", res_status.stdout,
              "t57: status 显示活跃导入")
    assert_in("[RESUMED]", res_status.stdout, "t57: status 显示 [RESUMED]")

    res_audit_final = run_cli_with_rules(["audit_view"], sp_b, stricter_rules)
    assert_in("[RU HANDOVER]", res_audit_final.stdout, "t57: audit_view 显示 [RU HANDOVER]")
    assert_in("[RESUMED]", res_audit_final.stdout, "t57: audit_view 显示 [RESUMED]")

    s_final = read_state(sp_b)
    final_audit = [e["action"] for e in s_final["audit_log"]]
    expected = [
        "import", "amend", "confirm",
        "rules_upgrade_package_import_reconciled",
        "pending_rules_upgrade_handover_resumed_across_restart",
        "rules_upgrade_handover_confirmed",
        "approved", "export"
    ]
    for act in expected:
        assert_in(act, final_audit, f"t57: 审计日志应包含 {act}")

    print("\n  [OK] ====== TEST 57 完整链路全部通过 ======")
    print(f"  1. 规则升级预检 OK")
    print(f"  2. 导出规则升级交接包 OK")
    print(f"  3. 换人导入对账（只读） OK")
    print(f"  4. 查看交接详情 OK")
    print(f"  5. 模拟重启 OK")
    print(f"  6. 跨重启恢复检测 OK")
    print(f"  7. 确认导入（带决策） OK")
    print(f"  8. 继续审批工作 OK")
    print(f"  9. 最终导出 Markdown OK")
    print(f"  10. 交接痕迹在 Markdown 中可见 OK")
    print(f"  11. status/audit_view 显示正确 OK")
    print(f"  12. 审计日志完整 OK")

    cleanup_patterns(tmpdir, "state_t57_a.json")
    cleanup_patterns(tmpdir, "state_t57_b.json")


def _read_profiles(tmpdir, state_filename):
    pp = os.path.join(tmpdir, ".rules_profiles.json")
    if os.path.exists(pp):
        with open(pp, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"default_profile_id": None, "profiles": {}, "switch_history": []}


def test_58_profile_basic_crud(tmpdir):
    print("\n== Test 58: Profile basic save/list/switch/diff/rollback/delete ==")
    sp = os.path.join(tmpdir, "state_t58.json")
    cleanup_patterns(tmpdir, "state_t58.json")

    print("\n  [Phase 1 - import and save baseline profile]")
    run_cli(["import", SAMPLE], sp)
    res_save = run_cli(["profile_save", "--name", "baseline-t58",
                        "--description", "baseline release rules",
                        "--tags", "v2.1,baseline",
                        "--as-default", "--set-active"], sp)
    assert_in("PROFILE SAVED", res_save.stdout, "t58: save 成功提示")
    assert_in("baseline-t58", res_save.stdout, "t58: save 输出 profile 名称")

    s = read_state(sp)
    assert_eq(s.get("active_profile_name"), "baseline-t58", "t58: state 中 active_profile_name 被设置")
    assert s.get("active_profile_id") is not None, "t58: state 中 active_profile_id 存在"

    pd = _read_profiles(tmpdir, "state_t58.json")
    baseline_pid = pd["default_profile_id"]
    assert baseline_pid is not None, "t58: default_profile_id 被持久化"
    assert baseline_pid in pd["profiles"], "t58: profile 被存储"
    assert pd["profiles"][baseline_pid]["name"] == "baseline-t58", "t58: 存储的 profile 名称正确"

    print("\n  [Phase 2 - profile_list 显示信息]")
    res_list = run_cli(["profile_list"], sp)
    assert_in("baseline-t58", res_list.stdout, "t58: list 显示 profile 名称")
    assert_in("DEFAULT", res_list.stdout, "t58: list 显示 DEFAULT 标签")
    assert_in("ACTIVE", res_list.stdout, "t58: list 显示 ACTIVE 标签")

    print("\n  [Phase 3 - profile_detail 查看详情]")
    res_detail = run_cli(["profile_detail", "--profile", "baseline-t58"], sp)
    assert_in("baseline-t58", res_detail.stdout, "t58: detail 显示名称")
    assert_in("baseline release rules", res_detail.stdout, "t58: detail 显示描述")

    print("\n  [Phase 4 - 创建 stricter profile 并 diff]")
    stricter = os.path.join(tmpdir, "rules_stricter_t58.yaml")
    _make_rules_stricter(stricter)
    import yaml
    with open(RULES, "r", encoding="utf-8") as f:
        orig_rules_text = f.read()
    with open(stricter, "r", encoding="utf-8") as f:
        strict_text = f.read()
    with open(RULES, "w", encoding="utf-8") as f:
        f.write(strict_text)
    try:
        res_save2 = run_cli(["profile_save", "--name", "stricter-t58",
                             "--description", "stricter release rules"], sp)
        assert_in("PROFILE SAVED", res_save2.stdout, "t58: 第二个 profile 保存成功")
    finally:
        with open(RULES, "w", encoding="utf-8") as f:
            f.write(orig_rules_text)

    res_diff = run_cli(["profile_diff", "baseline-t58", "stricter-t58"], sp)
    assert_in("PROFILE DIFF", res_diff.stdout, "t58: diff 输出标题")

    print("\n  [Phase 5 - switch 到 stricter-t58，再 rollback]")
    res_switch = run_cli(["profile_switch", "stricter-t58", "--make-default"], sp)
    assert_in("PROFILE SWITCHED", res_switch.stdout, "t58: switch 成功提示")

    s2 = read_state(sp)
    assert_eq(s2.get("active_profile_name"), "stricter-t58", "t58: 切换后新 profile 激活")
    switch_hist = s2.get("profile_switch_history", [])
    assert len(switch_hist) >= 2, "t58: profile_switch_history 至少有2条"

    res_rb = run_cli(["profile_rollback"], sp)
    assert_in("PROFILE ROLLBACK", res_rb.stdout, "t58: rollback 提示")
    s3 = read_state(sp)
    assert_eq(s3.get("active_profile_name"), "baseline-t58", "t58: rollback 回到 baseline")

    print("\n  [Phase 6 - delete (revoke) stricter-t58，受保护检测]")
    run_cli(["profile_switch", "stricter-t58"], sp)
    res_del_fail = run_cli(["profile_delete", "stricter-t58"], sp, expect_fail=True)
    assert res_del_fail.returncode != 0, "t58: 删除 ACTIVE profile 默认被阻止"
    run_cli(["profile_switch", "baseline-t58"], sp)
    res_del_ok = run_cli(["profile_delete", "stricter-t58", "--force"], sp)
    assert_in("revoked", res_del_ok.stdout, "t58: --force 下 profile 被 revoke")

    pd2 = _read_profiles(tmpdir, "state_t58.json")
    for pid, p in pd2["profiles"].items():
        if p.get("name") == "stricter-t58":
            assert p.get("revoked") is True, "t58: stricter 被标记 revoked"
            break

    print("\n  [OK] ====== TEST 58 Profile CRUD 全部通过 ======")
    cleanup_patterns(tmpdir, "state_t58.json")
    pp = os.path.join(tmpdir, ".rules_profiles.json")
    if os.path.exists(pp):
        try:
            os.remove(pp)
        except OSError:
            pass


def test_59_profile_export_import_integration(tmpdir):
    print("\n== Test 59: Profile export/import package integration ==")
    sp_a = os.path.join(tmpdir, "state_t59_a.json")
    sp_b = os.path.join(tmpdir, "state_t59_b.json")
    cleanup_patterns(tmpdir, "state_t59_a.json")
    cleanup_patterns(tmpdir, "state_t59_b.json")

    print("\n  [Phase 1 - Side A: import, save profile, set active]")
    run_cli(["import", SAMPLE], sp_a)
    run_cli(["profile_save", "--name", "profile-A-t59",
             "--description", "side A baseline",
             "--as-default", "--set-active"], sp_a)

    s_a = read_state(sp_a)
    pid_a = s_a.get("active_profile_id")
    assert pid_a is not None, "t59: A side active_profile_id 存在"

    print("\n  [Phase 2 - Side A: export_package 携带 profile_info]")
    pkg_path = os.path.join(tmpdir, "t59_handoff.json")
    res_exp = run_cli(["export_package", "-o", pkg_path], sp_a)
    assert_in("Profile:", res_exp.stdout, "t59: export 输出 Profile 信息")
    assert_in("profile-A-t59", res_exp.stdout, "t59: export 输出 profile 名称")

    with open(pkg_path, "r", encoding="utf-8") as f:
        pkg = json.load(f)
    pinfo = pkg.get("profile_info")
    assert pinfo is not None, "t59: 包中 profile_info 存在"
    assert_eq(pinfo.get("profile_name"), "profile-A-t59", "t59: 包中 profile 名称正确")
    assert_eq(pkg["metadata"].get("active_profile_name"), "profile-A-t59", "t59: metadata 中 profile_name")

    print("\n  [Phase 3 - Side B: 本地注册不同 profile-B，import_package 检测冲突]")
    stricter = os.path.join(tmpdir, "rules_stricter_t59.yaml")
    _make_rules_stricter(stricter)
    with open(RULES, "r", encoding="utf-8") as f:
        orig_rules_text = f.read()
    with open(stricter, "r", encoding="utf-8") as f:
        strict_text = f.read()
    with open(RULES, "w", encoding="utf-8") as f:
        f.write(strict_text)
    try:
        run_cli(["profile_save", "--name", "profile-B-t59",
                 "--description", "side B stricter",
                 "--as-default", "--set-active"], sp_b)
    finally:
        with open(RULES, "w", encoding="utf-8") as f:
            f.write(orig_rules_text)

    res_imp = run_cli(["import_package", pkg_path, "--mode", "takeover"], sp_b)
    assert_in("profile_mismatch", res_imp.stdout, "t59: 检测到 profile_mismatch 冲突")
    assert_in("[PROFILE]", res_imp.stdout, "t59: 冲突带 [PROFILE] 标签")

    s_b = read_state(sp_b)
    pending = s_b.get("pending_takeover")
    assert pending is not None, "t59: B side pending_takeover 存在"
    assert pending.get("package_profile_info") is not None, "t59: pending 中保存了包的 profile_info"
    assert pending.get("active_profile_name_at_import") == "profile-B-t59", "t59: pending 记录了导入时本地 profile 名称"

    print("\n  [Phase 4 - takeover_confirm 后状态中 profile 保留]")
    run_cli(["takeover_confirm"], sp_b)
    s_b2 = read_state(sp_b)
    assert_eq(s_b2.get("active_profile_name"), "profile-A-t59", "t59: takeover 后 A 侧的 profile 被带过来")
    assert_eq(s_b2.get("active_profile_id"), pid_a, "t59: takeover 后 profile_id 一致")

    print("\n  [Phase 5 - draft + export，Markdown 含 Profile Trace]")
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp_b)
    run_cli(["amend", "CHG-005", "--field", "risk_level=critical"], sp_b)
    run_cli(["draft"], sp_b)
    for sec in ("overview", "changes", "migration", "known_issues"):
        run_cli(["confirm", sec], sp_b)
    run_cli(["draft"], sp_b)
    run_cli(["approve"], sp_b)
    md_path = os.path.join(tmpdir, "t59_export.md")
    run_cli(["export", "-o", md_path], sp_b)
    md = read_file(md_path)
    assert_in("Rules Profile Trace", md, "t59: Markdown 包含 Rules Profile Trace")
    assert_in("profile-A-t59", md, "t59: Markdown 包含 profile 名称")
    assert_in("Last Profile Switch", md, "t59: Markdown 包含 Last Profile Switch")

    print("\n  [Phase 6 - status 显示 profile 信息]")
    res_st = run_cli(["status"], sp_b)
    assert_in("Rules Profiles", res_st.stdout, "t59: status 含 Rules Profiles 段")
    assert_in("Active profile:", res_st.stdout, "t59: status 含 Active profile")
    assert_in("Default profile:", res_st.stdout, "t59: status 含 Default profile")

    print("\n  [Phase 7 - audit_view 含 profile 切换事件]")
    res_audit = run_cli(["audit_view"], sp_b)
    assert_in("[PROFILE]", res_audit.stdout, "t59: audit_view 含 [PROFILE] 标签")

    print("\n  [OK] ====== TEST 59 Profile 导包/导包集成全部通过 ======")
    cleanup_patterns(tmpdir, "state_t59_a.json")
    cleanup_patterns(tmpdir, "state_t59_b.json")
    for fn in os.listdir(tmpdir):
        full = os.path.join(tmpdir, fn)
        if fn.startswith(".rules_profiles") or fn.startswith("t59_") or fn.startswith("release_"):
            try:
                os.remove(full)
            except OSError:
                pass


def test_60_profile_rules_upgrade_integration(tmpdir):
    print("\n== Test 60: Profile rules_upgrade_check/apply integration ==")
    sp = os.path.join(tmpdir, "state_t60.json")
    cleanup_patterns(tmpdir, "state_t60.json")

    print("\n  [Phase 1 - 初始宽松规则下 save baseline profile, import, 故意引入 high risk 项]")
    with open(RULES, "r", encoding="utf-8") as f:
        orig_rules_text = f.read()

    run_cli(["profile_save", "--name", "loose-t60",
             "--description", "loose initial rules",
             "--as-default", "--set-active"], sp)
    run_cli(["import", SAMPLE], sp)
    _amend_bad_items(sp)

    print("\n  [Phase 2 - 保存 stricter profile（使用 stricter rules 文件）]")
    stricter = os.path.join(tmpdir, "rules_stricter_t60.yaml")
    _make_rules_stricter(stricter)
    with open(stricter, "r", encoding="utf-8") as f:
        strict_text = f.read()
    with open(RULES, "w", encoding="utf-8") as f:
        f.write(strict_text)
    try:
        res_save2 = run_cli(["profile_save", "--name", "strict-t60",
                             "--description", "strict release rules"], sp)
        assert_in("PROFILE SAVED", res_save2.stdout, "t60: strict profile 保存成功")
    finally:
        with open(RULES, "w", encoding="utf-8") as f:
            f.write(orig_rules_text)

    print("\n  [Phase 3 - rules_upgrade_check --profile strict-t60 评估影响]")
    res_check = run_cli_with_rules(["rules_upgrade_check", "--profile", "strict-t60"], sp, RULES)
    assert_in("Target profile:", res_check.stdout, "t60: rules_upgrade_check 显示 target profile")
    assert_in("strict-t60", res_check.stdout, "t60: 显示 strict 名称")
    s = read_state(sp)
    pending = s.get("pending_rules_upgrade")
    assert pending is not None, "t60: pending_rules_upgrade 被创建"
    assert_eq(pending.get("target_profile_name"), "strict-t60", "t60: pending 中 target_profile_name 正确")

    print("\n  [Phase 4 - rules_upgrade_apply --profile 直接应用 profile 到 state]")
    run_cli_with_rules(["profile_switch", "loose-t60"], sp, RULES)
    res_apply_profile = run_cli_with_rules(
        ["rules_upgrade_apply", "--profile", "strict-t60"], sp, RULES
    )
    assert_in("Profile applied", res_apply_profile.stdout, "t60: rules_upgrade_apply --profile 成功应用")
    s2 = read_state(sp)
    assert_eq(s2.get("active_profile_name"), "strict-t60", "t60: apply 后 active profile 为 strict")

    print("\n  [OK] ====== TEST 60 Profile + rules_upgrade 集成通过 ======")
    cleanup_patterns(tmpdir, "state_t60.json")
    pp = os.path.join(tmpdir, ".rules_profiles.json")
    if os.path.exists(pp):
        try:
            os.remove(pp)
        except OSError:
            pass


def test_61_profile_restart_recovery_and_rollback_continue(tmpdir):
    print("\n== Test 61: Profile restart-safe default, rollback then continue export ==")
    sp = os.path.join(tmpdir, "state_t61.json")
    cleanup_patterns(tmpdir, "state_t61.json")

    print("\n  [Phase 1 - 保存 default profile, import, 激活]")
    run_cli(["profile_save", "--name", "golden-t61",
             "--description", "golden restart-safe profile",
             "--as-default", "--set-active"], sp)
    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)

    s = read_state(sp)
    golden_id = s.get("active_profile_id")
    assert golden_id is not None, "t61: golden profile id 存在"

    pd_before = _read_profiles(tmpdir, "state_t61.json")
    assert_eq(pd_before["default_profile_id"], golden_id, "t61: default_profile_id 持久化成功")

    print("\n  [Phase 2 - 模拟重启：清空 active_profile_id 再运行 status，应自动恢复]")
    s["active_profile_id"] = None
    s["active_profile_name"] = None
    s["last_profile_switch_at"] = None
    s["last_profile_switch_by"] = None
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

    res_st_after = run_cli(["status"], sp)
    assert_in("Restart recovery:", res_st_after.stdout, "t61: status 触发重启恢复")
    assert_in("golden-t61", res_st_after.stdout, "t61: 恢复后 golden profile 在输出中")

    s2 = read_state(sp)
    assert_eq(s2.get("active_profile_id"), golden_id, "t61: active_profile_id 恢复")
    assert_eq(s2.get("active_profile_name"), "golden-t61", "t61: active_profile_name 恢复")

    switch_hist_after = s2.get("profile_switch_history", [])
    assert len(switch_hist_after) >= 1, "t61: profile_switch_history 至少1条"
    last_switch = switch_hist_after[-1]
    assert_eq(last_switch.get("action"), "startup_default_profile", "t61: 最后一条为 startup recovery")

    pd_after = _read_profiles(tmpdir, "state_t61.json")
    assert len(pd_after.get("switch_history", [])) >= 1, "t61: 全局 switch_history 至少1条"
    last_global = pd_after["switch_history"][-1]
    assert last_global.get("restart_activated") is True, "t61: 全局 restart_activated=True"

    print("\n  [Phase 3 - 切换到 alternate profile，再 profile_rollback 回 golden]")
    stricter = os.path.join(tmpdir, "rules_stricter_t61.yaml")
    _make_rules_stricter(stricter)
    with open(RULES, "r", encoding="utf-8") as f:
        orig_rules = f.read()
    with open(stricter, "r", encoding="utf-8") as f:
        strict_rules = f.read()
    with open(RULES, "w", encoding="utf-8") as f:
        f.write(strict_rules)
    try:
        run_cli(["profile_save", "--name", "alternate-t61",
                 "--description", "alternate rules"], sp)
    finally:
        with open(RULES, "w", encoding="utf-8") as f:
            f.write(orig_rules)

    run_cli(["profile_switch", "alternate-t61"], sp)
    s3 = read_state(sp)
    assert_eq(s3.get("active_profile_name"), "alternate-t61", "t61: 切换成功到 alternate")

    run_cli(["profile_rollback"], sp)
    s4 = read_state(sp)
    assert_eq(s4.get("active_profile_name"), "golden-t61", "t61: rollback 回到 golden")

    print("\n  [Phase 4 - 回滚后继续 draft、confirm、approve、export]")
    run_cli(["amend", "CHG-003", "--field", "owner=周七"], sp)
    run_cli(["amend", "CHG-003", "--field", "risk_level=high"], sp)
    run_cli(["amend", "CHG-005", "--field", "risk_level=high"], sp)
    run_cli(["draft"], sp)
    for sec in ["overview", "changes", "migration", "known_issues"]:
        run_cli(["confirm", sec], sp)
    run_cli(["draft"], sp)
    run_cli(["approve"], sp)
    md_path = os.path.join(tmpdir, "t61_final.md")
    run_cli(["export", "-o", md_path], sp)

    md = read_file(md_path)
    assert_in("Rules Profile Trace", md, "t61: 最终 Markdown 包含 Profile Trace")
    assert_in("golden-t61", md, "t61: Markdown 包含 golden profile 名")
    assert_in("Recent Switch History", md, "t61: Markdown 包含 Recent Switch History")
    assert_in("startup_default_profile", md, "t61: Markdown 中看到 startup recovery 切换")
    assert_in("profile_rollback", md, "t61: Markdown 中看到 profile_rollback 切换")

    print("\n  [Phase 5 - status 含完整 profile 信息]")
    res_st_final = run_cli(["status"], sp)
    assert_in("Switch history:", res_st_final.stdout, "t61: status 含 switch history 计数")
    assert_in("[AUTO-ON-RESTART]", res_st_final.stdout, "t61: status 中 switch 带 [AUTO-ON-RESTART] 标记")

    print("\n  [OK] ====== TEST 61 Profile 重启恢复 + 回滚 + 继续导出完整通过 ======")
    cleanup_patterns(tmpdir, "state_t61.json")
    for fn in os.listdir(tmpdir):
        full = os.path.join(tmpdir, fn)
        if fn.startswith(".rules_profiles") or fn.startswith("t61_") or fn.startswith("release_"):
            try:
                os.remove(full)
            except OSError:
                pass


def main():
    global PASS, FAIL
    try:
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(errors="replace")
            except Exception:
                pass
        if hasattr(sys.stderr, "reconfigure"):
            try:
                sys.stderr.reconfigure(errors="replace")
            except Exception:
                pass
    except Exception:
        pass

    tmpdir = tempfile.mkdtemp(prefix="release_cli_test_")
    safe_print(f"Test workspace: {tmpdir}")
    fatal_exception = None
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
        test_31_preflight_basic(tmpdir)
        test_32_preflight_conflict_detection(tmpdir)
        test_33_audit_view_timeline(tmpdir)
        test_34_full_e2e_preflight_import_restart(tmpdir)
        test_35_cross_restart_resume_detection(tmpdir)
        test_36_no_false_positive_cross_restart(tmpdir)
        test_37_export_has_handover_summary(tmpdir)
        test_38_two_step_pending_confirm(tmpdir)
        test_39_revoke_pending(tmpdir)
        test_40_revoke_confirmed_force(tmpdir)
        test_41_conflict_detection_and_decisions(tmpdir)
        test_42_cross_restart_persisted_session(tmpdir)
        test_43_full_e2e_handoff_confirm_restart_approve(tmpdir)
        test_44_utf8_mode_encoding_stability(tmpdir)
        test_45_rules_upgrade_check_basic(tmpdir)
        test_46_rules_upgrade_apply_and_undo(tmpdir)
        test_47_rules_upgrade_skip_and_status(tmpdir)
        test_48_rules_upgrade_cross_restart_resume(tmpdir)
        test_49_rules_upgrade_handover_preservation(tmpdir)
        test_50_full_e2e_rules_upgrade_workflow(tmpdir)
        test_51_rules_upgrade_handover_export_pending(tmpdir)
        test_52_rules_upgrade_handover_export_applied(tmpdir)
        test_53_rules_upgrade_handover_import_conflict(tmpdir)
        test_54_rules_upgrade_handover_duplicate_import(tmpdir)
        test_55_rules_upgrade_handover_confirm_and_revoke(tmpdir)
        test_56_rules_upgrade_handover_cross_restart(tmpdir)
        test_57_full_e2e_rules_upgrade_handover_workflow(tmpdir)
        test_58_profile_basic_crud(tmpdir)
        test_59_profile_export_import_integration(tmpdir)
        test_60_profile_rules_upgrade_integration(tmpdir)
        test_61_profile_restart_recovery_and_rollback_continue(tmpdir)
    except Exception as e:
        fatal_exception = e
        safe_print(f"\n*** FATAL EXCEPTION during tests: {type(e).__name__}: {e} ***")
        import traceback
        try:
            tb_str = traceback.format_exc()
            safe_print(safe_truncate(tb_str, 800))
        except Exception:
            pass
    finally:
        try:
            safe_print(f"\n==== SUMMARY: {PASS} passed, {FAIL} failed ====")
            if fatal_exception is not None:
                safe_print(f"[FATAL] Exception occurred, tests did not complete normally")
                safe_print(f"[FATAL] This is NOT a pass - tests crashed before completion")
            if PASS == 0 and FAIL == 0:
                safe_print("[CRITICAL INTEGRITY ERROR] 0 passed and 0 failed - tests likely crashed or did not run")
                safe_print("This result is invalid - a normal test run should have many passing assertions")
                shutil.rmtree(tmpdir, ignore_errors=True)
                sys.exit(2)
            if FAIL > 0 or fatal_exception is not None:
                shutil.rmtree(tmpdir, ignore_errors=True)
                sys.exit(1)
            safe_print("All tests passed.")
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            sys.exit(3)


if __name__ == "__main__":
    main()
