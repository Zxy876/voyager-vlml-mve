#!/usr/bin/env python3
"""会话/组合层的**真空防线**：假值不许入库 —— 回归测试。

跑法：python mve/test_session_vacuous_guard.py（本地可跑，不依赖 duckdb / LLM）。

## 事故（2026-11-09 服务器实测，清场后重学场景 L 时抓到的）

L 场景的里程碑「八月份（month = 2026-08）在 up_工资明细 表中有多少条工资记录？」
—— 但 `up_工资明细` 表**不在 L 场景的库里**（那是 B 场景的数据源）。
执行层查表失败 → 里程碑退化成了「目标表存在性校验」，维度派生成
「表存不存在」「有哪些表」，整体取数产出：

    SELECT 0 AS record_count

这条**假值**被固化进了会话：

    learn_7f3b9415::milestone
      blueprint.rollout_sql = "SELECT 0 AS record_count"
      session_code 里有  r = query_sql(sql='SELECT 0 AS record_count')

## 两处根因（都在 learn_orchestrate.learn_milestone_composite）

① **组合层的真执行检查太弱**（第 ③ 道闸）：
       real = any(v is not None for row in rows[:3] ...)
   `SELECT 0` 返回一行 `[0]` → `0 is not None` 成立 → 被判成「取到真数」。
   ⚠️ 项目里早就有正确判据 `critic.is_vacuous`（`_is_zeroish(0)` → True →
   全零 = 真空），但这一层**自己写了个弱的**，没复用。

② **会话程序验收只查「RESULT 里有没有非空值」**：
       _ok_prog = ok and any(v is not None and v != "" ...)
   兄弟维度取到真值（表存在性检查确实取到了数）→ 放行；
   而 `overall`（会话区别于维度技能的那一半）是**占位假值**也照样入库。

## 铁律

**「整体取数」是会话的核心 —— 它假了，整条会话就是一条假配方。**
判据复用 `critic.is_vacuous`（不重造，同 `_has_real` 的既有做法）。

## 附带钉死

`SELECT 0` 判真空，但**真的 0 也是真空吗**？—— 按项目既有口径
（`is_vacuous` 的 `_is_zeroish`：数值 0 算空），**是**：0 与「没查到」
在 SQL 聚合里不可区分，空表上的 `COUNT(*)` 也返回 0 → 同一把尺子。
本测试钉住这个既有口径不被悄悄改掉。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}" + (f" —— {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    from voyager_vlml.agents.critic import is_vacuous

    print("== 1. 组合层用的真值判据必须能抓住 `SELECT 0` 这种占位 ==")
    # 组合层的 SQL 执行结果形状：{"columns": [...], "rows": [[...]]}
    _fake = {"columns": ["record_count"], "rows": [[0]]}
    check("SELECT 0 → 真空（is_vacuous）", is_vacuous(_fake))
    _empty_agg = {"columns": ["cnt"], "rows": [[None]]}
    check("空表聚合 (NULL) → 真空", is_vacuous(_empty_agg))
    _real = {"columns": ["cnt"], "rows": [[2]]}
    check("真实值 2 → 不真空", not is_vacuous(_real))

    print("== 2. learn_orchestrate 的组合层已改用 is_vacuous（不再自写弱的） ==")
    import inspect
    import learn_orchestrate as _lo
    _src = inspect.getsource(_lo.learn_milestone_composite)
    check("组合层引用了 is_vacuous",
          "is_vacuous" in _src)
    # 主路径必须走 is_vacuous；`except` 里的降级分支（判据取不到时）允许
    # 保留旧的宽松写法 —— 它只在 import 失败时兜底，不是常态路径。
    _main = _src.split("except Exception:")[0]
    check("主路径不再用「只判 None」的弱判据",
          "v is not None for row in rows[:3]" not in _main,
          "弱判据只在 except 降级分支里（判据取不到时的兜底）")

    print("== 3. 会话程序验收：overall 假值 → 不许入库 ==")
    check("会话程序验收里检查了 overall",
          "overall" in _src and "_overall_real" in _src)
    check("overall 用真空判据（is_vacuous）",
          "_isvac2" in _src)

    print("== 4. 既有口径不被悄悄改掉：数值 0 与「没查到」同尺 ==")
    check("0 算空（与空表聚合同尺）",
          is_vacuous({"columns": ["c"], "rows": [[0]]}))
    check("False 不算空（bool 是 int 子类，必须先挡）",
          not is_vacuous({"columns": ["c"], "rows": [[False]]}))
    check("True 不算空",
          not is_vacuous({"columns": ["c"], "rows": [[True]]}))
    check("空串算空", is_vacuous({"columns": ["c"], "rows": [[""]]}))
    check("字符串 '0' 不算空（非数值）",
          not is_vacuous({"columns": ["c"], "rows": [["0"]]}))

    print("== 5. 整表多列时：只要有一列是真值就不算真空 ==")
    check("一行里混着 0 和真值 → 不真空",
          not is_vacuous({"columns": ["a", "b"], "rows": [[0, 5]]}))
    check("一行里全是 0 → 真空",
          is_vacuous({"columns": ["a", "b"], "rows": [[0, 0]]}))

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
