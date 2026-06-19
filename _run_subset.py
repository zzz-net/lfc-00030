import tempfile, shutil, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_regression import (
    PASS, FAIL,
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
)

tmpdir = tempfile.mkdtemp(prefix="release_cli_subset_")
print(f"Subset test workspace: {tmpdir}")
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
]
try:
    for name, fn in tests:
        try:
            fn(tmpdir)
        except Exception as e:
            print(f"\n*** Test {name} EXCEPTION: {type(e).__name__}: {e} ***")
            import traceback
            traceback.print_exc()
    print(f"\n==== SUBSET SUMMARY: {PASS} passed, {FAIL} failed ====")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
