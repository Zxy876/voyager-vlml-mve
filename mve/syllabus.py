#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""syllabus —— 书单单元的**考点范围**（单元名 → 这一单元到底考什么）。

为什么要这一层
--------------
graph_books 只给了 648 个单元名（"Similarity"、"Sequences" 这类）。出题器拿到
一个标题，只能凭字面猜：实测当前单元是 Similarity，连出两次"直角三角形已知
两边求第三边"——它不知道 Similarity 指相似三角形/比例/AA·SSS·SAS 判定。
标题不是课程内容，考点才是。

写法约定
--------
· 键 = (subject, unit)，unit 与 graph_books.UNITS 里的字符串逐字一致；
· 值 = 用「；」分隔的 4~7 个考点，按该单元的教学顺序排，可直接拿去出题；
· 只写**学科标准知识点**（任何一本同名教材都会讲的内容），不冒充某本书的
  原文目录，也不写具体页码/例题编号——那是逐本核对真目录的活（TODO）。
· 没有条目的单元返回空串，调用方按"只有标题"降级，不许假装有考点。

覆盖进度
--------
L1 的 8 个学科（69 个单元）已全覆盖：prealgebra / algebra / geometry /
trigonometry / calculus / differential-equations / statistics / survey-math。
L2~L4 待补。
"""
from __future__ import annotations

# (subject, unit) -> 考点串
POINTS: dict[tuple[str, str], str] = {
    # ------------------------------------------------------------ prealgebra
    ("prealgebra", "Whole Numbers and Integers"): (
        "整数四则运算与运算顺序；负数加减法与数轴；绝对值；因数倍数与质因数分解；"
        "最大公约数与最小公倍数；整除判定规则"),
    ("prealgebra", "Fractions and Mixed Numbers"): (
        "分数化简与通分；分数四则运算；带分数与假分数互化；分数比较大小；"
        "分数的实际应用（按比例分配）"),
    ("prealgebra", "Decimals and Percents"): (
        "小数四则运算与四舍五入；小数与分数互化；百分数意义与换算；"
        "百分数应用题（增减、折扣、利息）；科学计数法"),
    ("prealgebra", "Ratios, Rates, and Proportions"): (
        "比与比值的化简；比例式与交叉相乘；单位率与单位换算；"
        "正比例与反比例；比例分配与相似图形中的比例"),
    ("prealgebra", "Expressions and Linear Equations"): (
        "代数式写法与求值；合并同类项；一元一次方程移项与去分母；"
        "应用题建模（年龄、行程、价格）；公式变形"),
    ("prealgebra", "Inequalities and Absolute Value"): (
        "不等式性质与解集表示；一元一次不等式；数轴上区间与端点开闭；"
        "绝对值意义与绝对值的方程/不等式；复合不等式"),
    ("prealgebra", "Graphing and Functions Basics"): (
        "平面直角坐标系与象限；有序对与散点图；线性关系与斜率直观；"
        "函数概念与输入输出表；读图与趋势判断"),
    ("prealgebra", "Geometry and Measurement"): (
        "长度/面积/体积单位换算；长方形、三角形、圆的周长与面积；"
        "角度与角的分类；立体图形表面积与体积入门；单位制（英制与公制）"),

    # --------------------------------------------------------------- algebra
    ("algebra", "Linear Equations and Inequalities"): (
        "一元一次方程解法（去分母、含参）；一元一次不等式与解集；"
        "绝对值方程与不等式；应用题建模；恒等式与无解/无穷多解判定"),
    ("algebra", "Graphs and Functions"): (
        "斜率与截距；点斜式/斜截式/一般式互化；平行与垂直的斜率关系；"
        "函数定义域值域；分段函数；图像的平移与变换"),
    ("algebra", "Systems of Equations"): (
        "二元一次方程组代入法与消元法；三元方程组；无解/无穷多解的判定（行列式或斜率）；"
        "不等式组可行域；方程组的应用题建模"),
    ("algebra", "Polynomials"): (
        "多项式加减乘；乘法公式（平方差、完全平方、立方和差）；"
        "多项式除法与余式定理；因式定理；韦达定理"),
    ("algebra", "Factoring"): (
        "提公因式；十字相乘；分组分解；平方差与完全平方；"
        "高次多项式因式分解与有理根判定"),
    ("algebra", "Rational Expressions"): (
        "有理式化简与通分；有理式四则运算；繁分式；"
        "分式方程与增根检验；反比例与有理函数图像"),
    ("algebra", "Radicals and Rational Exponents"): (
        "根式化简与有理化分母；分数指数与根式互化；根式运算；"
        "含根号的方程与增根检验；复数单位 i 入门"),
    ("algebra", "Quadratic Equations"): (
        "配方法、公式法、因式分解法；判别式与根的性质；"
        "根与系数关系；二次函数顶点式与最值；二次不等式"),
    ("algebra", "Exponential and Logarithmic Functions"): (
        "指数运算律；指数函数图像与增长模型；对数定义与运算律；"
        "换底公式；指数方程与对数方程；复利与半衰期应用"),
    ("algebra", "Conic Sections"): (
        "圆的标准方程与一般方程；抛物线焦点准线；椭圆方程与离心率；"
        "双曲线渐近线；圆锥曲线判别与配方法化标准形"),
    ("algebra", "Sequences and Series"): (
        "等差数列通项与求和；等比数列通项与求和；递推数列；"
        "求和符号与裂项；无穷等比级数收敛条件"),

    # -------------------------------------------------------------- geometry
    ("geometry", "Points, Lines, and Planes"): (
        "点、线、面、射线、线段的基本关系；线段长度与中点坐标；"
        "平面基本性质与共面判定；相交线与交点；尺规作图基本步骤"),
    ("geometry", "Angles and Parallel Lines"): (
        "角的分类与度量；余角补角与对顶角；平行线判定（同位角、内错角、同旁内角）；"
        "平行线性质；角平分线；三角形与多边形内角和"),
    ("geometry", "Triangles and Congruence"): (
        "三角形分类与内角和/外角定理；全等判定 SSS、SAS、ASA、AAS、HL；"
        "等腰与等边三角形性质；垂直平分线与角平分线性质；三角形不等式"),
    ("geometry", "Similarity"): (
        "相似三角形定义与相似比；AA、SSS、SAS 相似判定；对应边成比例、对应角相等；"
        "相似比与周长比（=k）、面积比（=k²）的关系；平行线分线段成比例；"
        "相似的实际测量（影长测高、镜面反射）；按顶点顺序写对应关系（△ABC∽△DEF）"),
    ("geometry", "Right Triangles and Trigonometry"): (
        "勾股定理及其逆定理；30-60-90 与 45-45-90 特殊三角形；"
        "sin/cos/tan 的边比定义；解直角三角形；仰角与俯角应用；几何均值定理"),
    ("geometry", "Circles"): (
        "圆心角、弧、弦的关系；垂径定理；切线与半径垂直、切线长相等；"
        "圆周角定理与直径所对圆周角；圆内接四边形对角互补；"
        "弧长与扇形面积；内切圆与外接圆半径"),
    ("geometry", "Area and Perimeter"): (
        "矩形、平行四边形、三角形、梯形面积；圆的周长与面积；"
        "组合图形与阴影部分面积；面积比与相似比的关系；海伦公式；周长最值问题"),
    ("geometry", "Surface Area and Volume"): (
        "棱柱、棱锥、圆柱、圆锥、球的表面积；同类立体体积公式；"
        "相似立体表面积比 k²、体积比 k³；截面与展开图；体积的实际应用（容积、排水）"),

    # ---------------------------------------------------------- trigonometry
    ("trigonometry", "Angles and Radian Measure"): (
        "角度与弧度互化；弧长公式 s=rθ；扇形面积；"
        "标准位置角与参考角；终边相同的角；角速度与线速度"),
    ("trigonometry", "Trigonometric Functions"): (
        "六个三角函数的定义（直角三角形与单位圆两种口径）；"
        "各象限符号；特殊角的函数值；周期性；奇偶性"),
    ("trigonometry", "The Unit Circle"): (
        "单位圆上的坐标与 sin/cos 对应；特殊角坐标；"
        "用单位圆求任意角的函数值；对称关系（π−θ、π+θ、−θ）；反三角函数值域"),
    ("trigonometry", "Graphs of Trigonometric Functions"): (
        "正弦余弦图像的振幅、周期、相位、垂直位移；y=A sin(Bx+C)+D 作图；"
        "正切函数图像与渐近线；由图像写解析式；简谐运动建模"),
    ("trigonometry", "Identities and Equations"): (
        "同角基本关系（平方关系、商数关系）；和差角公式；二倍角与半角公式；"
        "和积互化；三角方程通解与区间解；证明恒等式"),
    ("trigonometry", "Inverse Trigonometric Functions"): (
        "arcsin/arccos/arctan 的定义域值域；反三角与三角的复合化简；"
        "反三角的加法关系；解含反三角的方程；实际角度求解"),
    ("trigonometry", "Law of Sines and Cosines"): (
        "正弦定理与余弦定理；解斜三角形的四种情形（SSS/SAS/ASA/AAS/SSA 多解）；"
        "三角形面积公式（1/2 ab sinC）；海伦公式；测量与导航应用"),
    ("trigonometry", "Vectors and Polar Coordinates"): (
        "向量加减与数乘；分量式与模长方向；点积与夹角；"
        "极坐标与直角坐标互化；极坐标曲线（圆、心形线、玫瑰线）；向量投影"),

    # -------------------------------------------------------------- calculus
    ("calculus", "Limits and Continuity"): (
        "极限的直观与 ε-δ 口径；单侧极限；无穷极限与渐近线；"
        "极限运算法则与夹逼定理；重要极限 sinx/x 与 (1+1/x)^x；连续性与间断点分类；介值定理"),
    ("calculus", "Derivatives"): (
        "导数定义与差商；幂指对三角函数的导数；乘积法则、商法则、链式法则；"
        "隐函数求导；高阶导数；切线与法线方程；相关变化率"),
    ("calculus", "Applications of Derivatives"): (
        "单调性与一阶导数符号；极值与临界点；凹凸性与二阶导数；"
        "洛必达法则；最值应用（优化问题）；中值定理；函数作图"),
    ("calculus", "Integrals"): (
        "定积分定义（黎曼和）与几何意义；微积分基本定理；"
        "不定积分与原函数；变上限积分求导；定积分性质与对称性"),
    ("calculus", "Techniques of Integration"): (
        "换元积分法；分部积分法；有理函数部分分式；三角代换；"
        "反常积分收敛判定；数值积分（梯形、辛普森）"),
    ("calculus", "Applications of Integration"): (
        "曲线间面积；旋转体体积（圆盘法、壳法）；弧长；"
        "旋转曲面面积；平均值；功与流体压力；质心"),
    ("calculus", "Sequences and Series"): (
        "数列极限与单调有界；级数收敛判别（比较、比值、根值、积分）；"
        "交错级数与绝对收敛；幂级数与收敛半径；泰勒级数与余项"),
    ("calculus", "Parametric Equations and Polar Coordinates"): (
        "参数方程求导与弧长；参数曲线围成面积；极坐标面积；"
        "极坐标弧长；圆锥曲线的极坐标方程"),
    ("calculus", "Vectors and Geometry of Space"): (
        "空间向量运算；点积与叉积；直线与平面方程；"
        "空间曲面（柱面、二次曲面）；距离与夹角计算"),
    ("calculus", "Partial Derivatives"): (
        "多元函数定义域与等值线；偏导数；全微分与可微性；"
        "链式法则；方向导数与梯度；拉格朗日乘数法；二元极值判定"),
    ("calculus", "Multiple Integrals"): (
        "二重积分与累次积分；极坐标下二重积分；三重积分；"
        "柱坐标与球坐标；雅可比；重积分的应用（体积、质心、质量）"),

    # ---------------------------------------------- differential-equations
    ("differential-equations", "First-Order Equations"): (
        "可分离变量方程；一阶线性方程与积分因子；恰当方程；"
        "齐次方程代换；伯努利方程；初值问题与解的存在唯一性；增长与冷却模型"),
    ("differential-equations", "Second-Order Linear Equations"): (
        "常系数齐次方程特征根三种情形；非齐次方程待定系数法；"
        "参数变易法；弹簧-阻尼系统的振动；受迫振动与共振"),
    ("differential-equations", "Systems of Linear Equations"): (
        "一阶线性方程组矩阵形式；特征值法求解；相平面与轨线；"
        "平衡点稳定性分类；非线性系统线性化"),
    ("differential-equations", "Laplace Transforms"): (
        "拉普拉斯变换定义与性质；常用变换对；逆变换与部分分式；"
        "用变换解初值问题；阶跃函数与卷积；传递函数"),
    ("differential-equations", "Series Solutions"): (
        "常点处的幂级数解；递推关系求系数；奇点与正则奇点；"
        "Frobenius 方法；Bessel 与 Legendre 方程入门"),
    ("differential-equations", "Numerical Methods"): (
        "欧拉法及其误差；改进欧拉法与 Runge-Kutta；"
        "步长与稳定性；线性多步法；刚性方程初步"),
    ("differential-equations", "Boundary Value Problems"): (
        "两点边值问题；特征值与特征函数；Sturm-Liouville 问题；"
        "热传导方程分离变量；波动方程与达朗贝尔解"),
    ("differential-equations", "Applications and Modeling"): (
        "人口与 Logistic 模型；电路 RLC 建模；"
        "混合问题与牛顿冷却；机械振动；建模步骤与量纲检查"),

    # ------------------------------------------------------------ statistics
    ("statistics", "Descriptive Statistics"): (
        "总体与样本；频数分布与直方图；均值中位数众数；"
        "极差、方差、标准差；四分位数与箱线图；偏度与异常值判定"),
    ("statistics", "Probability Basics"): (
        "样本空间与事件；概率公理与补事件；加法法则与互斥；"
        "条件概率与乘法法则；独立性与贝叶斯公式；计数原理"),
    ("statistics", "Discrete Random Variables"): (
        "离散分布律与累积分布；期望与方差；二项分布；"
        "泊松分布；几何与超几何分布；线性变换的期望方差"),
    ("statistics", "Continuous Random Variables"): (
        "概率密度与累积分布函数；均匀分布；正态分布与标准化 Z；"
        "指数分布；连续分布的期望方差；分位数"),
    ("statistics", "Sampling Distributions"): (
        "抽样方法与抽样误差；样本均值的分布；中心极限定理；"
        "t 分布；样本比例的分布；有限总体校正"),
    ("statistics", "Confidence Intervals"): (
        "均值的置信区间（σ 已知/未知）；比例的置信区间；"
        "样本量确定；误差边际；区间解释的常见误解"),
    ("statistics", "Hypothesis Testing"): (
        "原假设与备择假设；第一类/第二类错误与功效；"
        "单样本 z/t 检验；比例检验；p 值口径；单双侧检验"),
    ("statistics", "Correlation and Regression"): (
        "散点图与相关系数 r；最小二乘回归直线；斜率与截距解释；"
        "决定系数 R²；残差分析；相关不等于因果"),
    ("statistics", "Chi-Square and ANOVA"): (
        "卡方拟合优度检验；列联表独立性检验；单因素方差分析；"
        "F 统计量与组间组内平方和；多重比较；前提假设检查"),

    # ----------------------------------------------------------- survey-math
    ("survey-math", "Sets and Logic"): (
        "集合表示与运算（并交补、差集）；文氏图与计数；"
        "命题与真值表；蕴含与等价；德摩根律；逻辑推理有效性"),
    ("survey-math", "Number Theory Basics"): (
        "整除与质数；最大公约数最小公倍数；同余与模运算；"
        "进制转换；RSA 思想入门；斐波那契与黄金比"),
    ("survey-math", "Consumer Mathematics"): (
        "百分比与折扣；单利复利；贷款月供与摊销；"
        "税与小费；通货膨胀与购买力；预算与储蓄计划"),
    ("survey-math", "Geometry in Art"): (
        "对称与平移旋转反射；黄金分割与斐波那契矩形；"
        "镶嵌与正多边形；透视与比例；分形入门"),
    ("survey-math", "Probability and Statistics in Society"): (
        "用概率解读新闻数据；抽样偏差与误导性图表；"
        "期望值与风险；保险与彩票的期望；统计显著性的通俗解释"),
    ("survey-math", "Graph Theory and Networks"): (
        "图的顶点与边；欧拉路径与欧拉回路；哈密顿路径；"
        "树与生成树；最短路径（Dijkstra 思想）；网络与调度应用"),
}


def points_for(subject: str, unit: str) -> str:
    """这一单元的考点串；没有条目返回空串（调用方按只有标题降级）。"""
    return POINTS.get((str(subject or ""), str(unit or "")), "")


def covered() -> tuple[int, int]:
    """(已写条数, 涉及学科数) —— 给 CLI/自检用。"""
    subs = {s for (s, _u) in POINTS}
    return len(POINTS), len(subs)


if __name__ == "__main__":
    n, s = covered()
    print(f"考点表：{n} 个单元 / {s} 个学科")
    for (sub, unit), p in list(POINTS.items())[:2]:
        print(f"- {sub} · {unit}\n  {p}")
