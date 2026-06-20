import tempfile, shutil, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_regression
from test_regression import (
    safe_print, safe_truncate,
    test_25_import_reject_target_newer,
    test_26_import_mode_takeover,
    test_27_import_mode_merge,
    test_28_import_reject_approved_target,
    test_30_full_e2e_handoff_workflow,
    test_33_audit_view_timeline,
    test_34_full_e2e_preflight_import_restart,
    test_35_cross_restart_resume_detection,
    test_36_no_false_positive_cross_restart,
    test_37_export_has_handover_summary,
    test_38_two_step_pending_confirm,
    test_39_revoke_pending,
    test_40_revoke_confirmed_force,
    test_41_conflict_detection_and_decisions,
    test_42_cross_restart_persisted_session,
    test_43_full_e2e_handoff_confirm_restart_approve,
    test_44_utf8_mode_encoding_stability,
)

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

tmpdir = tempfile.mkdtemp(prefix="release_cli_subset_")
safe_print(f"Subset test workspace: {tmpdir}")
tests = [
    ("25", test_25_import_reject_target_newer),
    ("26", test_26_import_mode_takeover),
    ("27", test_27_import_mode_merge),
    ("28", test_28_import_reject_approved_target),
    ("30", test_30_full_e2e_handoff_workflow),
    ("33", test_33_audit_view_timeline),
    ("34", test_34_full_e2e_preflight_import_restart),
    ("35", test_35_cross_restart_resume_detection),
    ("36", test_36_no_false_positive_cross_restart),
    ("37", test_37_export_has_handover_summary),
    ("38", test_38_two_step_pending_confirm),
    ("39", test_39_revoke_pending),
    ("40", test_40_revoke_confirmed_force),
    ("41", test_41_conflict_detection_and_decisions),
    ("42", test_42_cross_restart_persisted_session),
    ("43", test_43_full_e2e_handoff_confirm_restart_approve),
    ("44", test_44_utf8_mode_encoding_stability),
]
tests_completed = 0
fatal_in_subset = False
try:
    for name, fn in tests:
        try:
            safe_print(f"\n========== Running Test {name} ==========")
            fn(tmpdir)
            tests_completed += 1
        except Exception as e:
            fatal_in_subset = True
            safe_print(f"\n*** Test {name} EXCEPTION: {type(e).__name__}: {safe_truncate(str(e), 300)} ***")
            import traceback
            try:
                tb_str = traceback.format_exc()
                safe_print(safe_truncate(tb_str, 800))
            except Exception:
                pass
    safe_print(f"\n==== SUBSET SUMMARY: {test_regression.PASS} passed, {test_regression.FAIL} failed ({tests_completed}/{len(tests)} tests completed) ====")
    if test_regression.PASS == 0 and test_regression.FAIL == 0:
        safe_print("[CRITICAL INTEGRITY ERROR] 0 passed and 0 failed - tests likely crashed or did not run")
        safe_print("This result is invalid - a normal test run should have many passing assertions")
        shutil.rmtree(tmpdir, ignore_errors=True)
        sys.exit(2)
    if test_regression.FAIL > 0 or fatal_in_subset or tests_completed < len(tests):
        safe_print(f"[NOT PASSED] {test_regression.FAIL} failed assertions, {tests_completed}/{len(tests)} tests completed")
        shutil.rmtree(tmpdir, ignore_errors=True)
        sys.exit(1)
    safe_print("All subset tests passed.")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
