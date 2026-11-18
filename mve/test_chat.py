"""phase2_chat 回归测试 —— 不调真 LLM（除最后一项可选）。

对齐说明书逐条验：
  T1 门：phase!=2 → wrong_phase                     (route.ts:37-42)
  T2 正常：mode/response/exchange 三件齐            (schemas.ts:397-401)
  T3 非法 mode → 不成立，不猜                       (OpusMode 枚举)
  T4 只追加：三次调用 exchanges 长度=3，无 phase 字段 (route.ts:85)
  T5 history 只带最近 6 条                          (phase2-chat.ts:75)
  T6 chat_streak：连续/断开/换落点                   (phase2-chat.ts:12-18)
  T7 passable k=3                                   (常量，不由模型定)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import phase2_chat as pc  # noqa: E402

ok = 0
fail = []


def chk(name, cond, extra=""):
    global ok
    if cond:
        ok += 1
        print("  OK  %-46s %s" % (name, extra))
    else:
        fail.append(name)
        print("  !!  %-46s %s" % (name, extra))


def fake_llm(payload, mode="interrogative", resp="再想想你的循环出口在哪"):
    def f(msgs):
        payload.append(msgs)
        return json.dumps({"mode": mode, "response": resp}, ensure_ascii=False)
    return f


print("== T1 门：只认 phase==2")
r = pc.chat(prompt_text="p", spec_text="s", student_message="为什么我的循环提前结束",
            phase=1, llm=lambda m: "{}")
chk("phase=1 -> wrong_phase", r.get("refused") == "wrong_phase", str(r))
r = pc.chat(prompt_text="p", spec_text="s", student_message="", phase=2,
            llm=lambda m: "{}")
chk("空消息 -> empty_message", r.get("refused") == "empty_message")

print("== T2 正常一问一答")
calls = []
r = pc.chat(prompt_text="写一个函数证明 B≠A", spec_text="冻结的 spec 全文",
            current_code="def f(): pass", student_message="我的方法对吗",
            phase=2, llm=fake_llm(calls))
chk("mode=interrogative", r.get("mode") == "interrogative")
chk("exchange 四字段齐", set((r.get("exchange") or {}).keys())
    == {"timestamp", "studentMessage", "opusMode", "opusResponse"},
    str(list((r.get("exchange") or {}).keys())))
sysmsg = calls[0][0]["content"]
chk("system 逐字含 DECISIVE 段", "INTERROGATIVE mode — triggered"
    in sysmsg and "DIRECT mode — triggered" in sysmsg)
usr = calls[0][1]["content"]
chk("user 五段齐（题面/水平/spec/code/新消息）",
    all(x in usr for x in ["EXERCISE PROMPT:", "STUDENT LEVEL:",
                           "STUDENT'S SPEC (frozen):", "STUDENT'S CURRENT CODE:",
                           "NEW STUDENT MESSAGE:"]))
chk("maxTokens 常量 = 1024", pc.MAX_TOKENS == 1024)

print("== T3 非法 mode 不当成 direct")
r = pc.chat(prompt_text="p", spec_text="s", student_message="嗯", phase=2,
            llm=lambda m: json.dumps({"mode": "asking", "response": "x"}))
chk("bad_mode -> refused", r.get("refused") == "bad_mode", str(r))

print("== T4 只追加：三问三答，账本只多三条 exchange")
tmp = tempfile.mkdtemp(prefix="chat_")
topic = "action_code.py::_tool_signatures"
for i in range(3):
    r = pc.chat(prompt_text="p", spec_text="s", current_code="c",
                student_message="第%d问：这里的分支为什么走不到" % i,
                recent_exchanges=pc.load_exchanges(tmp, topic),
                phase=2, llm=fake_llm([]))
    pc.append_exchange(tmp, topic, r["exchange"], spec_text="s", code="c")
exs = pc.load_exchanges(tmp, topic)
chk("三次调用 exchanges=3", len(exs) == 3, "len=%d" % len(exs))
raw = json.loads(pc._chat_path(tmp, topic).read_text(encoding="utf-8"))
chk("账本无 phase 字段（chat 不推进相位）",
    "currentPhase" not in raw and "phase" not in raw, str(list(raw.keys())))
chk("账本只有 opusExchanges 是列表", isinstance(raw.get("opusExchanges"), list))

print("== T5 history 只带最近 6 条")
calls.clear()
long_hist = [{"studentMessage": "q%d" % i, "opusMode": "interrogative",
              "opusResponse": "a%d" % i} for i in range(10)]
pc.chat(prompt_text="p", spec_text="s", student_message="新问题",
        recent_exchanges=long_hist, phase=2, llm=fake_llm(calls))
u = calls[0][1]["content"]
chk("history 只出现 q4..q9", "student: \"q4\"" in u and "student: \"q9\"" in u
    and "student: \"q3\"" not in u and "student: \"q0\"" not in u)

print("== T6 chat_streak：游程")
ii = [{"opusMode": "interrogative", "studentMessage": "a"},
      {"opusMode": "interrogative", "studentMessage": "b"},
      {"opusMode": "interrogative", "studentMessage": "c"}]
chk("i,i,i -> 3", pc.chat_streak(ii) == 3)
chk("i,i,d -> 0", pc.chat_streak(ii + [{"opusMode": "direct",
                                        "studentMessage": "d"}]) == 0)
chk("i,i,d,i -> 1", pc.chat_streak([{"opusMode": "interrogative", "studentMessage": "a"},
                                    {"opusMode": "interrogative", "studentMessage": "b"},
                                    {"opusMode": "direct", "studentMessage": "c"},
                                    {"opusMode": "interrogative", "studentMessage": "d"}]) == 1)
# 换落点：末尾 3 条 interrogative，但只有最后 1 条指向 site
loc = {"a": "x.py::f", "b": "x.py::f", "c": "y.py::g"}.get
mixed = [{"opusMode": "interrogative", "studentMessage": "a"},
         {"opusMode": "interrogative", "studentMessage": "b"},
         {"opusMode": "interrogative", "studentMessage": "c"}]
chk("换落点即断 -> 1", pc.chat_streak(mixed, locate=loc, site="y.py::g") == 1)

print("== T7 passable：k 是常量")
chk("k=3 且 streak=3 -> True", pc.passable(ii, k=3) is True)
chk("k=3 且 streak=2 -> False", pc.passable(ii[:2], k=3) is False)
chk("默认 k=3", pc.passable(ii) is True)

print("== T8 发问器：抄事实 + 问一句，不按报错类型改写措辞")
_a = pc.build_student_message(err="ImportError: cannot import name 'xx'")
_b = pc.build_student_message(err="TypeError: unsupported operand type(s) for +: 'int' and 'str'")
_c = pc.build_student_message(err="AttributeError: 'dict' object has no attribute 'split'")
chk("报错原样照抄（一字不改）", "cannot import name 'xx'" in _a, _a[:52])
chk("三种报错同一句式（证明不按类型分支）",
    _a.split("是：")[0] == _b.split("是：")[0] == _c.split("是：")[0],
    _a[:26])
chk("不出现「该怎么用」这类被设计过的措辞",
    all("该怎么用" not in m for m in (_a, _b, _c)), _a[:40])
m3 = pc.build_student_message(critique="spec 要求返回错误信息，但程序静默返回空字典。")
chk("评语原样照抄", "静默返回空字典" in m3, m3[:46])
chk("评语末尾句号不重复", "。。" not in m3, m3[-18:])
m4 = pc.build_student_message(code="print(1)")
chk("都无 -> 问是否真证明", "真的算证明了" in m4, m4[:44])
chk("_is_external 已删除", not hasattr(pc, "_is_external"))
chk("发问器不带 mode 参数",
    "mode" not in __import__("inspect").signature(
        pc.build_student_message).parameters)

print("\n%d 项通过，%d 项失败 %s" % (ok, len(fail), ("→ " + ",".join(fail)) if fail else ""))
sys.exit(1 if fail else 0)
