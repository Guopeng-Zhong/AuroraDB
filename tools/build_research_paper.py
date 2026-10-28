"""Build the cited course paper from current source and measured evidence.

Research descriptions explicitly separate borrowed ideas, project adaptations,
bounded validation and unmeasured real-model quality. Never copy paper speedups
into AuroraDB's results. Source/evidence freshness is checked before publication.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.platypus import PageBreak,Spacer
from reportlab.lib.units import mm

from build_report import (ROOT,DOCS,CONTENT_W,p,heading,table,code,ReportDocument,CoverHero,
                          campus_evidence,campus_validation_content)


REFERENCES=[
    ('[1] Narasayya V, Chaudhuri S. Leveraging Query Optimizers to Verify the Soundness of LLM-based Query Rewrites for Real-World Workloads, and More! CIDR, 2026.',
     'https://www.cidrdb.org/cidr2026/papers/p33-narasayya.pdf'),
    ('[2] Fathollahzadeh S, Mansour E, Boehm M. ReSequel: Robust LLM-assisted Query Rewriting and Optimization using Templatization and Sampling. PVLDB, 19(10):2880-2893, 2026. DOI:10.14778/3828612.3828639.',
     'https://arxiv.org/abs/2606.20853'),
    ('[3] Zeng T, Lan J, Ma J, et al. PRICE: A Pretrained Model for Cross-Database Cardinality Estimation. PVLDB, 18(3):637-650, VLDB 2025. DOI:10.14778/3712221.3712231. Preprint first released 2024.',
     'https://github.com/StCarmen/PRICE'),
    ('[4] Li R, Zhao K, Yu J X, Wang G. CardOOD: robust query-driven cardinality estimation under out-of-distribution. The VLDB Journal, 35, article 28, 2026. DOI:10.1007/s00778-026-00979-3.',
     'https://link.springer.com/article/10.1007/s00778-026-00979-3'),
    ('[5] Khalaji M, Vanderkooy T, Guo R B, Daudjee K. Sampling-based Predictive Database Buffer Management. PVLDB, 18(13):5569-5581, 2025. DOI:10.14778/3773731.3773734.',
     'https://cs.uwaterloo.ca/~kdaudjee/Daudjee-BufferMan.pdf'),
    ('[6] Kumarasinghe U, Liu T, Mahmood A R, Liu C, Aref W G. iPDB -- Optimizing Semantic SQL Queries. arXiv:2601.16432v4, 2026. Preprint, not cited as an accepted journal paper.',
     'https://arxiv.org/abs/2601.16432'),
    ('[7] Chinook Database. Lerocha, official repository and MIT-licensed sample database; public table-size statistics from DemoDB Chinook downloads.',
     'https://github.com/lerocha/chinook-database'),
]


def evidence(name):
    value=json.loads((DOCS/name).read_text(encoding='utf-8'))
    if not value.get('passed'): raise ValueError(f'Experiment must pass: {name}')
    for path,digest in value.get('source_sha256',{}).items():
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest()!=digest:
            raise ValueError(f'Stale experiment evidence: {name}: {path}')
    return value


def page(story,number,title):
    story.extend([PageBreak()]+heading(number,title))


def build_story(test,optimizer,buffer,semantic,campus=None):
    story=[CoverHero('AuroraDB',['自研关系数据库与可信智能优化','版本 1.2.0 / 课程项目论文'],
                     [('4','研究扩展方向'),(str(test['tests_run']),'通过的自动化测试'),('7','研究与数据集引用')]),
           Spacer(1,10*mm),p('项目论文', 'h1'),
           p('题目：AuroraDB：融合可信查询优化、轻量学习、预测缓存与语义算子的自研关系数据库设计与实现'),
           p('摘要','h2'),
           p('本项目面向数据库原理课程，从 SQL 解析、关系执行、4096 字节磁盘页、B+ 树、缓冲管理与事务恢复出发，构建不依赖现成数据库引擎的关系数据库。'
             '在可观察的内核基础上，借鉴 2025-2026 年相关研究，增加带规则证据的查询改写实验、统计与轻量成本学习、扫描感知的预测淘汰策略，以及显式语义筛选、分类和聚合算子。'
             '系统通过普通 SQL 与研究实验分离、默认启发式/LRU、预算控制和回退保留确定性路径。实验数据由源码脚本生成，结果与限制逐项说明。'),
           p('关键词：关系数据库；查询优化；大语言模型；执行反馈；缓冲池；语义算子','small'),
           p('学术定位','h2'),p('创新点是自研内核上的系统集成、受约束实现与可复现实验。相关思想明确引用原论文；本项目不宣称发明 QO-Verify、PRICE、CardOOD 或复现其完整系统。校园 deepseek-chat 已完成小样本真实验收；批量/缓存对照仍用确定性假服务，通用模型质量未作统计评估。')]
    page(story,'01','问题与系统基础')
    story += [p('课程要求与实现','h2'),table(['组件','本项目实现'],[
        ['存储管理','4096 字节页；CRC；堆记录与稳定 RID；B+ 树点查/范围查询'],
        ['缓冲管理','有界 committed-page 缓存；默认 LRU；独立事务暂存'],
        ['查询处理','词法/递归下降语法解析；名称绑定；有限关系 SQL；可观察物理计划'],
        ['执行与事务','扫描、连接、分组排序；no-steal 页镜像 redo WAL；提交持久化与恢复'],
        ['用户界面','命令行与本机工作台；显示结果、计划、真实执行计数及研究实验'],
    ],[CONTENT_W*.24,CONTENT_W*.76]),
    p('研究问题','h2'),p('在单机教学内核、有限 SQL 子集和有限推理预算下，智能扩展能否提供可检查的正确性依据和真实收益？当数据分布、查询范围或热点变化时，如何检测方法的局限并回退？'),
    p('四项可验收贡献','h2'),p('（1）可信规则与外部候选分层验证，保留原查询回退和模板失效；（2）统计信息与实测访问成本学习接口；（3）可切换的扫描感知预测缓存；（4）批处理、去重、精确缓存和预算约束的语义算子管线。'),
    p('普通 SQL 保持独立：模型不可用时仍可读写数据库。研究端点只执行显式提交的只读实验，不修改数据文件；语义推理期间释放数据库锁。')]
    page(story,'02','相关研究与借鉴边界')
    story += [table(['引用','原研究重点','本项目的缩减实现'],[
        ['[1] QO-Verify','利用优化器逻辑变换集合证明部分 SQL 等价','限制在可信规则及其前置条件；没有 SQL Server 的 memo'],
        ['[2] ReSequel','元数据、模板、采样校验与候选性能排序','复用候选和真实执行反馈；不以有限数据比较代替通用证明'],
        ['[3] PRICE','跨数据库预训练基数估计','借鉴统计特征设计；实现轻量统计与访问成本模型，不加载原预训练模型'],
        ['[4] CardOOD','工作负载 OOD 与谓词包含的偏序约束','用变化的查询范围检验估计/回退；不宣称实现完整 OrderEmb'],
        ['[5] Predictive Buffer','扫描进度与小样本淘汰，包含并发扫描实验','串行扫描感知与热点保护；显式统计回退和策略开销'],
        ['[6] iPDB','SQL 内 ML/LLM 语义算子与优化','显式管线中的筛选、分类、聚合；无语义 JOIN 或扩展 SQL 语法'],
        ['[7] Chinook','公开音乐商店关系数据集与跨数据库分发','保留默认中文教学库；导入 11 表可运行子集，用于更真实的连接、聚合和索引演示'],
    ],[CONTENT_W*.22,CONTENT_W*.36,CONTENT_W*.42]),
    p('设计依据','h2'),p('QO-Verify [1] 提供了“规则证明”与“测试结果一致”的区分；ReSequel [2] 提醒我们把推理、验证与复用成本计入闭环。PRICE [3] 与 CardOOD [4] 为统计特征和泛化评估提供依据。预测缓冲研究 [5] 启发低开销候选采样；iPDB [6] 启发把模型判断变成有输入、输出和代价的关系算子。Chinook [7] 提供可公开核验的较大关系数据集，让课堂小数据与受控性能实验之间增加可复现的展示层。')]
    page(story,'02B','中文教学库与 Chinook 数据层')
    story += [p('为什么保留两种数据','h2'),
    p('中文教学库仍是默认启动路径，字段少、语义直观，适合讲解学生、课程、选课、NULL、事务和 AI 语义筛选。Chinook [7] 通过独立的 --dataset chinook 模式导入，不覆盖默认库；公开统计为 11 张表、约 15,607 行，其中 Track 3,503 行、PlaylistTrack 8,715 行、InvoiceLine 2,240 行。'),
    p('可运行边界','h2'),
    p('导入器只映射 AuroraDB 支持的 INTEGER、REAL、TEXT、BOOLEAN，并跳过视图、触发器、存储过程和 BLOB/媒体列。docs/chinook_import.json 保存来源、映射和每表行数；这使得“公开数据集模式”可复现，但不把上游数据库的全部功能写成 AuroraDB 已支持能力。'),
    code("# 独立目录，不影响中文教学库\ndist\\AuroraDB.exe --dataset chinook --web --port 0 --db data\\chinook\n\nSELECT ar.Name, COUNT(*) AS tracks\nFROM Artist ar JOIN Album al ON ar.ArtistId=al.ArtistId\nJOIN Track tr ON al.AlbumId=tr.AlbumId\nGROUP BY ar.Name ORDER BY tracks DESC;"),
    p('数据集与性能结论分层','h2'),
    p('Chinook 的静态行数用于展示多表关系和计划可观察性，不直接证明优化收益。5000/6000 行固定种子脚本仍负责索引、学习型优化和预测缓冲的定量结论；报告同时保留小表上的保守回退结果。')]
    page(story,'03','可信查询优化闭环')
    story += [p('流程与分级 [1][2]','h2'),p('原查询先通过语法、名称绑定与计划检查。本地规则产生可追溯候选；外部模型候选也先编译。原查询与候选在同一可见数据状态下执行，按结果与排序语义进行检查，再比较真实执行指标。规则证据、测试比较和性能判断分别记录。'),
    table(['验证级别','含义','采用边界'],[
        ['proven','受支持规则及前置条件覆盖的改写，有规则序列证据','仍需实际执行检查与性能门槛，满足条件才推荐手动替换'],
        ['tested','在当前数据上的有限执行比较通过','外部未知改写不自动推荐替换；不能推广到全部合法数据'],
        ['unknown','编译/语义范围不满足，或验证不足','保留原查询，展示原因'],
    ],[CONTENT_W*.18,CONTENT_W*.43,CONTENT_W*.39]),
    p('比较与失效','h2'),p('结果核对保留重复项与输出类型，NULL 与外连接必须按内核语义处理。LIMIT/OFFSET 等未覆盖边界拒绝实验；ORDER BY 需要可比较的输出排序键，同值组内按多重集合比较。相同 SQL/数据代次/模式/学习模型/缓冲策略可复用已验证的实测结论，并标记未重测。模板登记规则元信息和查询形状命中，目前不跳过候选构造；新参数仍重新执行比较。写入、事务状态、统计采集与策略切换触发失效。每路清空应用缓存，操作系统文件缓存保留，因此时间不是纯物理磁盘测量。'),
    p('模型调用','h2'),p('模型提出一个当前方言内的只读候选。请求仅含 SQL、表结构和计划，原 SQL 中的字面量可能发送。调用等待受网络总 deadline 约束，未结束的网络工作线程最多 2 个；解码后响应再次排密。网络请求与内核验证分开执行；服务端累计模型耗时与本地实验耗时，不把 SQL 执行加速等同于整体收益。'),
    code("POST /api/research/optimize\n{\"sql\":\"SELECT ...\",\"use_ai\":false}\n\nDatabase.optimize_query(sql, candidate_sql=None)")]
    page(story,'04','统计与轻量学习型优化')
    story += [p('统计特征与成本反馈 [3][4]','h2'),p('通过只读统计采集获得行数、NULL 数、不同值数量、范围与直方图。已有单列索引候选 RID 数量是准确计数，学习模型不替换该计数；模型学习的是执行代价，统计用于估计过滤和连接中间结果。运行时保持标准库，不要求 GPU 或外部训练服务。'),
    p('模式与回退','h2'),p('默认 heuristic 保留原访问路径选择。TableScan 与 IndexScan 各训练一个标准库 ridge 读路径成本模型，至少 8 个冷应用缓存样本且有 3 种不同特征向量才就绪；不估计完整查询耗时。adaptive 在训练特征范围内使用模型，否则回退启发式。过滤基数校准施加单调约束，连接基数使用 NDV 近似但不搜索连接顺序。状态公开样本、权重、训练误差与实验代次。写入、统计更新或缓冲策略切换使模型与优化结论失效。'),
    p('评估原则','h2'),p('估计误差与执行收益必须分开测量。更准确的行数估计不必然带来更快查询，因为页位置、缓存和模型调用开销也影响执行。借鉴 CardOOD [4] 的泛化问题设置不同查询范围；本项目没有跨数据库大模型预训练、完整连接重排或在线漂移理论保证。'),
    code("Database.analyze_statistics()\nDatabase.set_optimizer_mode('adaptive')\nDatabase.research_status()\nEXPLAIN ANALYZE SELECT ...;")]
    page(story,'05','预测式缓冲管理')
    story += [p('扫描感知的候选淘汰 [5]','h2'),p('缓存策略只作用于已提交页，保留事务暂存和 WAL 流程。堆表/B+ 树扫描提供局部扫描标签与下一页提示；从少量 resident 候选中选择淘汰页。结合受限频率、近期性与扫描信息保护热点，证据不足时回到 LRU。默认 LRU，预测策略可显式切换并恢复。'),
    p('开销和可观察性','h2'),p('resident 页数组维护采样集合；候选数量受 sample_size 与缓存大小限制。公开预测淘汰、采样、回退次数及策略记账墙钟时间，不是硬件 CPU 周期测量。历史元数据有界，不在持久页中增加预测状态。扫描上下文结束时通过 finally 释放注册；关闭 pager 也会清理仍被持有的扫描登记。'),
    p('串行边界','h2'),p('原论文 [5] 涉及并发长扫描及真实 Redshift/PostgreSQL 负载。本项目单连接串行，研究的是大扫描与热点点读交替的缓存污染，以及频率历史在热点迁移后的适应性。实验收益不能当作并发扫描论文的复现结果。'),
    code("Database.set_buffer_policy('predictive', sample_size=8)\npager.cache_policy_status()\nDatabase.set_buffer_policy('lru')")]
    page(story,'06','显式语义关系算子')
    story += [p('算子边界与计划 [6]','h2'),p('先由确定性 SQL 执行器物化一次提交态 SELECT，再把用户选择的 TEXT 字段交给语义管线。SemanticFilter 保留模型判为匹配的完整原行；SemanticProject 附加布尔判断与短标签；SemanticAggregate 对模型标签做确定性计数。计划显示语义算子与下层 SQL 计划。NULL 不发送模型，筛选时不匹配，聚合时归入 NULL 标签。'),
    p('批处理与精确缓存','h2'),p('同一运行内先按完整文本去重，再按批大小调用 Chat Completions。响应必须对每个输入 ID 恰好返回一个布尔判断和短标签；允许合法 ID 乱序并重排，缺失/重复/越界/错误类型拒绝。解码后的标签也执行密钥检查。协议不能证明模型为合法 ID 给出的语义判断正确。精确缓存的键包含文本、条件、模型与服务配置的摘要，内存容量默认 512，TTL 为 300 秒，不保存明文凭证。'),
    p('预算与一致性','h2'),p('默认 64 行、每批 8 条、最多 8 批，SQL 与推理总预算 30 秒。SQL 执行协作式检查 6 秒、扫描 10 万行、连接比较 20 万次、中间结果 2 万行；聚合产生小结果也不能绕过中间结果预算。规划和索引候选枚举耗时计入总时长，但不能强制中断所有规划工作，因此不是硬实时保证。每次 HTTP 使用总预算的剩余时间；失败不返回部分结果。活动事务拒绝语义实验；模型推理期间不持数据库锁，结果对应读取时物化的快照。普通 SQL 可继续执行。'),
    p('本地模式与远程模式','h2'),p('本地演示仅接受“关键词：词一|词二”的确定性规则，明确说明未调用大模型。远程模式仅发送所选文本与判断条件，不发送其他列、SQL 或表结构。模型判断是概率性标注，不能作为 SQL 等价证明。'),
    code("SELECT id,title FROM courses ORDER BY id;\n\nPOST /api/research/semantic\n{\"sql\":\"SELECT id,title FROM courses\",\"column\":\"title\",\n \"prompt\":\"关键词：数据库|索引\",\"operation\":\"filter\"}")]
    page(story,'07','实验设计与有效性')
    story += [table(['实验','对照与指标'],[
        ['可信改写','规则证据/外部候选；结果差异；推荐原因；缓存复用与写入失效；总实验耗时'],
        ['统计/成本学习','heuristic/adaptive；训练与未训练查询范围；估计/样本状态；页读取与耗时；回退'],
        ['缓存','LRU/predictive；四种负载；不同容量与采样；页读取/命中率/策略开销/阶段耗时'],
        ['语义管线','逐条与批量；冷/热精确缓存；调用次数；结果对应；NULL/重复项与预算'],
        ['回归','完整 SQL/存储/事务/HTTP/CLI；独立 SQLite 测试对照；可执行程序验收'],
    ],[CONTENT_W*.24,CONTENT_W*.76]),
    p('数据与度量','h2'),p('实验脚本建立临时数据库，固定生成方式与种子，JSON 保存参数、时间和源码摘要。页读取指数据库文件页读取次数，操作系统缓存保留。墙钟耗时包含 Python 与测试环境开销；预测策略的记账墙钟耗时也独立报告。'),
    p('有效性威胁','h2'),p('有限 SQL 子集、串行执行与小规模人工负载限制了外部有效性。有限数据比较不证明任意数据库上的 SQL 等价；学术论文中的结果只用于介绍方法，不作为本项目性能结论。批量/缓存对照使用确定性本机假服务及人为固定请求开销，只证明协议与管线机制；校园真实验收的单次时延另列，不代表系统性性能或通用模型质量。'),
    p(f"本次自动化回归：{test['tests_run']} 项，失败 {test['failures']}，错误 {test['errors']}。所有结果由 tools/verify.py 输出；不手填测试总数。")]
    page(story,'08','可信改写与学习实验结果')
    cases={c['name']:c for c in optimizer['cases']}
    case_names=[('trusted_or_index','OR→IN索引'),('null_duplicate_and','NULL/重复AND'),
                ('ordered_tie_bags','排序同值组'),('external_current_data_only','未知外部候选'),
                ('external_counterexample_after_insert','插入后的反例'),
                ('boolean_numeric_counterexample','布尔/数值反例'),('short_query_fallback','短查询回退')]
    story += [p('实测证据：docs/optimizer_research.json','h2'),
              p(f"{optimizer['rows']} 行、{optimizer['cache_pages']} 页缓存，{len(optimizer['cases'])} 个案例全部符合预期。下表为代表性正例与反例；全部原始输出、计数与试次均随 JSON 交付。"),
              table(['案例','验证','结果相同','推荐替换'],[[label,cases[key]['verification']['level'],
                    str(cases[key]['verification']['matched']),str(cases[key]['recommendation']['replace'])]
                    for key,label in case_names],[CONTENT_W*.37,CONTENT_W*.19,CONTENT_W*.22,CONTENT_W*.22])]
    trusted=cases['trusted_or_index']
    story += [p(f"OR→IN 原查询中位耗时 {trusted['original']['elapsed_ms']:.4f} ms，候选 {trusted['candidate']['elapsed_ms']:.4f} ms；扫描行数 {trusted['original']['stats']['rows_scanned']}→{trusted['candidate']['stats']['rows_scanned']}。该例依赖 IN 多探测访问路径，不能推广到任意模型改写。"),
              p(f"本次优化器闭环总成本 {trusted['timings']['total_ms']:.4f} ms，包含编译、结果核对和两路各 3 次执行。优化收益应按后续调用次数摊销；本次没有模型调用，不包含学校模型延迟。"),
              p('判定解释','h2'),p('候选结果一致、规则可证明与实测性能收益是三个独立条件。未知外部候选即使 tested，也不推荐替换。写入后的反例、重复项删除、布尔/数值混淆、外连接和排序变化都被拒绝或标明不可验证。'),
              p(f"相同版本结论缓存命中：本次请求 {cases['exact_same_version_cache']['timings']['total_ms']:.4f} ms，validation_ms=0，并保留旧实测时间；不声称重新做过验证。新字面值模板命中记录查询形状，仍构造候选并重新验证。",'small')]
    page(story,'08B','学习优化的留出结果')
    learning=optimizer['learning']; summary=learning['summary']
    def trend(field):
        before,after=summary['heuristic'][field],summary['adaptive'][field]
        return '下降' if after<before else ('上升' if after>before else '持平')
    story += [p(f"36 条训练查询；9 个未作为训练阈值的留出查询，每模式每条执行 3 次。成本模型就绪，访问路径改变 {summary['access_path_changes']} 次，全部结果以类型敏感多重集合核对一致。"),
              table(['度量','heuristic','adaptive'],[
                  ['平滑Q-error中位',summary['heuristic']['q_error_median'],summary['adaptive']['q_error_median']],
                  ['平滑Q-error P95',summary['heuristic']['q_error_p95'],summary['adaptive']['q_error_p95']],
                  ['9条查询中位耗时总和ms',summary['heuristic']['execution_ms_total'],summary['adaptive']['execution_ms_total']],
                  ['9个中位耗时的中位ms',summary['heuristic']['execution_ms_median'],summary['adaptive']['execution_ms_median']],
                  ['9个中位耗时的P95 ms',summary['heuristic']['execution_ms_p95'],summary['adaptive']['execution_ms_p95']],
              ],[CONTENT_W*.48,CONTENT_W*.26,CONTENT_W*.26]),
              p('误差口径','h2'),p('平滑 Q-error = max((actual+1)/(estimate+1), (estimate+1)/(actual+1))。加 1 处理零行边界，此数值不能直接与采用其他零值处理方式的论文表格比较。执行 P95 是 9 个逐查询中位耗时的分位数，并非服务请求尾延迟。'),
              p('收益与局限','h2'),p(f"当前留出范围中，总耗时{trend('execution_ms_total')}、中位耗时{trend('execution_ms_median')}，P95 {trend('execution_ms_p95')}。这些趋势只对应当前查询与测量环境。收益依赖查询选择性和数据页分布；更少扫描行不必然更快，离散 RID 索引访问可比顺序扫描更昂贵。候选枚举与估计开销保留在 estimation_ms，不能称为纯模型推理耗时。"),
              p(f"阈值单调性检查 {learning['monotonicity']['samples']} 个点，违规 {learning['monotonicity']['violations']}；这只验证当前单列范围，不证明任意分布泛化。原始 9×2×3 次测量与回退来源完整保存；没有跨数据库预训练，也没有学校模型参与。",'small')]
    page(story,'09','缓冲实验结果')
    story += [p('实测证据：docs/buffer_research.json','h2'),
              p(f"{len(buffer['runs'])} 次完整运行，{len(buffer['comparisons'])} 组配置对比；6000 行数据，缓存 8/24/64 页，采样 1/4/8/32，每种配置 3 次并随机化配置顺序。全部结果与固定基准一致。下表固定展示采样 8 的全部负载/容量，完整配置保留在 JSON。")]
    buffer_rows=[]
    names={'hot_points_with_large_scans':'热点+扫描','repeated_ranges':'重复范围','hotspot_migration':'热点迁移','pure_scans':'纯扫描'}
    for run in buffer['comparisons']:
        if run['sample_size']!=8: continue
        buffer_rows.append([names.get(run['workload'],run['workload']),run['capacity_pages'],
                            round(run['lru_mean_file_page_reads'],1),round(run['predictive_mean_file_page_reads'],1),
                            f"{run['file_read_reduction_pct']:.2f}%",
                            f"{run['predictive_mean_total_ms']/run['lru_mean_total_ms']:.3f}"])
    if buffer_rows:
        story += [table(['负载','容量页','LRU读页','预测读页','读页减少','耗时比'],buffer_rows,
                        [CONTENT_W*.21,CONTENT_W*.13,CONTENT_W*.17,CONTENT_W*.17,CONTENT_W*.17,CONTENT_W*.15])]
    best=max(r['file_read_reduction_pct'] for r in buffer['comparisons'])
    worst_time=max(r['predictive_mean_total_ms']/r['lru_mean_total_ms'] for r in buffer['comparisons'])
    story += [p(f"全部配置中最高读页减少 {best:.2f}%；重复范围与纯扫描没有读页收益。最差耗时比 {worst_time:.4f}，即约 {(worst_time-1)*100:.2f}% 增加，不能据读页下降宣称整体提速。耗时比为预测/LRU，大于 1 表示退化。")]
    story += [p('解释','h2'),p('热点点查夹大扫描、重复范围、热点迁移与纯扫描分别揭示策略对不同访问形态的行为。预测策略存在额外计算和元数据成本，纯扫描或信息不足时未必优于 LRU。全部查询结果必须一致，不能用错误结果换取缓存命中。'),
              p('完整参数、整次运行的操作 p50/p95、阶段页读取/耗时及策略开销保留在 JSON。没有量化迁移后的恢复时间。当前结果不是物理磁盘基准，也不是并发事务评测。')]
    page(story,'10','语义批处理与缓存实验结果')
    rows=[]
    for run in semantic['runs']:
        st=run['stats']
        rows.append([run['name'],run['batch_size'],st['model_calls'],st['cache_hits'],
                     st['elapsed_ms'],'一致' if run['fixture_agreement'] else '不同'])
    story += [table(['实验','批大小','HTTP调用','缓存命中','总耗时ms','假服务结果'],rows,
                    [CONTENT_W*.25,CONTENT_W*.11,CONTENT_W*.14,CONTENT_W*.14,CONTENT_W*.17,CONTENT_W*.19]),
              p(f"输入 {semantic['input_rows']} 行，完整文本去重后 {semantic['unique_texts']} 条。逐条请求、批量冷缓存和批量热缓存分别记录真实调用数。"),
              p('解释与边界','h2'),p('调用数下降来自同次运行去重、批处理和跨运行精确缓存。对应结果与预先定义的关键词假服务一致，证明行标识映射与管线处理正确。假服务每次请求人为等待 3ms，因此这里的速度差异不能代表 DeepSeek 或校园模型性能。'),
              p('校园接口已完成独立小样本实连，见下一页；本页假服务数据不能代表校园模型性能。仍应使用更大人工标注集比较精确率/召回率、标签一致性、推理延迟分布及缓存有效期。')]
    page(story,'10B','校园模型接入与真实验收')
    story += campus_validation_content(campus)
    page(story,'11','创新点、复现与后续研究')
    story += [p('可验证的课程创新点','h2'),p('四条路线共用同一自研内核，使查询建议、规则证据、访问代价、缓存行为和语义处理的输入输出可观察。接口保留启发式/LRU 对照，实验记录真实计数、预算及失效，形成从研究思想到可运行系统的工程贡献。'),
    p('复现顺序','h2'),code('python tools/verify.py\npython tools/optimizer_experiment.py\npython tools/buffer_experiment.py\npython tools/semantic_experiment.py\npython tools/build_exe.py\npython tools/executable_smoke.py\npython tools/research_executable_smoke.py\npython tools/build_report.py\npython tools/build_research_paper.py\npython tools/package_submission.py'),
    p('工作台入口','h2'),p('辅助工具中的“研究实验”提供统计采集、优化模式、缓存策略、可信改写和语义管线。启动工作台.cmd 在 ai.local.json 存在时自动加载，无配置时禁用；本地 AI 演示便于离线验收。学校基址为 https://models.sjtu.edu.cn/api/v1，默认 deepseek-chat。授权邮件还列 deepseek-reasoner、qwen / qwen3.8-27b，未逐一验收。配置与密钥不打包。'),
    p('后续研究','h2'),p('扩展可信规则和证明覆盖；加入更丰富的查询模板及跨分布留出集；用更大标注集测量校园模型语义质量与预算取舍；在未来多会话内核中研究并发扫描预测。外键、复合索引、子查询、MVCC、磁盘溢写及语义连接仍属于未实现范围。')]
    page(story,'12','参考文献')
    for title,url in REFERENCES:
        story += [p(escape(title)),p(f'<link href="{escape(url)}" color="#008a91">{escape(url)}</link>','small'),Spacer(1,2*mm)]
    story += [p('引用使用说明','h2'),p('正文 [1]-[6] 对应规则证明、模板与反馈、统计特征、泛化约束、预测缓存和语义算子；[7] 对应 Chinook 公开数据集来源与规模。所有原论文方法与实验均归于原作者；AuroraDB 的参数和实验只引用本项目 JSON 证据。引用核对日期：2026-10-25。')]
    return story


def main():
    test=evidence('test_results.json')
    optimizer=evidence('optimizer_research.json')
    buffer=evidence('buffer_research.json')
    semantic=evidence('semantic_research.json')
    path=DOCS/'AuroraDB_项目论文.pdf'
    campus=campus_evidence()
    ReportDocument(path,'项目论文').build(build_story(test,optimizer,buffer,semantic,campus))
    print(json.dumps({'paper':str(path),'bytes':path.stat().st_size},ensure_ascii=False))


if __name__=='__main__': main()
