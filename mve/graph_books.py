#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_books —— 视频书单（The Math Sorcerer 四级 72 本）进图谱。

和 graph_curriculum 的分工
--------------------------
graph_curriculum 管**有题库**的场景（N）：关 = math_modules 的一节，题 = 库里的
exercise，进度靠 answer_norm 对答案。
graph_books 管**没有题库**的场景（P）：关 = 一本书的一个单元，题由模型按单元
现场出，Phase1-3 拆解判定。两者写的是同一张图谱、同一套节点/边语义：

  book:<book_id>        kind=book     label=书名
                        detail={level, order, subject, author, public_text}
  topic:unit:<b>:<i>    kind=topic    label=单元名
                        detail={module_id, book_id, subject, unit, unit_ord,
                                level, ord, index, usable_total=1, per_topic=1,
                                source="curriculum:subject"}
  scene:P --has_book--> book              declared（人预写：书单）
  book --has_unit--> topic:unit:<b>:<i>   declared（人预写：单元）
  book:<b> --next--> book:<b+1>           declared（书单顺序）
  topic:unit:<b>:<i> --has_exercise--> exercise:gen:...   observed（学完）

单元名是**学科通用单元**（Limits and Continuity 这类），不是某本书的原文目录。
标注 source=curriculum:subject 就是为了不冒充原文：逐本核对真目录是后续的活，
现在先把「路线」立起来，让规划层有东西可读。

进度口径：一个单元 `per_topic=1` 道验证过的题就算过 —— 没有题库时，不存在
「这一关有 13 道可算题」这回事，模型出 1 道、拆通、判定过，就算学过这一单元。

用法
----
    python3 graph_books.py --scene P --seed
    python3 graph_books.py --scene P --progress
    python3 graph_books.py --scene P --anchor unit:l1-01:1   # 锚点：从这一单元起
    python3 graph_books.py --scene P --spec                  # 给下一单元的出题规格
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOOKS_JSON = HERE / "books_mathsorcerer.json"          # 旧书单（Math Sorcerer 72 本）

# 场景声明优先（2026-11-16 助产士同构的同一原则）：scenes.json 里写了
# books_json 的场景，书单文件由场景自己指定 —— 换资源 = 换声明，不改代码。
def _books_json(scene: str = "P") -> Path:
    try:
        import scenes as _scenes
        _f = str((_scenes.get_scene(str(scene or "P")) or {}).get("books_json") or "").strip()
        if _f:
            _p = Path(_f) if Path(_f).is_absolute() else HERE / _f
            if _p.exists():
                return _p
    except Exception:
        pass
    return BOOKS_JSON

BOOK_KIND = "book"
TOPIC_KIND = "topic"

# 每学科的通用单元（顺序即学习顺序）。刻意用学科通用名，不用某本书的私有名。
UNITS: dict[str, list[str]] = {
    "prealgebra": [
        "Whole Numbers and Integers", "Fractions and Mixed Numbers",
        "Decimals and Percents", "Ratios, Rates, and Proportions",
        "Expressions and Linear Equations", "Inequalities and Absolute Value",
        "Graphing and Functions Basics", "Geometry and Measurement",
    ],
    "algebra": [
        "Linear Equations and Inequalities", "Graphs and Functions",
        "Systems of Equations", "Polynomials", "Factoring",
        "Rational Expressions", "Radicals and Rational Exponents",
        "Quadratic Equations", "Exponential and Logarithmic Functions",
        "Conic Sections", "Sequences and Series",
    ],
    "geometry": [
        "Points, Lines, and Planes", "Angles and Parallel Lines",
        "Triangles and Congruence", "Similarity",
        "Right Triangles and Trigonometry", "Circles",
        "Area and Perimeter", "Surface Area and Volume",
    ],
    "trigonometry": [
        "Angles and Radian Measure", "Trigonometric Functions", "The Unit Circle",
        "Graphs of Trigonometric Functions", "Identities and Equations",
        "Inverse Trigonometric Functions", "Law of Sines and Cosines",
        "Vectors and Polar Coordinates",
    ],
    "calculus": [
        "Limits and Continuity", "Derivatives", "Applications of Derivatives",
        "Integrals", "Techniques of Integration", "Applications of Integration",
        "Sequences and Series", "Parametric Equations and Polar Coordinates",
        "Vectors and Geometry of Space", "Partial Derivatives", "Multiple Integrals",
    ],
    "differential-equations": [
        "First-Order Equations", "Second-Order Linear Equations",
        "Systems of Linear Equations", "Laplace Transforms", "Series Solutions",
        "Numerical Methods", "Boundary Value Problems", "Applications and Modeling",
    ],
    "statistics": [
        "Descriptive Statistics", "Probability Basics", "Discrete Random Variables",
        "Continuous Random Variables", "Sampling Distributions",
        "Confidence Intervals", "Hypothesis Testing",
        "Correlation and Regression", "Chi-Square and ANOVA",
    ],
    "survey-math": [
        "Sets and Logic", "Number Theory Basics", "Consumer Mathematics",
        "Geometry in Art", "Probability and Statistics in Society",
        "Graph Theory and Networks",
    ],
    "proof": [
        "Statements and Logic", "Quantifiers", "Direct Proof",
        "Contrapositive and Contradiction", "Mathematical Induction", "Sets",
        "Relations", "Functions", "Cardinality",
    ],
    "discrete-math": [
        "Logic and Proofs", "Sets and Functions", "Algorithms and Complexity",
        "Number Theory and Cryptography", "Induction and Recursion", "Counting",
        "Relations", "Graphs", "Trees",
    ],
    "real-analysis": [
        "Real Numbers and Completeness", "Sequences", "Series",
        "Limits and Continuity", "Differentiation", "Riemann Integration",
        "Sequences of Functions", "Metric Spaces", "Multivariable Analysis",
        "Lebesgue Integration",
    ],
    "linear-algebra": [
        "Systems of Linear Equations", "Vector Spaces", "Linear Transformations",
        "Matrices and Determinants", "Eigenvalues and Eigenvectors",
        "Inner Product Spaces", "Canonical Forms", "Applications",
    ],
    "abstract-algebra": [
        "Groups", "Subgroups and Homomorphisms", "Quotient Groups", "Rings",
        "Ideals and Quotient Rings", "Fields and Extensions", "Polynomials",
        "Modules and Galois Theory",
    ],
    "complex-analysis": [
        "Complex Numbers", "Analytic Functions", "Elementary Functions",
        "Complex Integration", "Cauchy Theory", "Power Series", "Residues",
        "Conformal Mapping",
    ],
    "topology": [
        "Topological Spaces", "Basis and Subbasis",
        "Continuity and Homeomorphism", "Connectedness", "Compactness",
        "Separation Axioms", "Metric Spaces", "Fundamental Group",
    ],
    "set-theory": [
        "Axioms of Set Theory", "Relations and Functions", "Orderings",
        "Ordinal Numbers", "Cardinal Numbers", "Axiom of Choice",
        "Transfinite Induction",
    ],
    "number-theory": [
        "Divisibility", "Primes and Factorization", "Congruences",
        "Fermat and Euler Theorems", "Quadratic Residues", "Arithmetic Functions",
        "Diophantine Equations", "Continued Fractions",
    ],
    "probability": [
        "Probability Spaces", "Conditional Probability", "Random Variables",
        "Common Distributions", "Expectation and Variance",
        "Law of Large Numbers", "Central Limit Theorem", "Estimation and Testing",
    ],
    "pde": [
        "First-Order Equations", "Heat Equation", "Wave Equation",
        "Laplace Equation", "Separation of Variables",
        "Fourier Series and Transforms", "Green's Functions", "Numerical Methods",
    ],
    "numerical-analysis": [
        "Error Analysis", "Root Finding", "Interpolation", "Numerical Integration",
        "Linear Systems", "Eigenvalue Problems", "Initial Value Problems",
        "Boundary Value Problems",
    ],
    "engineering-math": [
        "Complex Numbers and Functions", "Linear Algebra and Matrices",
        "Ordinary Differential Equations", "Laplace Transforms",
        "Fourier Series and Transforms", "Vector Calculus",
        "Partial Differential Equations", "Numerical Methods",
    ],
    "measure-theory": [
        "Sigma-Algebras", "Measures", "Measurable Functions", "Integration",
        "Convergence Theorems", "Product Measures", "Radon-Nikodym Theorem",
        "Lp Spaces",
    ],
    "functional-analysis": [
        "Normed Spaces", "Banach Spaces", "Hilbert Spaces",
        "Bounded Linear Operators", "Dual Spaces", "Spectral Theory",
        "Compact Operators", "Applications",
    ],
}

# 学习顺序：同学科的书按书单顺序读，但**先读完一个学科再进下一个**会让路线
# 在 5 本微积分之间来回跳。所以路线按 (level, book.order, unit) —— 即书单原序。
# 这正是视频的主张：一本一本啃完，再进下一本。

DIFFICULTY = {1: "compute", 2: "prove", 3: "structure", 4: "rebuild", 5: "interface", 6: "infra"}


def load_books(scene: str = "P") -> list[dict]:
    return list(json.loads(_books_json(scene).read_text(encoding="utf-8"))["books"])


def units_of(book: dict) -> list[str]:
    """单元的**标题**列表。真目录优先：有 PDF 的书返回书里的章/节名。"""
    return [s["title"] for s in unit_specs(book)]


def unit_specs(book: dict) -> list[dict]:
    """单元的完整规格（标题 + 考点 + 页范围 + 来源）。

    两级来源，绝不混：
      pdf:toc                —— 从这本书的 PDF 里抽出来的真目录，带页范围
      curriculum:subject     —— 占位：学科通用单元名，书里没有这一章

    为什么要区分：模板单元让 72 本书共用一套词，出题器照标题编题，看起来
    在「学这本书」，其实和书无关。真目录至少让进度落在真实章节上；页范围
    是为了「题从书里来」那一步能回查（answer_from 区禁读，等于防作弊）。
    """
    # 场景声明优先（2026-11-17 场景 S）：书单 JSON 里内联了 units 的书，
    # 单元 = 声明的真实模块组（title/points/files），不再走 PDF 目录或学科模板。
    inline = book.get("units")
    if inline:
        return [{"title": u["title"],
                 "points": u.get("points", ""),
                 "source": "inline:declared",
                 "files": list(u.get("files") or [])} for u in inline]

    import book_content

    bid = str(book.get("id") or "")
    real = book_content.units(bid)
    if real:
        return [{"title": u["title"],
                 "points": POINTS_BOOK.get(f"{bid}:{u['i']}", ""),
                 "source": "pdf:toc",
                 "chapter": u.get("chapter"),
                 "chapter_title": u.get("chapter_title") or "",
                 "pdf_from": u.get("pdf_from"),
                 "pdf_to": u.get("pdf_to"),
                 "answer_from": u.get("answer_from"),
                 "answer_to": u.get("answer_to")} for u in real]

    from syllabus import points_for as _points_for
    subj = str(book.get("subject") or "")
    return [{"title": t, "points": _points_for(subj, t), "source": "curriculum:subject"}
            for t in UNITS.get(subj, [])]


# 真目录单元的考点：书里那一章/一节讲什么，就写什么。空着也不算错——
# 模板考点是按学科通用单元名写的，套到真章节上会指错地方，宁可空。
POINTS_BOOK: dict[str, str] = {
    # ---- l3-03 Introductory Discrete Mathematics（Balakrishnan，9 章真目录）----
    "l3-03:1": "集合的并交差与补；幂集；集合的基数；关系与函数的定义；单射满射双射；良序原理；数学归纳法；递归定义；命题联结词与真值表；重言式与矛盾式",
    "l3-03:2": "加法原理与乘法原理；排列 P(n,r)；组合 C(n,r)；可重排列与可重组合；圆排列；鸽巢原理；容斥原理；二项式系数与多项式系数；集合的划分",
    "l3-03:3": "普通生成函数；指数生成函数；用生成函数解计数问题；生成函数的运算（加法/乘法/求导）",
    "l3-03:4": "齐次递推关系的特征方程解法；非齐次递推关系；用生成函数解递推；算法复杂度分析中的递推",
    "l3-03:5": "图与有向图的定义；邻接矩阵与关联矩阵；顶点的度；连通性与强连通；路径与回路",
    "l3-03:6": "欧拉路径与欧拉回路；de Bruijn 图；哈密顿路径与哈密顿回路；顶点着色与平面性",
    "l3-03:7": "树的定义与性质；生成树；二叉树；前缀码",
    "l3-03:8": "Kruskal 贪心算法；Prim 贪心算法；最小生成树；两种算法的比较",
    "l3-03:9": "Dijkstra 最短路径算法；Floyd-Warshall 算法；带权有向图的最短距离",
    # ---- l1-03 Schaum's Outline of Geometry（19 章真目录）----
    "l1-03:1": "点、线、面基本术语；线段与射线；角的度量与分类；三角形按边按角分类；对顶角与邻补角",
    "l1-03:2": "演绎推理；公设与公理；定理的条件与结论；逆命题；证明的书写规范",
    "l1-03:3": "全等的定义与对应关系（对应边对应角必须一一对应）；SSS/SAS/ASA/AAS/HL 判定；等腰三角形等边对等角与三线合一；等边三角形",
    "l1-03:4": "平行线的判定与性质；同位角内错角同旁内角；三角形内角和与外角定理；多边形内角和；两平行线间距离",
    "l1-03:5": "平行四边形性质与判定；矩形菱形正方形；梯形与梯形中位线；三角形中位线定理",
    "l1-03:6": "圆的元素；弦与垂径定理；圆心角与圆周角；切线与切线长；圆内接四边形",
    "l1-03:7": "相似三角形判定（AA/SAS/SSS）；相似比与面积比；比例线段；平行线分线段成比例",
    "l1-03:8": "正弦余弦正切的定义；特殊角的三角函数值；解直角三角形；仰角俯角的应用",
    "l1-03:9": "多边形面积公式；三角形面积（底乘高、海伦公式、正弦公式）；圆与扇形面积；面积比与相似比",
    "l1-03:10": "正多边形性质；边心距与半径；正多边形面积；圆的周长与弧长",
    "l1-03:11": "轨迹的概念；常见轨迹（垂直平分线、角平分线、圆）",
    "l1-03:12": "坐标系；两点距离与中点公式；直线斜率与方程；圆的方程；坐标法证题",
    "l1-03:13": "不等式性质；三角形三边关系；反证法",
    "l1-03:14": "充分条件与必要条件；常见逻辑推理错误辨析",
    "l1-03:15": "尺规作图基本作图；作垂线平行线角平分线；作图题的证明",
    "l1-03:16": "重要定理的证明（勾股定理、角平分线定理等）",
    "l1-03:17": "立体图形；体积与表面积；棱柱棱锥圆柱圆锥球",
    "l1-03:18": "平移、旋转、反射；对称性；位似变换",
    "l1-03:19": "非欧几何简介；球面几何的基本差别",
    "l1-01:1": "整数与分数的四则混合运算；括号优先级；指数与根；正负数加减乘除",
    "l1-01:2": "运算顺序（PEMDAS）；嵌套括号；分数与指数混排；逐步化简",
    "l1-01:3": "分数约分与通分；分数四则运算；带分数；分数化简求值",
    "l1-01:4": "小数四则运算；小数与分数互化；四舍五入；循环小数",
    "l1-01:5": "百分数与分数/小数互化；求一个数的百分之几；增减百分比；百分数应用",
    "l1-01:6": "代数式求值；合并同类项；去括号；分配律；代入数值计算",
    "l1-01:7": "一元一次方程：移项、去分母、去括号；解方程并验算；应用题建模",
    "l1-01:8": "比与比例；比例式求解；比例分配；相似图形中的比例",
    "l1-01:9": "速率问题：路程=速度×时间；单位换算；相遇/追及；工作效率",
    "l1-01:10": "不等式性质；解一元一次不等式；解集在数轴上的表示；端点开闭",
    # ---- 人教A版高中数学（hs-01..05，2026-11-16 场景 P 换资源）----
    "hs-01:1": "集合的概念与表示；子集真子集空集；交并补运算；充分条件必要条件充要条件；全称量词与存在量词；含量词命题的否定",
    "hs-01:2": "不等式性质与比较大小；基本不等式 ab≤(a+b)/2 及应用；一元二次函数图象与性质；一元二次方程根与判别式；一元二次不等式解法（三个二次的关系）",
    "hs-01:3": "函数的概念、定义域值域与对应关系；区间与无穷；函数的表示法（解析法/图象/列表）；单调性与单调区间（定义法证明）；奇偶性；幂函数图象与性质",
    "hs-01:4": "指数与根式运算；指数函数图象与性质（底数分类讨论）；对数运算与换底公式；对数函数图象与性质；函数零点存在定理与二分法；函数模型及应用（增长速度比较）",
    "hs-01:5": "任意角与弧度制；三角函数定义与符号；同角三角函数基本关系；诱导公式；正弦余弦正切函数图象与性质；y=Asin(ωx+φ) 的图象变换；两角和差公式与二倍角公式；辅助角公式；三角恒等变换化简求值",
    "hs-02:1": "向量的实际背景与概念；线性运算与共线向量定理；平面向量基本定理；坐标表示与坐标运算；数量积与夹角；投影向量；正弦定理与余弦定理；解三角形与应用（测量、最值）",
    "hs-02:2": "复数的概念与分类；复数相等与共轭；复数的几何意义与模；复数四则运算；|z1-z2| 的几何意义；复数三角表示与棣莫弗（选学）",
    "hs-02:3": "棱柱棱锥棱台圆柱圆锥球的表面积与体积；斜二测直观图；空间点线面位置关系与8条基本事实；异面直线所成角；线面平行/垂直的判定与性质；面面平行/垂直的判定与性质；空间角与距离的初步计算",
    "hs-02:4": "简单随机抽样与分层随机抽样；频率分布直方图与频率分布表；总体百分位数；平均数中位数众数；方差与标准差；用样本估计总体；统计案例分析",
    "hs-02:5": "随机事件与样本空间；事件的运算与关系；古典概型（列举法）；事件独立性与乘法公式；条件概率；频率与概率的关系；生活中的概率模型",
    "hs-03:1": "空间向量及其线性运算；共线向量与共面向量定理；空间向量基本定理；空间向量的坐标运算；空间角（异面直线角、线面角、二面角）的向量求法；点面距与空间距离的向量法",
    "hs-03:2": "直线的倾斜角与斜率；直线方程五种形式；两直线平行垂直与交点；两点间距离公式；圆的标准方程与一般方程；直线与圆的位置关系（弦长问题）；圆与圆的位置关系",
    "hs-03:3": "椭圆的定义、标准方程与几何性质（离心率）；双曲线的定义、标准方程与渐近线；抛物线的定义、标准方程与焦点弦；直线与圆锥曲线的位置关系（联立、韦达定理、中点弦）；轨迹问题",
    "hs-04:1": "数列的概念与通项公式；数列的函数性质；等差数列定义、通项与前n项和；等比数列定义、通项与前n项和；错位相减与裂项相消求和；an与Sn关系；递推数列构造；数学归纳法",
    "hs-04:2": "瞬时变化率与导数概念；导数的几何意义（切线）；基本初等函数导数公式；导数四则运算法则；复合函数求导（链式法则）；用导数研究单调性；极值与最值；恒成立与参数分离；函数零点与图象综合",
    "hs-05:1": "分类加法计数原理与分步乘法计数原理；排列与排列数；组合与组合数及性质；分组分配问题；二项式定理；通项公式与特定项系数；杨辉三角",
    "hs-05:2": "离散型随机变量与分布列；两点分布与二项分布；超几何分布；期望与方差的性质及线性变换；正态分布 3σ 原则与对称性；决策问题",
    "hs-05:3": "变量的相关关系与散点图；样本相关系数；一元线性回归模型与最小二乘法；残差分析；2×2 列联表；χ² 独立性检验及应用",
}


def unit_key(book_id: str, i: int) -> str:
    """topic 节点 id 的后半段，也是 record_solved 的 module_id。"""
    return f"unit:{book_id}:{i}"


def parse_unit_key(key: str) -> tuple[str, int]:
    parts = str(key or "").split(":")
    if len(parts) < 3 or parts[0] != "unit":
        return "", 0
    try:
        return parts[1], int(parts[2])
    except Exception:
        return parts[1], 0


# ---------------------------------------------------------------- 写：seed

def seed(scene: str = "P", *, save: bool = True) -> dict:
    from knowledge_graph import Edge, KnowledgeGraph, Node

    books = load_books(scene)
    g = KnowledgeGraph.load()
    g.ensure_scene_root(scene)
    root = f"scene:{scene}"
    nodes = getattr(g, "nodes", {}) or {}
    new_nodes = new_edges = topics = 0
    replaced: list[str] = []

    for b in books:
        bid = str(b["id"])
        nid = f"{BOOK_KIND}:{bid}"
        specs = unit_specs(b)
        detail = {
            "book_id": bid, "title": b["title"], "author": b.get("author") or "",
            "level": int(b["level"]), "order": int(b["order"]),
            "subject": b.get("subject") or "",
            "public_text": bool(b.get("public_text")),
            "public_url": b.get("public_url") or "",
            "difficulty": DIFFICULTY.get(int(b["level"]), ""),
            "source": "seed:video_book_list",
            # 这本书的单元是原文目录还是学科模板？面板要能一眼看出来。
            "units_source": (specs[0]["source"] if specs else ""),
            "units_count": len(specs),
        }
        if nid in nodes:
            nodes[nid].detail = {**(nodes[nid].detail or {}), **detail}
        else:
            g.add_node(Node(id=nid, kind=BOOK_KIND, label=f"L{b['level']}·{b['title']}",
                            detail=detail, root_scene=scene,
                            status="verified", evidence=1))
            new_nodes += 1
        before = len(g.edges)
        g.add_edge(Edge(src=root, dst=nid, relation="has_book",
                        origin="declared", reason="视频四级书单"))
        new_edges += len(g.edges) - before

        for i, spec in enumerate(specs, 1):
            key = unit_key(bid, i)
            tid = f"{TOPIC_KIND}:{key}"
            udetail = {
                "module_id": key, "book_id": bid, "book_title": b["title"],
                "subject": b.get("subject") or "", "unit": spec["title"],
                "unit_ord": i, "level": int(b["level"]),
                "ord": int(b["level"]) * 1_000_000
                       + int(b["order"]) * 10_000 + i * 10,
                "usable_total": 1, "per_topic": 1,
                # 考点范围：单元名只是标题，出题器照标题猜会跑偏（实测
                # Similarity 出了勾股定理）。有考点的单元按考点出题。
                "points": spec.get("points") or "",
                "source": spec["source"],
            }
            if spec.get("files"):
                udetail["files"] = spec["files"]
            if spec.get("pdf_from"):
                # 页码让「题从书里来」能回查；answer_* 是书后答案区，禁读。
                udetail.update({
                    "pdf_from": spec.get("pdf_from"), "pdf_to": spec.get("pdf_to"),
                    "answer_from": spec.get("answer_from"),
                    "answer_to": spec.get("answer_to"),
                })
            if spec.get("chapter") is not None:
                udetail["chapter"] = spec.get("chapter")
                udetail["chapter_title"] = spec.get("chapter_title") or ""
            old = nodes.get(tid)
            if old is not None:
                nodes[tid].detail = udetail
                # label 必须跟着换：真目录替换模板后还挂着模板名，面板会
                # 出现「单元 id 是 1、名字却还是 Whole Numbers」的分裂。
                if nodes[tid].label != spec["title"]:
                    nodes[tid].label = spec["title"]
                    replaced.append(tid)
            else:
                g.add_node(Node(id=tid, kind=TOPIC_KIND, label=spec["title"],
                                detail=udetail, root_scene=scene,
                                status="verified", evidence=1))
                new_nodes += 1
            topics += 1
            before = len(g.edges)
            g.add_edge(Edge(src=nid, dst=tid, relation="has_unit",
                            origin="declared",
                            reason=("书内真实目录" if spec["source"] == "pdf:toc"
                                    else ("场景声明（books_json 内联单元）"
                                          if spec["source"] == "inline:declared"
                                          else "学科通用单元"))))
            new_edges += len(g.edges) - before

        # 单元变少时（模板 9 个 → 真章节 6 个）删掉越界的旧节点，否则
        # 进度里会留着一批点不进去的幽灵单元。
        stale = [n for n, nd in list(nodes.items())
                 if str(getattr(nd, "kind", "")) == TOPIC_KIND
                 and str((nd.detail or {}).get("book_id") or "") == bid
                 and str(getattr(nd, "root_scene", "") or "") == str(scene)
                 and int((nd.detail or {}).get("unit_ord") or 0) > len(specs)]
        if stale:
            for n in stale:
                nodes.pop(n, None)
            g.edges = [e for e in g.edges
                       if getattr(e, "src", "") not in stale
                       and getattr(e, "dst", "") not in stale]
            replaced.extend(stale)

    ordered = sorted(books, key=lambda x: (int(x["level"]), int(x["order"])))
    for a, b in zip(ordered, ordered[1:]):
        before = len(g.edges)
        g.add_edge(Edge(src=f"{BOOK_KIND}:{a['id']}", dst=f"{BOOK_KIND}:{b['id']}",
                        relation="next", origin="declared",
                        reason="书单顺序（视频出现顺序）"))
        new_edges += len(g.edges) - before

    if save:
        g.save()
    real = sum(1 for b in books if unit_specs(b)[:1]
               and unit_specs(b)[0]["source"] == "pdf:toc")
    return {"scene": scene, "books": len(books), "units": topics,
            "books_with_real_toc": real,
            "relabeled": len(replaced),
            "new_nodes": new_nodes, "new_edges": new_edges,
            "total_nodes": len(getattr(g, "nodes", {}) or {}),
            "total_edges": len(getattr(g, "edges", []) or [])}


# ---------------------------------------------------------------- 写：锚点

# ---------------------------------------------------------------- 内容定位
# 导入的题面**不带**单元 id —— 用户只投一道题，位置该由系统认。
# 这里做的是「题面 ↔ 图谱里的单元」的对照：把中文题目里的关键说法翻成
# 单元名用的英文词，再看哪些单元被覆盖得最完整。
# 不用向量：648 个单元的词汇量很小，命中可解释（能说清「因为出现了 similarity」），
# 而向量相似度在数学单元名上经常把「Similarity」判给「Sequences」。
CN2EN: dict[str, tuple[str, ...]] = {
    "相似": ("similar", "similarity"),
    "全等": ("congruent", "congruence"),
    "三角": ("triangle", "triangles"),
    "四边形": ("quadrilateral",),
    "圆": ("circle", "circles"),
    "角": ("angle", "angles"),
    "平行": ("parallel",),
    "垂直": ("perpendicular",),
    "坐标": ("coordinate", "graph", "graphs"),
    "图象": ("graph", "graphs"),
    "一次函数": ("linear",),
    "二次": ("quadratic",),
    "函数": ("function", "functions"),
    "方程": ("equation", "equations"),
    "方程组": ("systems",),
    "不等式": ("inequalit",),
    "绝对值": ("absolute",),
    "因式": ("factor", "factoring"),
    "分数": ("fraction", "fractions"),
    "小数": ("decimal", "decimals"),
    "百分": ("percent",),
    "比例": ("proportion", "ratio", "ratios"),
    "比值": ("ratio",),
    "面积": ("area",),
    "周长": ("perimeter",),
    "体积": ("volume",),
    "表面积": ("surface",),
    "概率": ("probability",),
    "统计": ("statistic", "statistics"),
    "回归": ("regression",),
    "相关": ("correlation",),
    "随机": ("random",),
    "分布": ("distribution", "distributions"),
    "假设检验": ("hypothesis",),
    "置信": ("confidence",),
    "抽样": ("sampling",),
    "极限": ("limit", "limits"),
    "连续": ("continuity", "continuous"),
    "导数": ("derivative", "derivatives"),
    "微分": ("differenti", "derivative"),
    "积分": ("integral", "integrals"),
    "级数": ("series",),
    "数列": ("sequence", "sequences"),
    "矩阵": ("matrix", "matrices"),
    "向量": ("vector", "vectors"),
    "线性": ("linear",),
    "多项式": ("polynomial", "polynomials"),
    "指数": ("exponential",),
    "对数": ("logarithm",),
    "集合": ("set", "sets"),
    "逻辑": ("logic",),
    "证明": ("proof", "proofs"),
    "素数": ("prime", "primes"),
    "整除": ("divisibility",),
    "同余": ("congruence", "congruences"),
    "复数": ("complex",),
    "拓扑": ("topolog",),
    "群": ("group", "groups"),
    "随机变量": ("random", "variables"),
    "单位圆": ("unit", "circle"),
    "弧度": ("radian",),
    "正弦": ("trigonometric", "sine"),
    "余弦": ("trigonometric", "cosine"),
    "正切": ("trigonometric", "tangent"),
    "函数记号": ("function", "notation"),
    "差商": ("difference", "quotient"),
    "定义域": ("domain",),
    "值域": ("range",),
}


# 停用词：单元名里这些词不携带学科信息，参与打分只会让长标题虚高。
_STOP = {"and", "the", "for", "with", "from", "into", "over", "under", "its",
         "their", "about", "using", "basic", "basics", "introduction", "review"}


def _words(s: str) -> set[str]:
    import re as _re
    out = set()
    for w in _re.findall(r"[a-z][a-z\-]{2,}", str(s or "").lower()):
        if w in _STOP:
            continue
        # 轻量词干：Equations / equation、Functions / function 是同一个词。
        # 不做会把「解方程」判给唯一用单数的「Laplace Equation」。
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def locate(scene: str = "P", text: str = "", topk: int = 3) -> dict:
    """拿题面去图谱里认位置。返回 {candidates: [{unit, title, score, hits}]}。

    score 是「这个单元名里的词被题面覆盖了多少」，所以短而准的单元名会赢：
    「Similarity」1/1 > 「Triangles and Congruence」1/3 —— 题里同时提到三角形
    和相似时，落在 Similarity，跟人判的一样。
    """
    from knowledge_graph import KnowledgeGraph

    t = str(text or "")
    bag = _words(t)
    for cn, ens in CN2EN.items():
        if cn in t:
            for e in ens:
                bag.update(_words(e))
    out: list[dict] = []
    try:
        g = KnowledgeGraph.load()
    except Exception:
        return {"candidates": []}
    low = t.lower()
    # 先收齐本场景的单元名，统计每个词出现在多少个单元里（df）。
    # 为什么要它：「equations」这类词几十个单元都有，「similarity」只有两个 ——
    # 不按稀有度加权，一道只有「解方程」三个字的题会命中「Quadratic Equations」
    # （短标题命中率高），而该命中「Linear Equations and Inequalities」。
    import math
    labels: list[tuple[str, object, str, set[str]]] = []
    for nid, nd in (getattr(g, "nodes", {}) or {}).items():
        if str(getattr(nd, "kind", "") or "") != TOPIC_KIND:
            continue
        if str(getattr(nd, "root_scene", "") or "") != str(scene):
            continue
        label = str(getattr(nd, "label", "") or "")
        lw = _words(label)
        if lw:
            labels.append((nid, nd, label, lw))
    df: dict[str, int] = {}
    for _, _, _, lw in labels:
        for w in lw:
            df[w] = df.get(w, 0) + 1
    total = max(1, len(labels))

    def _w(word: str) -> float:
        return 1.0 + math.log((total + 1.0) / (1.0 + df.get(word, 0)))

    for nid, nd, label, lw in labels:
        hit = lw & bag
        if not hit:
            continue
        denom = sum(_w(x) for x in lw)
        score = (sum(_w(x) for x in hit) / denom) if denom else 0.0
        # 题面里整段出现单元名（英文题面常见）—— 强证据
        if label.lower() in low:
            score += 0.5
        d = getattr(nd, "detail", {}) or {}
        out.append({"unit": str(d.get("module_id") or nid.split(":", 1)[-1]),
                    "title": label, "book": str(d.get("book") or ""),
                    "level": d.get("level"), "ord": int(d.get("ord") or 0),
                    "score": round(score, 3), "hits": sorted(hit)})
    # 同分按 ord：分数一样时选更基础的那一关（书的顺序就是学习顺序）。
    # 之前第二键是「命中词数」，会让「Graphs and Functions」（命中 2 词）
    # 压过「Similarity」（命中 1 词）—— 词多不等于更贴题。
    out.sort(key=lambda x: (-x["score"], x["ord"]))
    return {"candidates": out[:max(1, int(topk))]}


def set_anchor(scene: str, key: str, *, save: bool = True) -> int:
    """把某一单元设为起点：它之前的单元标 skipped，不参与进度也不拦路。

    用户投的第一道题决定从哪儿开始 —— 不必从 Pre-algebra 第一页学起。
    """
    from knowledge_graph import KnowledgeGraph

    bid, i = parse_unit_key(key)
    if not bid:
        return 0
    g = KnowledgeGraph.load()
    target = f"{TOPIC_KIND}:{unit_key(bid, i)}"
    nodes = getattr(g, "nodes", {}) or {}
    if target not in nodes:
        return 0
    t_ord = int((nodes[target].detail or {}).get("ord") or 0)
    # 已学过的单元不标 skipped：skip 的语义是「不学」，学过的不该被抹成没学。
    # 2026-11-15 实测：把锚点抬到 l1-03:3 时，l1-01 已学完的 Ch1–Ch7 全被标了
    # skipped，render_text 的「已学完」计数直接归零（learned 账还在，只是被
    # skip 过滤遮住），面板看着像进度被清空。
    _learned = {str((getattr(nd, "detail", {}) or {}).get("module_id") or "")
                for nd in nodes.values()
                if str(getattr(nd, "kind", "")) == "exercise"
                and str(getattr(nd, "root_scene", "") or "") == str(scene)
                and str(getattr(nd, "status", "") or "") == "verified"}
    _learned.discard("")
    n = 0
    for nid, nd in nodes.items():
        if str(getattr(nd, "kind", "")) != TOPIC_KIND:
            continue
        if str(getattr(nd, "root_scene", "") or "") != str(scene):
            continue
        d = nd.detail or {}
        o = int(d.get("ord") or 0)
        if o < t_ord:
            mid = str(d.get("module_id") or nid.split(":", 1)[-1])
            if mid in _learned:
                if d.get("skipped"):
                    nd.detail = {**d, "skipped": False}
                    n += 1
                continue
            if not d.get("skipped"):
                nd.detail = {**d, "skipped": True}
                n += 1
        elif d.get("skipped"):
            # 往回退：锚点改到更早的单元时，后面的单元要重新参与进度。
            # 否则「导入一道基础题」会把已经跳过的后半本永久锁死。
            nd.detail = {**d, "skipped": False}
            n += 1
    root = nodes.get(f"scene:{scene}")
    if root is not None:
        root.detail = {**(root.detail or {}), "anchor_unit": key}
    if save:
        g.save()
    return n


# ---------------------------------------------------------------- 读：规划

# ---------------------------------------------------------------- 自由学：下一题
#
# 2026-11-18 用户拍板：**材料入口只剩一个背包** —— 没有书单、没有单元规划、
# 没有「图谱将做清单」。「下一题」只有一条路：出题器（curriculum_auto）。
#
# 原版对照（voyager/agents/curriculum.py:243-291）：下一题只由两样东西决定
#   —— 观察面（nearby_blocks + inventory，:142-209）和两条硬编码
#   （progress==0 → 常量最小任务 / 背包满 → 存箱）。既不看「这本书讲到第几
#   章」，也不看「图谱上还有哪些没做」。
#
# 因此这里不再有 ① 图谱将做 ② 书单当前单元 两条分支；`plan()` /
# `todo_reqs()` / `note_proposed()` / `unit_of_proposed()` 一并删除 ——
# 它们存在的唯一理由原是「给没有落点账的材料补一个进度单位」（旧 §16.5），
# 而落点账现在由会话事件流自己产出（`curriculum_auto.slot_content`）。

def default_llm(msgs):
    """出题器要的 llm 可调用（背包改造时被误删，2026-11-18 恢复）。

    ⚠️ `CurriculumAgent` 里是 `if self._llm is not None` 才调模型 —— 传 None
    会**静默不出题**。调用方必须给一个真能调的。

    模型口径：全局同一模型 —— 默认跟 ZHIPU_MODEL 走（面板服务层=glm-4.6），
    只在显式设置 MVE_PROMPT_MODEL 时才允许覆盖。出题属低频高影响调用，
    关思考、max_tokens 给足正文。
    """
    from llm_client import chat
    import os
    return chat(msgs, temperature=0.7, max_tokens=1800, timeout=150.0,
                model=os.getenv("MVE_PROMPT_MODEL", "")
                or os.getenv("ZHIPU_MODEL", "glm-4.6"),
                thinking={"type": "disabled"})


def next_task(scene: str = "P", *, llm=None) -> dict:
    """自由学的下一题 = 出题器。返回 {ok, source, question, unit, module_id, why}。

    面板「▶ 启动 Voyager」与 scene_learn 都调这里（同一份实现，不另开入口）。
    """
    import curriculum_auto as _ca
    try:
        r = _ca.propose_for(scene, llm=(llm or default_llm))
    except Exception as e:
        return {"ok": False, "source": "自由学", "question": "",
                "error": f"{type(e).__name__}: {e}"}
    if not r.get("question"):
        return {"ok": False, "source": "自由学", "question": "",
                "error": str(r.get("why") or "出题器没给出题")}
    return {"ok": True, "source": "自由学", "question": str(r["question"]),
            "unit": "", "module_id": "", "why": str(r.get("why") or "")}



def points_of(scene: str, module_id: str, *, graph=None) -> str:
    """单元节点上的考点串（seed 时写进 detail.points）；没有就返回空串。

    出题器要的是"这一单元考什么"，不是一个标题。空串时调用方按只有标题
    降级，不许假装有考点（syllabus 只覆盖 L1 的 8 个学科）。
    """
    try:
        if graph is None:
            from knowledge_graph import KnowledgeGraph
            graph = KnowledgeGraph.load()
        nd = (getattr(graph, "nodes", {}) or {}).get(f"{TOPIC_KIND}:{module_id}")
        return str(((getattr(nd, "detail", {}) or {}) if nd else {})
                   .get("points") or "")
    except Exception:
        return ""


def render_text(scene: str = "P", *, graph=None, limit: int = 6) -> str:
    from graph_curriculum import progress

    slots = progress(scene, graph=graph)
    live = [s for s in slots if not s.get("skipped")]
    if not live:
        return ""
    done = sum(1 for s in live if s["done"])
    cur = next((s for s in live if not s["done"]), None)
    head = (f"书单进度：{len(live)} 个单元，已学完 {done} 个"
            f"（每单元 1 道拆通并判定的题即算学完）\n")
    start = max(0, (cur["index"] - 1) if cur else 0)
    # live 的 index 是全量序号，按它在 live 里的位置开窗
    pos = live.index(cur) if cur else 0
    window = live[pos:pos + max(1, int(limit))]
    lines = []
    for t in window:
        mark = "  ← 当前" if cur and t["module_id"] == cur["module_id"] else ""
        lines.append(f"[{t['index']:>4}] L{t.get('level', '?')} {t['title'][:34]:<34}"
                     f" {t['book'][:26]:<26} {t['learned']}/{t['need']}{mark}")
    tail = ""
    if cur:
        # 真目录单元的页范围也报出来：面板上要能对上「这一关在书的哪几页」。
        try:
            import book_content as _bc
            _bid, _i = parse_unit_key(str(cur.get("module_id") or ""))
            hint = _bc.page_hint(_bid, _i) if _bid else ""
        except Exception:
            hint = ""
        pts = points_of(scene, str(cur.get("module_id") or ""), graph=graph)
        if not pts:
            try:
                import syllabus as _syl
                pts = _syl.points_for(str(cur.get("subject") or ""),
                                      str(cur.get("title") or ""))
            except Exception:
                pts = ""
        tail = (f"\n下一单元：L{cur.get('level','?')}「{cur['title']}」"
                f"（书：{cur['book']}）；单元 id = {cur['module_id']}。")
        if hint:
            tail += f"\n位置：{hint}"
        if pts:
            tail += (f"\n本单元考点：{pts}\n"
                     "出的题必须落在上面这些考点里（挑 1~2 个），"
                     "Context 行写明考的是哪一个；不要出别的单元的内容。")
        else:
            tail += "出题就从这一单元的知识点范围里出一道可判定的题。"
    else:
        tail = "\n书单已全部学完。"
    return head + "\n".join(lines) + tail


def spec(scene: str = "P") -> str:
    """下一单元的出题规格（题面模板）。exercise_id 用 gen:<scene>:<book>:<i>:<seq>，
    Phase2 回写时由 graph_curriculum.EID_RE 抓出来，落到对应 unit 节点。"""
    s = plan(scene)
    if not s:
        return ""
    bid, i = parse_unit_key(s["module_id"])
    eid = f"gen:{scene}:{bid}:{i}:1"
    return (f"场景 {scene} 的数学书单，当前单元：L{s.get('level', '?')}「{s['title']}」"
            f"（书：{s['book']}；单元 id = {s['module_id']}）。\n\n"
            f"按这一单元的知识点自己出一道有确定答案的题（含具体数值或表达式），"
            f"然后从头到尾做一遍。\n"
            f"不要去查目录、页码、章节编号——那些和解题无关，也不算进度。")


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="P")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--anchor", default="")
    ap.add_argument("--spec", action="store_true")
    a = ap.parse_args()
    if a.seed:
        print(json.dumps(seed(a.scene), ensure_ascii=False, indent=1))
    if a.anchor:
        print("skipped", set_anchor(a.scene, a.anchor))
    if a.spec:
        print(spec(a.scene))
    if a.json:
        from graph_curriculum import progress
        print(json.dumps(progress(a.scene), ensure_ascii=False, indent=1))
    elif a.progress or not (a.seed or a.anchor or a.spec):
        print(render_text(a.scene) or "（还没 seed）")


if __name__ == "__main__":
    main()
