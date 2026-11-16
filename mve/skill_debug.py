#!/usr/bin/env python
"""单技能调试：把技能库里的一条技能，当成**独立自动化程序**跑一遍。

为什么需要它（2026-11-13 用户口径）：「voyager 在这个 task 写了这段代码，
它跑这段代码，能够做到我想要的流程（我触发就是点 start）——它会跑每一段：
比如 ①查书 ②出题」。也就是说技能不是「一段能被调用的函数」，而是**一段跑
起来就把整条流程走完、最后交出 RESULT 的程序**。判它合不合格不能看代码长相，
只能**真的跑一次**：零上下文、独立命名空间、单次执行。

用法：
  python skill_debug.py --list --scene N
  python skill_debug.py --skill learn_c45d6a58::milestone --scene N
  python skill_debug.py --code-file /tmp/x.py --scene N [--db /path/x.duckdb]

输出：每次数据源调用一行（序号 / 函数 / 参数摘要 / 返回形状），最后给
RESULT 摘要与判定。判定为 ✗ 时退出码 1（可当 Phase2 的入库门用）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import local_agent as _la


# ───────────────────────── 场景 ─────────────────────────
def _scene_dir(scene: str) -> str:
    import os
    for p in (f"/root/mve/scene_data/{scene}", f"/root/mve/mve/scene_data/{scene}"):
        if os.path.isdir(p):
            return p
    return ""


def _db_path(scene: str) -> str:
    try:
        import scene_profile
        prof = scene_profile.profile(scene) or {}
        p = str(prof.get("db_path") or "")
        if p:
            return p
    except Exception:
        pass
    import os
    d = _scene_dir(scene)
    if d and os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.endswith(".duckdb"):
                return os.path.join(d, f)
    return ""


# ───────────────────────── 技能库 ─────────────────────────
def _skills(scene: str) -> list[dict[str, Any]]:
    import skill_store
    out = []
    for s in (skill_store.all_skills() or []):
        s = s or {}
        if scene and str(s.get("source_scene") or "") != scene:
            continue
        out.append(s)
    return out


def _get_skill(key: str) -> dict[str, Any] | None:
    import skill_store
    for s in (skill_store.all_skills() or []):
        s = s or {}
        if str(s.get("key") or "") == key:
            return s
    # 允许用短名
    for s in (skill_store.all_skills() or []):
        s = s or {}
        if str(s.get("name") or "") == key or str(s.get("key") or "").startswith(key):
            return s
    return None


# ───────────────────────── 执行 ─────────────────────────
_TBL = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", re.I)


def _brief(v: Any, n: int = 90) -> str:
    if isinstance(v, str):
        s = " ".join(v.split())
        return s if len(s) <= n else s[:n] + "…"
    try:
        s = json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    return s if len(s) <= n else s[:n] + "…"


def _shape(ret: Any) -> str:
    if isinstance(ret, dict):
        rows = ret.get("rows")
        cols = ret.get("columns")
        if isinstance(rows, list):
            c = len(cols) if isinstance(cols, list) else (
                len(rows[0]) if rows and isinstance(rows[0], (list, tuple)) else 0)
            return f"{len(rows)} 行" + (f" × {c} 列" if c else "")
        return f"dict({', '.join(list(ret)[:6])})"
    if isinstance(ret, list):
        return f"list[{len(ret)}]"
    return type(ret).__name__


def _stages(sql: str) -> str:
    t = [x.lower() for x in _TBL.findall(sql or "")]
    return ("表:" + "/".join(dict.fromkeys(t))) if t else ""


def _say(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, flush=True)


def run_code(code: str, scene: str, *, db_path: str = "", timeout: float = 90.0,
             trace: list[dict[str, Any]] | None = None, quiet: bool = False,
             scene_dir: str = "") -> dict[str, Any]:
    """跑一段技能代码 —— 用**将来调用它的那个执行域**（原语 + 已学技能）。

    返回 {ok, RESULT, calls, error}。`quiet=True` 时不打印（给入库门用）。
    """
    sd = scene_dir or _scene_dir(scene)
    db = db_path or _db_path(scene)
    ns = _la._ns_base(sd, scene, tracer=[], loop=None, db_path=db,
                      question="", submit_fn=None)
    # 已学技能也是执行域的一部分（原版 index.js:256 programs + code 同作用域）：
    # 程序单跑时能调到的兄弟技能，和它将来被调用时一致 —— 否则「单技能调试」
    # 会误判组合型程序跑不通。
    try:
        import skill_store as _SS
        _all = _SS.all_skills() or []
        _sk = [s for s in _all
               if str((s or {}).get("source_scene") or "default")
               in {str(scene or ""), "default", ""}
               and "RESULT" in str((s or {}).get("code") or "")]
        ns.update(_la._build_skill_fns(_sk, sd, scene, [], loop=None,
                                       evidence=[], question="", db_path=db))
    except Exception:
        pass
    calls = trace if trace is not None else []
    _seq = {"i": 0}

    def _wrap(name: str, fn: Any) -> Any:
        def _w(*a: Any, **k: Any) -> Any:
            _seq["i"] += 1
            i = _seq["i"]
            arg = _brief(a[0] if a else (k or ""), 120)
            extra = _stages(str(a[0])) if name == "query_sql" and a else ""
            try:
                ret = fn(*a, **k)
            except Exception as e:                     # noqa: BLE001
                calls.append({"i": i, "fn": name, "arg": arg, "ret": "",
                              "err": f"{type(e).__name__}: {e}"})
                _say(f"  [{i}] {name}  {arg}\n       ✗ {type(e).__name__}: {e}",
                     quiet)
                raise
            shape = _shape(ret)
            calls.append({"i": i, "fn": name, "arg": arg, "ret": shape,
                          "tables": extra})
            _say(f"  [{i}] {name}  {arg}"
                 + (f"\n       → {shape}" + (f"（{extra}）" if extra else "")
                    if shape else ""), quiet)
            return ret
        return _w

    for _n in list(ns):
        if callable(ns[_n]) and not str(_n).startswith("_"):
            ns[_n] = _wrap(_n, ns[_n])

    r = _la._run_restricted(code, ns, timeout=timeout)
    return {"ok": bool(r.get("ok")), "error": str(r.get("error") or ""),
            "RESULT": ns.get("RESULT"), "calls": calls, "ns": ns,
            # 原文一起带回：入库门要判「RESULT 是算出来的还是抄进来的」
            "code": str(code or "")}


SRC_FNS = ("query_sql", "read_file", "read_json", "list_files",
           "web_search", "web_fetch")

# 占位符：RESULT 里写着「PENDING / TODO / 待补」这类东西，等于流程某一步没真做
# （2026-11-13 实测：程序能单跑、RESULT 有 11 个键，但 derived_answer 是
#  PENDING_SPECIFIC_IMPLEMENTATION、verification_passed=false —— ②那步是空的）。
_PLACEHOLDER = re.compile(
    r"(PENDING|TODO|TBD|FIXME|NOT_?IMPLEMENTED|PLACEHOLDER|占位|待补|未实现)",
    re.I)


def placeholders(val: Any, depth: int = 0) -> list[str]:
    """找出 RESULT 里所有占位符（递归到第 3 层，够覆盖常见形态）。"""
    hits: list[str] = []
    if depth > 3:
        return hits
    if isinstance(val, str):
        if _PLACEHOLDER.search(val):
            hits.append(val[:120])
    elif isinstance(val, dict):
        for k, v in val.items():
            if isinstance(k, str) and _PLACEHOLDER.search(k):
                hits.append(str(k)[:120])
            hits += placeholders(v, depth + 1)
    elif isinstance(val, (list, tuple)):
        for x in val[:30]:
            hits += placeholders(x, depth + 1)
    return hits[:5]


def _leaves(val: Any, depth: int = 0) -> list[Any]:
    """RESULT 里真正承载结论的那些值（递归到第 3 层）。"""
    if depth > 3:
        return []
    if isinstance(val, dict):
        out: list[Any] = []
        for v in val.values():
            out += _leaves(v, depth + 1)
        return out
    if isinstance(val, (list, tuple)):
        out = []
        for x in list(val)[:30]:
            out += _leaves(x, depth + 1)
        return out
    return [val]


def _literal_in(val: Any, code: str) -> bool:
    """这个值是不是**原样写在代码里**的一个字面量。

    数字按独立 token 比（`36` 不会命中 `136` 或 `36.5`），字符串按整段比。
    """
    if isinstance(val, bool) or val is None:
        return True                     # 布尔/None 不承载"算出来的结论"
    if isinstance(val, (int, float)):
        try:
            s = ("%g" % float(val))
        except Exception:
            return True
        if s in ("inf", "-inf", "nan"):
            return True
        return re.search(r"(?<![\w.])" + re.escape(s) + r"(?![\w.])",
                         code) is not None
    if isinstance(val, str):
        s = val.strip()
        if len(s) < 2:                  # 单字符不算结论（多半是分隔符）
            return True
        return s in code
    return True


def derived(R: Any, code: str) -> bool:
    """RESULT 里有没有**至少一个不是抄进来**的值 —— 即流程②真的走了一步。

    为什么不能再按"调没调数据源"判（2026-11-14 用户口径：Phase2 不写代码、
    不积累技能，之后遇到问题无法复用）：把"结论必须来自数据源"当唯一证据，
    等于把**推导型**任务全挡在门外 —— 它们不查库，真值只能算出来。判据要
    落在"产出是不是真做出来的"，与学科无关：

      · 抄答案：`RESULT = {"overall": 36}`、`{"answer": "30°"}` —— 值原样
        出现在代码里，流程没走，拒；
      · 真算：`overall = x * 12` 得到 36、`str(a) + "°"` 拼出 "30°" ——
        值在代码里找不到，说明至少被算过一次，收。

    两种取材方式都覆盖：真取数（值来自数据源，也不在代码里）与真推导。
    """
    return any(not _literal_in(v, str(code or "")) for v in _leaves(R))


def verdict(chk: dict[str, Any], *, allow_stub: bool = False) -> tuple[bool, str]:
    """入库门的判据（与 report 同源）：能不能当自动化程序用。

    `allow_stub=True`：草稿档 —— 允许占位符（它是"半成品起点"，下一轮在这份上
    把没做的那一步补完），但仍然要求**跑得通 + 有 RESULT + 有一步真做过**。

    「真做过」有两档（`derived()`）：真调了数据源，或 RESULT 里有算出来的值。
    只要一档成立即可 —— 数据源只是**外部材料的一种**，不是全部。
    """
    if not chk.get("ok"):
        return False, "单技能调试跑不通：" + str(chk.get("error") or "")[:300]
    R = chk.get("RESULT")
    if R is None or (isinstance(R, dict) and not R):
        return False, ("单技能调试跑完了，但没有 RESULT —— 流程没有产出"
                       "（只是一段探查/取数代码，不是自动化）")
    _has_src = any(str((c or {}).get("fn")) in SRC_FNS
                   for c in (chk.get("calls") or []))
    if not (_has_src or derived(R, str(chk.get("code") or ""))):
        return False, ("单技能调试：RESULT 里的值全是代码里原样写的字面量 —— "
                       "它把结论抄了一遍，没有一步是真取/真算出来的")
    if not allow_stub:
        _ph = placeholders(R)
        if _ph:
            return False, ("单技能调试：RESULT 里还有占位符（"
                           + _ph[0] + "）—— 说明流程里有一"
                           "步没真做，只是写了个「待补」")
    return True, ""


def report(title: str, code: str, res: dict[str, Any]) -> bool:
    print(f"\n=== {title} ===", flush=True)
    print(f"码 {len(code)} 字｜含 RESULT 赋值：{'RESULT' in code}", flush=True)
    print("--- 执行（零上下文，独立命名空间）---", flush=True)
    if not res["calls"]:
        print("  （没有任何数据源调用）", flush=True)
    if not res["ok"]:
        print(f"\n判定：✗ 跑不通 —— {res['error'][:400]}", flush=True)
        return False
    R = res.get("RESULT")
    print("\n--- 交付物 ---", flush=True)
    if R is None:
        print("  RESULT：（无）—— 这段代码跑完什么都没交", flush=True)
        print("\n判定：✗ 不能当自动化用：流程走完没有产出（无 RESULT）", flush=True)
        return False
    if isinstance(R, dict):
        for k, v in list(R.items())[:12]:
            print(f"  {k}: {_brief(v, 160)}", flush=True)
        n = len(R)
    else:
        print(f"  {_brief(R, 400)}", flush=True)
        n = 1
    has_src = any(c["fn"] in SRC_FNS for c in res["calls"])
    has_calc = derived(R, str(res.get("code") or code))
    _ph = placeholders(R)
    if _ph:
        print("  ⚠ 占位符：" + _ph[0], flush=True)
    print(f"\n判定：{'✓' if (n and (has_src or has_calc) and not _ph) else '✗'} "
          f"调用 {len(res['calls'])} 次外部函数，RESULT {n} 个键"
          + ("" if has_src else
             ("（没查数据源，但 RESULT 是算出来的）" if has_calc else
              "（RESULT 全是抄进代码的字面量，流程没走）"))
          + ("（流程里有一步只是占位符，没真做）" if _ph else ""), flush=True)
    return bool(n and (has_src or has_calc) and not _ph)


# ───────────────────────── CLI ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="N")
    ap.add_argument("--skill", default="")
    ap.add_argument("--code-file", default="")
    ap.add_argument("--db", default="")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--json", action="store_true", help="结果以 JSON 输出（给门用）")
    a = ap.parse_args()

    if a.list:
        rows = _skills(a.scene)
        print(f"场景 {a.scene}：{len(rows)} 条技能")
        for s in rows:
            print(f"- {s.get('key')} | verified={s.get('verified')} "
                  f"| {len(str(s.get('code') or ''))} 字 "
                  f"| {'有' if 'RESULT' in str(s.get('code') or '') else '无'} RESULT "
                  f"| {str(s.get('text') or '')[:60]}")
        return 0

    code = ""
    title = ""
    if a.code_file:
        code = open(a.code_file, encoding="utf-8").read()
        title = a.code_file
    elif a.skill:
        s = _get_skill(a.skill)
        if not s:
            print(f"找不到技能 {a.skill}", file=sys.stderr)
            return 2
        code = str(s.get("code") or "")
        title = f"{s.get('key')}（verified={s.get('verified')}）"
    else:
        ap.print_help()
        return 2

    res = run_code(code, a.scene, db_path=a.db, timeout=a.timeout)
    ok = report(title, code, res)
    if a.json:
        print("\n" + json.dumps({
            "ok": ok, "run_ok": res["ok"], "error": res["error"],
            "calls": [{k: v for k, v in c.items()} for c in res["calls"]],
            "result_keys": (list(res["RESULT"]) if isinstance(res["RESULT"], dict)
                            else ([] if res["RESULT"] is None else ["<value>"])),
        }, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
