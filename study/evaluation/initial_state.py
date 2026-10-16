"""核对固定镜像的初始源码；记录 Sphinx 官方镜像的安装准备差异。"""


def inspect_initial_state(execute, task: dict) -> dict:
    """execute 返回 (退出码, 文本)，只在新建的任务容器执行只读命令。"""
    def read(command: str) -> str:
        code, output = execute(command)
        assert code == 0 and "QEMU internal" not in output, f"初始状态读取失败：{command}\n{output[:1000]}"
        return output.strip()

    # 在模型请求之前暴露跨架构工具故障，不能把环境异常当作代码问题。
    grep_version = read("grep --version").splitlines()[0]
    base = task["base_commit"]
    head, image_tree, base_tree = read(f"git rev-parse HEAD HEAD^{{tree}} {base}^{{tree}}").splitlines()
    assert not read("git status --porcelain"), "初始工作目录有修改"
    changed = read(f"git diff --name-only {base} HEAD").splitlines()
    if image_tree != base_tree:
        # 官方 Sphinx 镜像锁定旧依赖，并给 pytest 加 -rA；不能要求全树与 base 相等。
        # 不允许这种例外扩大到源码、测试文件或其他仓库。具体 diff 完整留档。
        assert task["repo"] == "sphinx-doc/sphinx"
        assert set(changed) <= {"setup.py", "tox.ini"}, changed
        assert read("git rev-parse HEAD^") == base
        assert read("git log -1 --format=%s") == "SWE-bench"
    else:
        assert not changed
    return {"head": head, "image_tree": image_tree, "base_tree": base_tree, "grep_version": grep_version,
            "preparation_files": changed,
            "preparation_diff": read(f"git diff {base} HEAD"),
            "source_and_tests_match_base": True, "clean": True}
