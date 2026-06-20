"""稳定复现 & 验证：python -X utf8 下子进程 audit_view 的编码链路
不依赖 PYTHONIOENCODING，只靠 release_cli.py main() 中主动设置的 sys.stdout.reconfigure(encoding="utf-8")
"""
import tempfile, os, sys, json, subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(SCRIPT_DIR, "release_cli.py")
RULES = os.path.join(SCRIPT_DIR, "rules.yaml")
SAMPLE = os.path.join(SCRIPT_DIR, "sample_manifest.json")

def safe_print(s):
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
    except Exception:
        safe = s.encode("ascii", errors="replace").decode("ascii", errors="replace")
        sys.stdout.write(safe + "\n")
        sys.stdout.flush()

def run(args, state_path, utf8_mode=False):
    cmd = [sys.executable]
    if utf8_mode:
        cmd += ["-X", "utf8"]
    cmd += [CLI, "--rules", RULES, "--state", state_path] + args
    res = subprocess.run(cmd, capture_output=True)
    res.stdout_text = res.stdout.decode("utf-8", errors="replace")
    res.stderr_text = res.stderr.decode("utf-8", errors="replace")
    return res

def main():
    safe_print("=" * 70)
    safe_print("UTF-8 审计输出链路 — 稳定复现 & 验证")
    safe_print("=" * 70)

    tmpdir = tempfile.mkdtemp(prefix="repro_utf8_")
    safe_print(f"Workspace: {tmpdir}\n")

    # ====== 构造测试数据 ======
    safe_print("[Step 1] 构造测试数据：A 机 import→draft→amend→confirm→export_package ...")
    sp_a = os.path.join(tmpdir, "state_a.json")
    run(["import", SAMPLE], sp_a)
    run(["draft"], sp_a)
    run(["amend", "CHG-003", "--field", "owner=测试人员张三"], sp_a)
    run(["amend", "CHG-005", "--field", "risk_level=critical"], sp_a)
    for sec in ["overview", "changes", "rollout", "migration", "known_issues"]:
        run(["confirm", sec], sp_a)
    pkg = os.path.join(tmpdir, "pkg.json")
    run(["export_package", "-o", pkg, "--operator", "源机负责人李四"], sp_a)
    safe_print("  构造完成\n")

    # ====== B 机：import + takeover ======
    safe_print("[Step 2] B 机：import→draft→import_package takeover ...")
    sp_b = os.path.join(tmpdir, "state_b.json")
    run(["import", SAMPLE], sp_b, utf8_mode=True)
    run(["draft"], sp_b, utf8_mode=True)
    run(["import_package", pkg, "--mode", "takeover", "--operator", "接管人王五"], sp_b, utf8_mode=True)
    safe_print("  对账完成\n")

    # ====== 关键验证 1：python -X utf8 audit_view（pending 状态）======
    safe_print("[验证 1] python -X utf8 audit_view (pending) ...")
    r = run(["audit_view"], sp_b, utf8_mode=True)
    safe_print(f"  exit={r.returncode}, stdout={len(r.stdout_text)} bytes")
    assert r.returncode == 0, "exit 必须为 0"
    assert len(r.stdout_text) > 50, "stdout 不能是空或极短"
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in r.stdout_text)
    has_pending = "[PENDING]" in r.stdout_text
    safe_print(f"  中文字符: {has_zh}")
    safe_print(f"  [PENDING] 标签: {has_pending}")
    assert has_zh and has_pending, "中文审计内容和 [PENDING] 标签必须可见"
    safe_print("  ✅ PENDING 审计输出正常\n")

    # ====== 关键验证 2：python -X utf8 takeover_detail ======
    safe_print("[验证 2] python -X utf8 takeover_detail ...")
    r = run(["takeover_detail"], sp_b, utf8_mode=True)
    safe_print(f"  exit={r.returncode}, stdout={len(r.stdout_text)} bytes")
    assert r.returncode == 0, "exit 必须为 0"
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in r.stdout_text)
    has_header = "TAKEOVER DETAIL:" in r.stdout_text
    safe_print(f"  中文字符: {has_zh}")
    safe_print(f"  TAKEOVER DETAIL 标题: {has_header}")
    assert has_zh and has_header, "详情必须含中文和标题"
    safe_print("  ✅ 接管详情输出正常\n")

    # ====== 确认接管 + 模拟跨重启 ======
    safe_print("[Step 3] takeover_confirm → 做后续工作 → 模拟重启（改 session_pid）...")
    run(["takeover_confirm", "--operator", "接管人王五"], sp_b, utf8_mode=True)
    run(["confirm", "rollout"], sp_b, utf8_mode=True)
    run(["confirm", "migration"], sp_b, utf8_mode=True)
    with open(sp_b, "r", encoding="utf-8") as f:
        s = json.load(f)
    tid = s["takeover_history"][0]["takeover_id"]
    s["confirmed_takeover_sessions"][tid]["session_pid"] = 99999
    with open(sp_b, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    safe_print("  session_pid = 99999（模拟重启）\n")

    # ====== 关键验证 3：python -X utf8 audit_view（跨重启 [RESUMED]）======
    safe_print("[验证 3] python -X utf8 audit_view (跨重启续做) ...")
    r = run(["audit_view"], sp_b, utf8_mode=True)
    safe_print(f"  exit={r.returncode}, stdout={len(r.stdout_text)} bytes")
    assert r.returncode == 0, "exit 必须为 0"
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in r.stdout_text)
    has_resumed = "[RESUMED]" in r.stdout_text
    has_cross = "Cross-restart:  YES" in r.stdout_text
    has_takeover = "[TAKEOVER]" in r.stdout_text
    safe_print(f"  中文字符: {has_zh}")
    safe_print(f"  [RESUMED] 标签: {has_resumed}")
    safe_print(f"  Cross-restart: YES: {has_cross}")
    safe_print(f"  [TAKEOVER] 标签: {has_takeover}")
    assert has_zh and has_resumed and has_cross and has_takeover, "跨重启续做标记必须全部可见"
    safe_print("  ✅ 跨重启 [RESUMED] 审计输出正常\n")

    # ====== 关键验证 4：python -X utf8 takeover_revoke + audit_view([REVOKED]) ======
    safe_print("[验证 4] python -X utf8 takeover_revoke --force → audit_view([REVOKED]) ...")
    r = run(["takeover_revoke", "--operator", "撤销人赵六",
             "--reason", "中文撤销原因测试", "--force", "--takeover-id", tid],
            sp_b, utf8_mode=True)
    safe_print(f"  revoke exit={r.returncode}, stdout={len(r.stdout_text)} bytes")
    assert r.returncode == 0, "revoke exit 必须为 0"
    has_zh_rev = any("\u4e00" <= c <= "\u9fff" for c in r.stdout_text)
    has_rev_msg = "revoked successfully" in r.stdout_text
    safe_print(f"  revoke 输出含中文: {has_zh_rev}")
    safe_print(f"  revoked successfully: {has_rev_msg}")

    r2 = run(["audit_view"], sp_b, utf8_mode=True)
    has_revoked = "[REVOKED]" in r2.stdout_text
    has_zh2 = any("\u4e00" <= c <= "\u9fff" for c in r2.stdout_text)
    safe_print(f"  audit_view 含 [REVOKED]: {has_revoked}")
    safe_print(f"  audit_view 含中文: {has_zh2}")
    assert has_zh_rev and has_rev_msg and has_revoked and has_zh2, "撤销链路必须正常"
    safe_print("  ✅ 撤销 [REVOKED] 审计输出正常\n")

    # ====== 关键验证 5：无 -X utf8 模式下也能正常工作（兼容性） ======
    safe_print("[验证 5] 普通 python（无 -X utf8）调用 audit_view（兼容性） ...")
    r = run(["audit_view"], sp_b, utf8_mode=False)
    safe_print(f"  exit={r.returncode}, stdout={len(r.stdout_text)} bytes")
    assert r.returncode == 0, "exit 必须为 0"
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in r.stdout_text)
    has_revoked = "[REVOKED]" in r.stdout_text
    safe_print(f"  中文字符: {has_zh}")
    safe_print(f"  [REVOKED] 标签: {has_revoked}")
    assert has_zh and has_revoked, "普通模式下也必须正常"
    safe_print("  ✅ 普通 python 模式兼容正常\n")

    safe_print("=" * 70)
    safe_print("全部 5 项验证通过 ✅")
    safe_print("  - 根因：release_cli.py main() 主动 sys.stdout.reconfigure(encoding='utf-8')")
    safe_print("  - 无 PYTHONIOENCODING 环境变量注入")
    safe_print("  - 无测试辅助层兜底")
    safe_print("  - 父子进程 I/O 编码边界自洽")
    safe_print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
