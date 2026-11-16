#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_code_ref —— 场景 S（MVE 学 MVE）执行门的**机检判据**。

执行门口径（用户 2026-11-17 拍板）
--------------------------------
门不是预写的针对性测试，而是：学生带**全权限**在真仓库探索（读文件 / grep /
python 真跑），**自己出题**；交答案时必须附**可机检的证据**（file:line 引用 +
可选真跑代码），本模块就是那个证据核验器。

判据哲学（与冻结版 DuckDB 判据同构，论文「判据通用性」的证据）：
  ground truth = AST / grep / 解释器真跑，**不过 LLM** ——
  材料面（P/N 场景）的真值是 SQL 取到数，代码面（S 场景）的真值是
  「引用的行真实存在且内容匹配」+「贴出的代码真算出声称的结果」。
  学生可以自己出题、自己作答，但证据必须过机检 —— 助产士同构：
  缺口闭合由可核对的承诺证明，不由「答得好不好」判定。

三种检查
--------
  verify_ref(r)     file:line 真实存在 + 行内容匹配（归一空白，防 OCR 式背写）
  verify_run(r)     python -c 真跑：退出码 0 且 stdout 含声称的输出片段
  verify_claim(c)   组合：{"refs": [...], "run": {...}} 全部通过才算闭合

安全口径：verify_run 不经过 shell（参数列表直传），cwd 限定本仓库，带超时；
执行门本身是全权限学生环境，机检只是**核证据**，不限制学生探索本身。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE


def _repo_root(scene: str = "S") -> Path:
    """学习材料仓库根：场景声明优先（S 的 data_source），兜底本模块所在目录。

    data_source 可以指 GitHub 完整仓库克隆（如 /root/mve/scene_data/S/repo，
    模块在其 mve/ 子目录），也可以直接指模块目录。
    """
    try:
        import scenes as _s
        ds = str((_s.get_scene(str(scene or "S")) or {}).get("data_source") or "").strip()
        if ds:
            p = Path(ds)
            if p.exists():
                sub = p / "mve"
                if (sub / "skill_store.py").exists():
                    return sub          # GitHub 完整仓库：模块在 mve/ 子目录
                if (p / "skill_store.py").exists():
                    return p
    except Exception:
        pass
    return HERE


REPO = _repo_root()


# ---------------------------------------------------------------- 工具
def _flat(s: str) -> str:
    """归一空白：全角空格/制表/换行都压成一个空格再全去掉 —— 只比内容不比排版。"""
    return re.sub(r"\s+", "", str(s or ""))


def _safe_path(fname: str) -> Path | None:
    """只允许本仓库内的相对路径；拒绝绝对路径与 .. 逃逸。"""
    f = str(fname or "").strip()
    if not f or f.startswith("/") or ".." in Path(f).parts:
        return None
    p = REPO / f
    try:
        p.resolve().relative_to(REPO)
    except ValueError:
        return None
    return p if p.exists() else None


# ---------------------------------------------------------------- 1) 行引用
def verify_ref(r: dict) -> dict:
    """核一条 file:line 证据。

    r = {"file": "skill_store.py", "line": 123,
         "expect": "def add(",          # 可选：该行归一后必须包含的片段
         "context": 0}                   # 可选：允许匹配向下扩 N 行（默认 0）
    返回 {"ok": bool, "file":…, "line":…, "reason": str, "found": 归一后的实际内容}
    """
    out = {"file": r.get("file"), "line": r.get("line"), "ok": False, "reason": ""}
    p = _safe_path(str(r.get("file") or ""))
    if p is None:
        out["reason"] = "文件不存在或路径越界（只认仓库内相对路径）"
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        out["reason"] = f"读文件失败：{type(e).__name__}: {e}"
        return out
    try:
        ln = int(r.get("line") or 0)
    except (TypeError, ValueError):
        ln = 0
    if ln < 1 or ln > len(lines):
        out["reason"] = f"行号越界：file 有 {len(lines)} 行，引用在第 {ln} 行"
        return out
    ctx = max(0, min(int(r.get("context") or 0), 5))
    window = _flat("".join(lines[ln - 1: ln + ctx]))
    out["found"] = window[:200]
    expect = str(r.get("expect") or "").strip()
    if not expect:
        out["ok"] = True            # 只断言「行真实存在」也算一条弱证据
        return out
    if _flat(expect) in window:
        out["ok"] = True
        return out
    out["reason"] = (f"行内容不匹配：期望含 {_flat(expect)[:80]}，"
                     f"实际是 {window[:120]!r}")
    return out


# ---------------------------------------------------------------- 2) 真跑
def verify_run(r: dict, *, timeout: int = 60) -> dict:
    """核一段真跑证据：python -c <code>，退出码 0 且 stdout 含 expect 片段。

    r = {"code": "import skill_store; ...", "expect": "…"}   # expect 可选
    """
    code = str(r.get("code") or "").strip()
    out = {"ok": False, "reason": "", "stdout": "", "exit": None}
    if not code:
        out["reason"] = "没有可执行的验证代码"
        return out
    try:
        ast.parse(code)             # 语法先过一遍，别把 traceback 当判据输出
    except SyntaxError as e:
        out["reason"] = f"验证代码本身有语法错误：line {e.lineno}: {e.msg}"
        return out
    try:
        cp = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        out["reason"] = f"真跑超时（>{timeout}s）"
        return out
    except Exception as e:
        out["reason"] = f"真跑异常：{type(e).__name__}: {e}"
        return out
    out["exit"] = cp.returncode
    out["stdout"] = (cp.stdout or "")[-2000:]
    if cp.returncode != 0:
        out["reason"] = f"退出码 {cp.returncode}，stderr 尾部：{(cp.stderr or '')[-300:]}"
        return out
    expect = str(r.get("expect") or "").strip()
    if expect and _flat(expect) not in _flat(cp.stdout):
        out["reason"] = f"输出不含期望片段：期望 {_flat(expect)[:80]}"
        return out
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 3) 组合
def verify_claim(c: dict) -> dict:
    """核一组证据（全部通过才算闭合）。

    c = {"refs": [ref, ...], "run": run或None}
    返回 {"ok": bool, "checks": [各检查结果], "reason": 首个失败原因}
    """
    checks: list[dict] = []
    refs = c.get("refs") or []
    if not isinstance(refs, list):
        refs = [refs]
    if not refs and not c.get("run"):
        return {"ok": False, "checks": [], "reason": "空证据：至少要有一条 file:line 引用或一段真跑"}
    ok = True
    for r in refs:
        res = verify_ref(r if isinstance(r, dict) else {})
        checks.append({"kind": "ref", **res})
        ok = ok and res["ok"]
    if c.get("run"):
        res = verify_run(c["run"] if isinstance(c["run"], dict) else {})
        checks.append({"kind": "run", **res})
        ok = ok and res["ok"]
    first_fail = next((x["reason"] for x in checks if not x.get("ok")), "")
    return {"ok": ok, "checks": checks, "reason": first_fail}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="执行门机检：file:line + 真跑")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        # 三条自检：真引用 / 假行号 / 真跑
        t1 = verify_ref({"file": "skill_store.py", "line": 1, "expect": '"""'})
        t2 = verify_ref({"file": "skill_store.py", "line": 999999})
        t3 = verify_run({"code": "import sys; print('run-ok')",
                         "expect": "run-ok"})
        print("ref-real:", t1["ok"], "| ref-fake:", (not t2["ok"]),
              "| run:", t3["ok"], t3.get("stdout", "").strip()[:20])
