#!/usr/bin/env python3
"""
Workspace 功能单元测试 — 在开发机（无 NapCat、不运行机器人）上验证实现有效性。

覆盖：
  - Feature 1 工作区快照：树形结构、相对路径、缓存跳过、目录优先排序、大小标注
  - 未来追加：Feature 2 delete_workspace_file / 配额弹性清理协议（待实现后补）

Usage:
    cd /home/windows11/QQBotAgent/QQBot
    python test_workspace.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.workspace_snapshot import (
    fmt_bytes,
    should_skip,
    list_entries,
    dir_size,
    rel_to_root,
    build_tree,
    MAX_TREE_ENTRIES,
    MAX_TREE_DEPTH,
)
from agent.quota_cleanup import (
    CleanupCandidate,
    list_candidates,
    execute_cleanup,
    resolve_targets,
)
from agent.context import _current_user_workspace
from agent.special_session import SpecialSessionManager
from tools.builtin_tools import delete_workspace_file


# ── 输出辅助 ──────────────────────────────────────────────────────

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def header(text):
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}  {text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}\n")


def ok(text):
    print(f"  {Colors.GREEN}✓ PASS{Colors.RESET} — {text}")


def fail(text):
    print(f"  {Colors.RED}✗ FAIL{Colors.RESET} — {text}")


class TmpWorkspace:
    """Context manager 提供一个临时工作区目录，退出时清理。"""

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def cleanup(self):
        self._tmp.cleanup()


# ── 测试用例 ──────────────────────────────────────────────────────

class TestFmtBytes:
    def run(self):
        header("1. fmt_bytes — 字节格式化")
        cases = [
            (0, "0 B"),
            (1023, "1023 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (523000, "510.7 KB"),
            (5 * 1024 * 1024, "5.0 MB"),
            (1500 * 1024 * 1024, "1.46 GB"),
        ]
        for size, expected in cases:
            got = fmt_bytes(size)
            assert got == expected, f"fmt_bytes({size}) = {got!r}, 期望 {expected!r}"
        ok("字节/千字节/兆字节/吉字节边界正确")


class TestShouldSkip:
    def run(self):
        header("2. should_skip — 缓存/隐藏条目过滤")
        for name in [".git", "__pycache__", "node_modules", ".cache",
                     ".pytest_cache", ".mypy_cache", ".ruff_cache",
                     "a.pyc", "b.pyo"]:
            assert should_skip(name), f"应跳过 {name!r}"
        for name in ["normal.py", "Assignment_3.pdf", "code", "uploads",
                     "report.md", "chart.png"]:
            assert not should_skip(name), f"不应跳过 {name!r}"
        ok("缓存目录与 .pyc/.pyo 正确跳过，普通文件保留")


class TestListEntries:
    def run(self):
        header("3. list_entries — 目录优先 + 字母序 + 缓存跳过")
        ws = TmpWorkspace()
        try:
            root = ws.root
            (root / "z_file.txt").write_text("z")
            (root / "a_file.txt").write_text("a")
            (root / "M_dir").mkdir()
            (root / "b_dir").mkdir()
            (root / ".git").mkdir()
            (root / "__pycache__").mkdir()
            (root / "x.pyc").write_text("x")

            names = [e.name for e in list_entries(root)]
            # 目录优先（b_dir, M_dir 按不区分大小写），文件随后（a_file, z_file）
            assert names == ["b_dir", "M_dir", "a_file.txt", "z_file.txt"], \
                f"排序错误: {names}"
            ok("目录优先 + 不区分大小写字母序")
            ok(".git/__pycache__/.pyc 从列表剔除")
        finally:
            ws.cleanup()


class TestDirSize:
    def run(self):
        header("4. dir_size — 递归大小 + 缓存跳过")
        ws = TmpWorkspace()
        try:
            root = ws.root
            (root / "a.txt").write_bytes(b"x" * 100)
            sub = root / "sub"
            sub.mkdir()
            (sub / "b.txt").write_bytes(b"y" * 50)
            (sub / "__pycache__").mkdir()
            (sub / "__pycache__" / "c.pyc").write_bytes(b"z" * 9999)  # 应被跳过

            assert dir_size(root) == 150, f"期望 150, 实际 {dir_size(root)}"
            ok("递归求和 100+50=150，缓存字节不计入")
        finally:
            ws.cleanup()


class TestRelToRoot:
    def run(self):
        header("5. rel_to_root — 绝对路径脱敏")
        assert rel_to_root("/data/root/2578260985/workspace", "/data/root") \
            == "2578260985/workspace"
        assert rel_to_root("/data/root/2578260985/workspace/uploads", "/data/root") \
            == "2578260985/workspace/uploads"
        assert rel_to_root("/data/root", "/data/root") == "."
        ok("隐藏数据根前缀，返回相对路径")


class TestBuildTree:
    def _make_standard_ws(self, root: Path):
        """构造一个标准工作区：含目录、嵌套文件、空目录、缓存。"""
        (root / "uploads").mkdir()
        (root / "uploads" / "a.pdf").write_bytes(b"x" * 523000)
        (root / "uploads" / "b.txt").write_text("hello")
        (root / "uploads" / "__pycache__").mkdir()
        (root / "code").mkdir()
        (root / "code" / "sort.py").write_text("print(1)")
        (root / "output").mkdir()
        (root / "repos").mkdir()
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("g")

    def run(self):
        header("6. build_tree — 树形结构渲染")
        self._test_empty()
        self._test_missing()
        self._test_standard()
        self._test_symlink()
        self._test_entry_cap()
        self._test_depth_cap()

    def _test_empty(self):
        ws = TmpWorkspace()
        try:
            lines = build_tree(ws.root, "w/", indent="")
            assert lines == ["w/", "(空)"], f"空工作区输出异常: {lines}"
            ok("空工作区 → (空)")
        finally:
            ws.cleanup()

    def _test_missing(self):
        ws = TmpWorkspace()
        try:
            missing = ws.root / "nope"
            lines = build_tree(missing, "w/", indent="")
            assert lines[-1] == "(工作区目录不存在)", f"缺失目录输出异常: {lines}"
            ok("目录不存在 → (工作区目录不存在)")
        finally:
            ws.cleanup()

    def _test_standard(self):
        ws = TmpWorkspace()
        try:
            self._make_standard_ws(ws.root)
            lines = build_tree(ws.root, "2578260985/workspace/", indent="")
            expected = [
                "2578260985/workspace/",
                "├── code/ (1 项, 8 B)",
                "│   └── sort.py (8 B)",
                "├── output/ (空)",
                "├── repos/ (空)",
                "└── uploads/ (2 项, 510.7 KB)",
                "    ├── a.pdf (510.7 KB)",
                "    └── b.txt (5 B)",
            ]
            assert lines == expected, f"\n实际:\n{lines}\n期望:\n{expected}"
            ok("目录优先 + 嵌套树 + 大小标注")
            ok(".git / __pycache__ 不出现在树中")
        finally:
            ws.cleanup()

    def _test_symlink(self):
        ws = TmpWorkspace()
        try:
            (ws.root / "real.txt").write_text("hi")
            os.symlink(ws.root / "real.txt", ws.root / "link.txt")
            lines = build_tree(ws.root, "w/", indent="")
            assert any("link.txt -> (符号链接)" in ln for ln in lines), \
                f"符号链接未正确渲染: {lines}"
            ok("符号链接 → (符号链接) 且不递归")
        finally:
            ws.cleanup()

    def _test_entry_cap(self):
        ws = TmpWorkspace()
        try:
            d = ws.root / "many"
            d.mkdir()
            for i in range(MAX_TREE_ENTRIES + 5):
                (d / f"f{i:02d}.txt").write_text("x")
            lines = build_tree(ws.root, "w/", indent="")
            joined = "\n".join(lines)
            assert f"还有 5 项未显示" in joined, f"未触发条目上限: {lines}"
            ok(f"单目录超 {MAX_TREE_ENTRIES} 项 → 截断并提示剩余")
        finally:
            ws.cleanup()

    def _test_depth_cap(self):
        ws = TmpWorkspace()
        try:
            p = ws.root
            for i in range(MAX_TREE_DEPTH + 2):
                p = p / f"d{i}"
                p.mkdir()
            (p / "leaf.txt").write_text("x")
            lines = build_tree(ws.root, "w/", indent="")
            joined = "\n".join(lines)
            assert "深度限制" in joined, f"未触发深度上限: {lines}"
            ok(f"嵌套超 {MAX_TREE_DEPTH} 层 → 深度限制提示")
        finally:
            ws.cleanup()


# ── Feature 2: 文件删除与配额清理 ────────────────────────────────

class TestDeleteWorkspaceFile:
    """delete_workspace_file — 路径校验、隐藏文件、非空目录、文件/空目录删除。"""

    def _with_ws(self, fn):
        ws = TmpWorkspace()
        try:
            token = _current_user_workspace.set(str(ws.root))
            try:
                fn(ws.root)
            finally:
                _current_user_workspace.reset(token)
        finally:
            ws.cleanup()

    def run(self):
        header("7. delete_workspace_file — 删除行为与安全约束")

        def _test_file(root):
            (root / "uploads").mkdir()
            f = root / "uploads" / "a.txt"
            f.write_text("hello")
            r = delete_workspace_file("uploads/a.txt")
            assert "已删除文件" in r and "uploads/a.txt" in r, f"文件删除异常: {r}"
            assert not f.exists(), "文件应被删除"
        self._with_ws(_test_file)
        ok("删除文件成功并汇报释放空间")

        def _test_empty_dir(root):
            (root / "empty").mkdir()
            r = delete_workspace_file("empty")
            assert "已删除空目录" in r, f"空目录删除异常: {r}"
            assert not (root / "empty").exists(), "空目录应被删除"
        self._with_ws(_test_empty_dir)
        ok("删除空目录成功")

        def _test_non_empty_dir(root):
            (root / "repos" / "r").mkdir(parents=True)
            (root / "repos" / "r" / "f.txt").write_text("x")
            r = delete_workspace_file("repos/r")
            assert "非空" in r, f"非空目录应被拒绝: {r}"
            assert (root / "repos" / "r" / "f.txt").exists(), "非空目录内容不应被删除"
        self._with_ws(_test_non_empty_dir)
        ok("非空目录删除被拒绝")

        def _test_hidden(root):
            (root / ".secret").write_text("s")
            r = delete_workspace_file(".secret")
            assert "隐藏" in r, f"隐藏文件应被拒绝: {r}"
            assert (root / ".secret").exists(), "隐藏文件不应被删除"
        self._with_ws(_test_hidden)
        ok("隐藏文件删除被拒绝")

        def _test_traversal(root):
            r = delete_workspace_file("../evil")
            assert "非法" in r or "超出" in r, f"路径穿越应被拒绝: {r}"
        self._with_ws(_test_traversal)
        ok("路径穿越被拒绝")


class TestListCandidates:
    """list_candidates — mtime 排序、缓存/隐藏跳过、仓库整体化。"""

    def run(self):
        header("8. list_candidates — 删除候选枚举")
        ws = TmpWorkspace()
        try:
            root = ws.root
            (root / "uploads").mkdir()
            (root / "repos" / "repoA").mkdir(parents=True)
            (root / ".git").mkdir()

            old = root / "uploads" / "old.txt"
            old.write_text("x" * 100)
            new = root / "uploads" / "new.txt"
            new.write_text("y" * 200)
            (root / "repos" / "repoA" / "f.py").write_text("z" * 50)
            (root / "uploads" / ".hidden").write_text("h" * 9999)
            (root / "uploads" / "__pycache__").mkdir()
            (root / "uploads" / "__pycache__" / "c.pyc").write_text("p")

            # 显式拉开 mtime——部分文件系统（ext2/ext3）时间戳精度为秒级，
            # 连续创建的文件 mtime 会完全相同，排序退化为按路径，测试将不确定。
            now = time.time()
            os.utime(old, (now - 30, now - 30))
            os.utime(new, (now - 20, now - 20))
            os.utime(root / "repos" / "repoA" / "f.py", (now - 10, now - 10))

            cands = list_candidates(root)
            rels = [c.rel_path for c in cands]
            # 隐藏文件、.git、__pycache__ 均不在候选
            assert "uploads/.hidden" not in rels, f"隐藏文件不应在候选: {rels}"
            assert not any(".git" in r for r in rels), f".git 不应在候选: {rels}"
            assert not any("__pycache__" in r for r in rels), f"缓存不应在候选: {rels}"
            # repos/repoA 整体化为一个 repo 候选，其内部文件不单独出现
            assert "repos/repoA" in rels, f"仓库应作为整体候选: {rels}"
            assert "repos/repoA/f.py" not in rels, f"仓库内部文件不应单独列出: {rels}"
            # mtime 升序：old.txt 最早
            assert cands[0].rel_path == "uploads/old.txt", f"最早文件应排第一: {rels}"
            ok("跳过隐藏/缓存，仓库整体化，mtime 升序")
        finally:
            ws.cleanup()


class TestExecuteCleanup:
    """execute_cleanup — 按 mtime 删除至配额内。"""

    def run(self):
        header("9. execute_cleanup — 删除至配额内")
        ws = TmpWorkspace()
        try:
            root = ws.root
            (root / "uploads").mkdir()
            f1 = root / "uploads" / "a.txt"
            f1.write_text("x" * 500)
            f2 = root / "uploads" / "b.txt"
            f2.write_text("y" * 300)
            f3 = root / "uploads" / "c.txt"
            f3.write_text("z" * 200)

            # 同上：显式拉开 mtime，避免秒级精度文件系统上的平局
            now = time.time()
            os.utime(f1, (now - 30, now - 30))
            os.utime(f2, (now - 20, now - 20))
            os.utime(f3, (now - 10, now - 10))

            cands = list_candidates(root)
            # 配额 800 字节，目标 80% = 640：应删除 a(500) 后剩 500 <= 640
            result = execute_cleanup(root, 800, candidates=cands, target_ratio=0.8)
            deleted = [c.rel_path for c in result["deleted"]]
            assert deleted == ["uploads/a.txt"], f"应只删除最早文件: {deleted}"
            assert f1.exists() is False, "a.txt 应被删除"
            assert f2.exists() and f3.exists(), "b/c 应保留"
            assert result["usage_after"] == 500, f"删除后应为 500: {result['usage_after']}"
            ok("按 mtime 删除至 <80% 配额")
        finally:
            ws.cleanup()


class TestResolveTargets:
    """resolve_targets — 清理协议响应的解析。"""

    def _cands(self):
        return [
            CleanupCandidate("uploads/a.png", "/w/uploads/a.png", 1, 1.0, "file"),
            CleanupCandidate("uploads/b.png", "/w/uploads/b.png", 2, 2.0, "file"),
            CleanupCandidate("repos/repoA", "/w/repos/repoA", 3, 3.0, "repo"),
        ]

    def run(self):
        header("10. resolve_targets — 清理响应解析")
        c = self._cands()

        d = resolve_targets("删吧", c)
        assert d.mode == "confirm" and len(d.targets) == 3, f"确认解析异常: {d.mode}"

        d = resolve_targets("不删", c)
        assert d.mode == "skip", f"跳过解析异常: {d.mode}"

        d = resolve_targets("保留 repoA", c)
        assert d.mode == "keep", f"保留解析异常: {d.mode}"
        assert [t.rel_path for t in d.targets] == ["uploads/a.png", "uploads/b.png"], \
            f"保留 repoA 应删除其余: {[t.rel_path for t in d.targets]}"

        d = resolve_targets("删 uploads/b.png", c)
        assert d.mode == "explicit" and [t.rel_path for t in d.targets] == ["uploads/b.png"], \
            f"显式删除解析异常: {d.mode} {[t.rel_path for t in d.targets]}"

        d = resolve_targets("删 1", c)
        assert d.mode == "explicit" and [t.rel_path for t in d.targets] == ["uploads/a.png"], \
            f"编号删除解析异常: {d.mode}"

        d = resolve_targets("你好", c)
        assert d.mode == "unclear", f"模糊响应应 unclear: {d.mode}"

        ok("确认/跳过/保留/显式/模糊 分支正确")


# ── Feature 3: Session File Provenance ───────────────────────────

class TestSessionFileProvenance:
    """add_file 幂等、delete 删文件 + 保留仓库 + 返回摘要、越界/缺失跳过。"""

    def _new_mgr(self, root):
        mgr = SpecialSessionManager(user_data_root=root)
        mgr.create("10001", "demo")
        mgr.switch_to("10001", "demo")
        return mgr

    def run(self):
        header("11. Session File Provenance — 溯源记录与删除")
        self._test_add_file_idempotent()
        self._test_delete_removes_files_keeps_repos()
        self._test_delete_skips_missing_and_traversal()

    def _test_add_file_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._new_mgr(tmp)
            mgr.add_file("10001", "demo", "uploads/a.pdf")
            mgr.add_file("10001", "demo", "uploads/a.pdf")
            files = mgr._load_index("10001")["sessions"][0]["metadata"]["files"]
            assert files == ["uploads/a.pdf"], f"幂等失败: {files}"
            mgr.add_file("10001", "nope", "x.txt")  # 不存在会话，静默 no-op
            ok("add_file 幂等 + 会话不存在静默")

    def _test_delete_removes_files_keeps_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._new_mgr(tmp)
            ws = os.path.join(tmp, "10001", "workspace")
            up = os.path.join(ws, "uploads", "a.pdf")
            os.makedirs(os.path.dirname(up), exist_ok=True)
            with open(up, "w") as f:
                f.write("x" * 100)
            repo_f = os.path.join(ws, "repos", "demo-repo", "README.md")
            os.makedirs(os.path.dirname(repo_f), exist_ok=True)
            with open(repo_f, "w") as f:
                f.write("r")
            mgr.add_file("10001", "demo", "uploads/a.pdf")
            mgr.add_file("10001", "demo", "repos/demo-repo")

            result = mgr.delete("10001", "demo")
            assert not os.path.exists(up), "会话级文件应被删除"
            assert os.path.exists(repo_f), "仓库应保留"
            assert result["deleted"] == ["uploads/a.pdf"], result
            assert result["kept_repos"] == ["repos/demo-repo"], result
            assert result["freed_bytes"] == 100, result
            assert mgr._load_index("10001")["sessions"] == [], "index 应清空"
            ok("delete 删文件 + 保留仓库 + 返回摘要")

    def _test_delete_skips_missing_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._new_mgr(tmp)
            mgr.add_file("10001", "demo", "uploads/gone.pdf")  # 不存在
            mgr.add_file("10001", "demo", "../evil.txt")        # 越界
            result = mgr.delete("10001", "demo")
            assert result["deleted"] == [] and result["freed_bytes"] == 0, result
            ok("delete 跳过缺失/越界记录不崩溃")


# ── 入口 ──────────────────────────────────────────────────────────

def main():
    suites = [
        TestFmtBytes(),
        TestShouldSkip(),
        TestListEntries(),
        TestDirSize(),
        TestRelToRoot(),
        TestBuildTree(),
        TestDeleteWorkspaceFile(),
        TestListCandidates(),
        TestExecuteCleanup(),
        TestResolveTargets(),
        TestSessionFileProvenance(),
    ]
    passed = 0
    failed = 0
    for s in suites:
        try:
            s.run()
            passed += 1
        except AssertionError as e:
            failed += 1
            fail(str(e))
        except Exception as e:  # noqa: BLE001 — 报告任意意外错误
            failed += 1
            fail(f"{type(e).__name__}: {e}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Workspace 测试完成：{passed} 套通过 / {failed} 套失败{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 60}{Colors.RESET}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
