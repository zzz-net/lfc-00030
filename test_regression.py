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


def run_cli(args, state_path, expect_fail=False, manifest_override=None):
    cmd = [sys.executable, CLI, "--rules", RULES, "--state", state_path] + args
    if manifest_override and args[0] == "import":
        cmd = [sys.executable, CLI, "--rules", RULES, "--state", state_path, "import", manifest_override]
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


def test_1_rollback_after_approve(tmpdir):
    """批准后可以回滚：清掉批准记录、恢复批准时的快照、可再重新批准+导出"""
    print("\n== Test 1: Rollback after approve ==")
    sp = os.path.join(tmpdir, "state_t1.json")
    cleanup_patterns(tmpdir, "state_t1.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    s = read_state(sp)
    for it in s["items"]:
        if not it["owner"]:
            it["owner"] = "周七"
        if it["risk_level"] == "extreme":
            it["risk_level"] = "critical"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
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
    """批准状态下禁止导入新批次"""
    print("\n== Test 2: No new import when approved ==")
    sp = os.path.join(tmpdir, "state_t2.json")
    cleanup_patterns(tmpdir, "state_t2.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    s = read_state(sp)
    for it in s["items"]:
        if not it["owner"]:
            it["owner"] = "周七"
        if it["risk_level"] == "extreme":
            it["risk_level"] = "critical"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
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
    """带 UTF-8 BOM 的清单能正常导入"""
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
    """批准后若手动修改了版本/草稿号，export会被拒绝"""
    print("\n== Test 4: Export drift rejection ==")
    sp = os.path.join(tmpdir, "state_t4.json")
    cleanup_patterns(tmpdir, "state_t4.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)
    s = read_state(sp)
    for it in s["items"]:
        if not it["owner"]:
            it["owner"] = "周七"
        if it["risk_level"] == "extreme":
            it["risk_level"] = "critical"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
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
    """重启一致性：走完流程导出后，重新load state核对"""
    print("\n== Test 5: Restart consistency across multiple runs ==")
    sp = os.path.join(tmpdir, "state_t5.json")
    cleanup_patterns(tmpdir, "state_t5.json")

    run_cli(["import", SAMPLE], sp)
    run_cli(["draft"], sp)

    s1 = read_state(sp)
    for it in s1["items"]:
        if not it["owner"]:
            it["owner"] = "周七"
        if it["risk_level"] == "extreme":
            it["risk_level"] = "critical"
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(s1, f, ensure_ascii=False, indent=2)

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
    """重复导入同一批不计数，imported_batches不变"""
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

        print(f"\n==== SUMMARY: {PASS} passed, {FAIL} failed ====")
        if FAIL:
            sys.exit(1)
        print("All tests passed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
