"""Build the two Chinese submission PDFs from actual project evidence.

Run from the project root: python tools/build_report.py
Runtime PDF dependency: reportlab. Figures are native vectors; screenshots are
captured from the real workbench. Test counts and timings come from JSON files.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Preformatted, Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN
NAVY = colors.HexColor("#112439")
INK = colors.HexColor("#24374b")
MUTED = colors.HexColor("#687b90")
TEAL = colors.HexColor("#008a91")
GOLD = colors.HexColor("#c38c35")
PALE = colors.HexColor("#edf4f7")
LINE = colors.HexColor("#d7e3eb")


def register_fonts():
    custom = os.environ.get("AURORADB_CJK_FONT")
    candidates = [Path(custom)] if custom else []
    candidates += [Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simsun.ttc"),
                   Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")]
    for font in candidates:
        if font.is_file():
            pdfmetrics.registerFont(TTFont("AuroraCJK", str(font), subfontIndex=0))
            bold = Path("C:/Windows/Fonts/msyhbd.ttc")
            pdfmetrics.registerFont(TTFont("AuroraCJKBold", str(bold if bold.exists() else font), subfontIndex=0))
            pdfmetrics.registerFontFamily("AuroraCJK", normal="AuroraCJK", bold="AuroraCJKBold")
            return
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdfmetrics.registerFontFamily("STSong-Light", normal="STSong-Light", bold="STSong-Light")
    global FONT, BOLD
    FONT = BOLD = "STSong-Light"


FONT, BOLD = "AuroraCJK", "AuroraCJKBold"
register_fonts()
STYLES = {
    "body": ParagraphStyle("body", fontName=FONT, fontSize=9.4, leading=15.7,
                           textColor=INK, spaceAfter=7, wordWrap="CJK"),
    "small": ParagraphStyle("small", fontName=FONT, fontSize=8.1, leading=13,
                            textColor=MUTED, spaceAfter=5, wordWrap="CJK"),
    "h1": ParagraphStyle("h1", fontName=BOLD, fontSize=20, leading=27,
                         textColor=NAVY, spaceAfter=12, wordWrap="CJK"),
    "h2": ParagraphStyle("h2", fontName=BOLD, fontSize=11.2, leading=17,
                         textColor=TEAL, spaceBefore=8, spaceAfter=5, wordWrap="CJK"),
    "cell": ParagraphStyle("cell", fontName=FONT, fontSize=8.2, leading=12.9,
                           textColor=INK, wordWrap="CJK"),
    "headcell": ParagraphStyle("headcell", fontName=BOLD, fontSize=8.3, leading=13,
                               textColor=colors.white, wordWrap="CJK"),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=8.0, leading=12.3,
                           textColor=NAVY, leftIndent=10, rightIndent=10,
                           borderColor=LINE, borderWidth=.5, borderPadding=10,
                           backColor=PALE, spaceBefore=5, spaceAfter=10),
}


def p(text, style="body"):
    return Paragraph(text, STYLES[style])


def heading(number, title, subtitle=None):
    result = [p(f"{number} / AURORADB", "small"), p(title, "h1")]
    if subtitle:
        result.append(p(subtitle, "small"))
    return result


def table(headers, rows, widths=None):
    cells = [[p(escape(str(x)), "headcell") for x in headers]]
    cells.extend([[p(escape(str(x)), "cell") for x in row] for row in rows])
    t = Table(cells, colWidths=widths or [CONTENT_W / len(headers)] * len(headers),
              repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PALE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LINEBELOW", (0, 0), (-1, 0), .4, NAVY),
        ("LINEBELOW", (0, 1), (-1, -1), .3, LINE),
    ]))
    return t


def code(text):
    style = STYLES["code"]
    if any(ord(char) > 127 for char in text):
        style = ParagraphStyle("cjkcode", parent=style, fontName=FONT)
    return Preformatted(text.strip("\n"), style, maxLineLength=83,
                        splitChars=" ,", newLineChars="  ")


def figure(filename, caption, max_height=78 * mm, crop_top=0, crop_box=None):
    candidates = [DOCS / filename, DOCS / Path(filename).with_suffix(".jpg"),
                  DOCS / Path(filename).with_suffix(".png")]
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"Real workbench screenshot required: {filename}")
    from PIL import Image as PILImage
    image_source = str(path)
    with PILImage.open(path) as source:
        if crop_top or crop_box:
            from io import BytesIO
            box = (tuple(int(value * (source.width if i % 2 == 0 else source.height))
                         for i, value in enumerate(crop_box)) if crop_box else
                   (0, int(source.height * crop_top), source.width, source.height))
            source = source.crop(box)
            image_source = BytesIO()
            source.save(image_source, format="PNG")
            image_source.seek(0)
        width, height = source.size
    scale = min(CONTENT_W / width, max_height / height)
    return [Image(image_source, width * scale, height * scale, hAlign="LEFT"),
            Spacer(1, 4), p(caption, "small")]


class Diagram(Flowable):
    def __init__(self, kind, height=170):
        super().__init__()
        self.width, self.height, self.kind = CONTENT_W, height, kind

    def box(self, x, y, w, h, title, sub="", fill=PALE):
        c = self.canv
        c.setFillColor(fill)
        c.setStrokeColor(LINE)
        c.roundRect(x, y, w, h, 6, fill=1, stroke=1)
        c.setFillColor(NAVY)
        c.setFont(BOLD, 9)
        c.drawCentredString(x + w / 2, y + h / 2 + (4 if sub else -3), title)
        if sub:
            c.setFont(FONT, 7.2)
            c.setFillColor(MUTED)
            c.drawCentredString(x + w / 2, y + h / 2 - 10, sub)

    def arrow(self, x1, y1, x2, y2, label=None):
        c = self.canv
        c.setStrokeColor(TEAL)
        c.setFillColor(TEAL)
        c.setLineWidth(1)
        c.line(x1, y1, x2, y2)
        import math
        angle = math.atan2(y2 - y1, x2 - x1)
        path = c.beginPath()
        path.moveTo(x2, y2)
        path.lineTo(x2 - 5 * math.cos(angle - .45), y2 - 5 * math.sin(angle - .45))
        path.lineTo(x2 - 5 * math.cos(angle + .45), y2 - 5 * math.sin(angle + .45))
        path.close()
        c.drawPath(path, fill=1, stroke=0)
        if label:
            c.setFont(FONT, 7)
            c.setFillColor(MUTED)
            c.drawString((x1 + x2) / 2 + 4, (y1 + y2) / 2 + 3, label)

    def draw(self):
        w, c = self.width, self.canv
        if self.kind == "architecture":
            bw = 125
            x = (w - bw) / 2
            self.box(x, 140, bw, 38, "Web / CLI", "接收 SQL 与显示结果")
            self.box(x, 88, bw, 38, "查询处理器", "词法 / 语法 / 绑定 / 计划")
            self.box(x, 36, bw, 38, "执行引擎", "扫描 / 连接 / 分组 / 排序")
            self.box(8, 36, 130, 38, "Catalog 元数据", "schema / indexes / root")
            self.box(w - 138, 36, 130, 38, "Buffer + DiskPager", "LRU / Heap / B+ / WAL")
            self.arrow(x + bw / 2, 140, x + bw / 2, 126)
            self.arrow(x + bw / 2, 88, x + bw / 2, 74)
            self.arrow(138, 55, x, 55)
            self.arrow(x + bw, 55, w - 138, 55)
            c.setFont(FONT, 7.6)
            c.setFillColor(MUTED)
            c.drawCentredString(w / 2, 12, "SQL 语义由自研引擎解释；持久化读写均经过分页存储层")
        elif self.kind == "page":
            specs = [("Magic", "8 bytes", 95), ("Page ID", "4 bytes", 85),
                     ("Length", "4 bytes", 85), ("CRC32", "4 bytes", 85),
                     ("JSON + padding", "4076 bytes", w - 350)]
            x = 0
            for title, sub, width in specs:
                self.box(x, 94, width - 2, 46, title, sub)
                x += width
            self.box(18, 26, 130, 42, "Page 0 manifest", "目录分片页清单")
            self.box((w - 130) / 2, 26, 130, 42, "Heap pages", "slots / forward RID")
            self.box(w - 148, 26, 130, 42, "B+ tree pages", "internal / linked leaves")
        elif self.kind == "tree":
            self.box(w / 2 - 66, 134, 132, 38, "Internal root", "separator: (key, RID)")
            centers = [w / 6, w / 2, 5 * w / 6]
            for i, x in enumerate(centers):
                self.box(x - 66, 48, 132, 45, f"Leaf {i + 1}", "ordered (key, RID) entries")
                self.arrow(w / 2, 134, x, 93)
                if i:
                    self.arrow(centers[i - 1] + 66, 68, x - 66, 68)
            c.setFont(FONT, 8)
            c.setFillColor(MUTED)
            c.drawCentredString(w / 2, 21, "查找沿树下降；范围查询沿 next 叶链前进；重复键由 RID 排序")
        elif self.kind == "wal":
            labels = [("1. staging", "未提交页仅驻留内存"), ("2. WAL fsync", "BEGIN + images + COMMIT"),
                      ("3. data fsync", "应用完整页映像"), ("4. clear WAL", "提交完成后清理")]
            bw = (w - 30) / 4
            for i, (title, sub) in enumerate(labels):
                self.box(i * (bw + 10), 48, bw, 55, title, sub)
                if i < 3:
                    self.arrow(i * (bw + 10) + bw, 76, (i + 1) * (bw + 10), 76)
            c.setFillColor(GOLD)
            c.setFont(FONT, 8)
            c.drawCentredString(w / 2, 16, "完整提交 WAL 是恢复依据；恢复重复应用页映像具备幂等性")
        elif self.kind == "ai":
            bw = (w - 20) / 3
            labels = [("工作台输入", "问题 / SQL / 表结构 / 计划"),
                      ("AIService", "兼容请求 / 限时 / 解析"),
                      ("学校模型 API", "DeepSeek 等兼容服务")]
            for i, (title, sub) in enumerate(labels):
                self.box(i * (bw + 10), 93, bw, 49, title, sub)
                if i < 2: self.arrow(i * (bw + 10) + bw, 117, (i + 1) * (bw + 10), 117)
            lower_w = (w - 36) / 2
            self.box(0, 16, lower_w, 48, "自研内核编译校验", "单个 SELECT / 名称绑定 / 计划")
            self.box(w - lower_w, 16, lower_w, 48, "用户审阅并选择", "填入编辑器后手动执行")
            self.arrow(w / 2, 93, lower_w / 2, 64)
            self.arrow(lower_w, 40, w - lower_w, 40)


class BenchmarkBars(Flowable):
    def __init__(self, scan_rows, index_rows):
        super().__init__()
        self.width, self.height = CONTENT_W, 92
        self.scan_rows, self.index_rows = scan_rows, index_rows

    def draw(self):
        c, start, usable = self.canv, 115, self.width - 170
        for y, name, value, color in [(58, "TableScan", self.scan_rows, MUTED),
                                      (22, "IndexScan", self.index_rows, TEAL)]:
            c.setFont("Helvetica", 9)
            c.setFillColor(INK)
            c.drawString(0, y + 3, name)
            c.setFillColor(color)
            c.roundRect(start, y, max(2.5, usable * value / self.scan_rows), 14, 3, fill=1, stroke=0)
            c.setFillColor(INK)
            c.drawString(start + usable + 10, y + 3, f"{value:,}")


class CoverHero(Flowable):
    def __init__(self, title, subtitle, metrics):
        super().__init__()
        self.width, self.height = CONTENT_W, 285
        self.title, self.subtitle, self.metrics = title, subtitle, metrics

    def draw(self):
        c, w = self.canv, self.width
        c.setFillColor(NAVY)
        c.roundRect(0, 0, w, self.height, 10, fill=1, stroke=0)
        c.setFillColor(TEAL)
        c.rect(0, 238, 6, 36, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#8bd9dc"))
        c.setFont("Helvetica-Bold", 11)
        c.drawString(24, 249, "AURORADB  /  DATABASE SYSTEM")
        c.setFillColor(colors.white)
        c.setFont(BOLD, 25)
        c.drawString(24, 195, self.title)
        c.setFont(FONT, 10)
        for i, text in enumerate(self.subtitle):
            c.drawString(24, 164 - i * 20, text)
        bw = (w - 48) / 3
        for i, (value, label_text) in enumerate(self.metrics):
            x = 24 + i * bw
            c.setFillColor(colors.HexColor("#8bd9dc"))
            c.setFont("Helvetica-Bold", 25)
            c.drawString(x, 66, str(value))
            c.setFont(FONT, 8.8)
            c.setFillColor(colors.HexColor("#cbd9e4"))
            c.drawString(x, 43, label_text)


class ReportDocument(BaseDocTemplate):
    def __init__(self, path, name):
        super().__init__(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=22 * mm, bottomMargin=20 * mm,
                         title=f"AuroraDB {name}", author="AuroraDB 项目", subject="数据库原理课程大作业方案一")
        self.name = name
        frame = Frame(MARGIN, 20 * mm, CONTENT_W, PAGE_H - 42 * mm, id="content",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates(PageTemplate(id="main", frames=frame, onPage=self.header_footer))

    def header_footer(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 8)
        canvas.setFillColor(TEAL)
        canvas.drawString(MARGIN, PAGE_H - 12 * mm, "AURORADB")
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 12 * mm, self.name)
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN, PAGE_H - 15 * mm, PAGE_W - MARGIN, PAGE_H - 15 * mm)
        canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
        canvas.setFont(FONT, 7)
        canvas.drawString(MARGIN, 9 * mm, "数据库原理 · 大作业方案一 · 2026-10-23")
        canvas.drawRightString(PAGE_W - MARGIN, 9 * mm, f"{doc.page:02d}")
        canvas.restoreState()


def campus_evidence():
    """Optional real-model smoke evidence; never inspect private AI config."""
    path = DOCS / "campus_api_smoke.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    checks = value.get("checks")
    sources = value.get("source_sha256")
    if (not value.get("passed") or not isinstance(checks, list) or not checks
            or any(not check.get("passed") for check in checks)
            or not isinstance(sources, dict) or not sources):
        raise ValueError("Reports require passing campus API evidence with source hashes")
    for name, digest in sources.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Stale campus API evidence: {name}")
    exe = ROOT / "dist" / "AuroraDB.exe"
    if (not exe.is_file() or value.get("exe_sha256") != hashlib.sha256(exe.read_bytes()).hexdigest()):
        raise ValueError("Campus API evidence must match the packaged executable")
    return value


def campus_validation_content(campus):
    """One compact page of measured campus acceptance, not quality claims."""
    if campus is None:
        return [p("当前构建未附校园接口实连证据；不能据此宣称已测模型质量或真实推理延迟。")]
    labels = ["远程配置与密钥隔离", "生成 SQL 与已知结果", "解释 SQL 与只读校验",
              "可信改写本地验证", "语义筛选及热缓存", "数据与调用状态"]
    measured_at = datetime.fromisoformat(campus["generated_at"]).astimezone(
        timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（UTC+8）")
    rows = [[labels[i] if i < len(labels) else item["name"],
             f"{item['elapsed_seconds']:.3f}", "通过"]
            for i, item in enumerate(campus["checks"])]
    observations = {item["name"]: item.get("observed", {}) for item in campus["checks"]}
    optimized = observations["real model candidate passes the local trusted optimizer pipeline"]
    semantic = observations["real semantic filter and warm-cache reuse match the labelled fixture"]
    configured_timeout = float(campus.get("configured_timeout_seconds", 90))
    return [p("真实校园模型的小样本验收", "h2"),
            p(f"{measured_at}，打包 EXE 使用 {escape(campus['model'])}，"
              f"{len(campus['checks'])} 项检查全部通过，计划并完成 {campus['planned_remote_calls']} 次真实远程推理，配置等待上限 {configured_timeout:g} 秒。"
              "仅发送人工构造的课程示例，不发送用户数据库。数据来自 docs/campus_api_smoke.json，并校验源码和 EXE 摘要。"),
            table(["验收项", "单次端到端秒", "结果"], rows,
                  [CONTENT_W*.56, CONTENT_W*.27, CONTENT_W*.17]),
            p("结果解释", "h2"),
            p("生成查询经本地编译且与已知样例结果一致；解释返回有效只读查询。"
              f"模型候选在当前样例上结果一致，验证级别 {escape(str(optimized['verification_level']))}，"
              f"recommended={str(optimized['recommended']).lower()}；结果一致、规则证据与性能建议分别判断。"
              f"语义筛选与已标注样例一致，再运行为 {semantic['warm_calls']} 次模型调用、{semantic['warm_cache_hits']} 次缓存命中。"
              "这验证远程调用与本地检查闭环，没有把模型候选直接执行为写操作。"),
            p("计量边界", "h2"),
            p("表中是一次功能验收的端到端墙钟时间，包含本地编译、验证及缓存复用等操作，不是纯模型推理延迟或统计性性能基准。"
              "本轮先前在 90 秒配置下观察到一次 SQL 解释请求超时，原失败记录单独保留；增加等待上限不是服务稳定性的证明。"
              "少量成功不代表长期可用或通用准确率；其他模型、长时吞吐、token 额度与实际费用未测。"
              "原批量/缓存实验仍使用确定性假服务，不能与这里的校园单次时延混合计算加速比。"),
            p("运行约束", "h2"),
            p(f"校园配置使用每分钟 {campus['requests_per_minute']} 次请求上限；工作台进程内共享计数，遇 HTTP 429 记录冷却并提示重试。"
              "本地限流不跨进程或设备共享，组员共用同一凭证时仍需协调学校总额度。在校内网络或获准的校园 VPN 环境使用。"
              "密钥只保存在本地配置或环境变量，不写入页面、证据和提交包。", "small")]


def report(test, bench, smoke, audit=None, ui=None, campus=None):
    n = test["tests_run"]
    numeric_smoke_note = ("；数值核验还检查 ROUND 正负半值及超大有限 AVG"
                          if any("Decimal" in check for check in smoke["checks"]) else "")
    counts = Counter(case["test"].split(".")[1] for case in test["cases"])
    if sum(counts.values()) != n:
        raise ValueError("Per-module test evidence must add up to the total test count")
    test_groups = {
        "test_sql": ("SQL 前端", "优先级、注释/引号、错误位置、不支持语法、深度边界"),
        "test_engine": ("关系引擎", "CRUD、约束、空表绑定、连接、聚合、NULL、事务和持久化"),
        "test_regressions": ("语义回归", "类型敏感 DISTINCT/GROUP/JOIN/IN、排序绑定、数值函数、NULL 与语法回滚"),
        "test_server": ("Web / CLI", "会话/Host/Origin、严格 JSON/UTF-8 与字段白名单、REPL、示例幂等"),
        "test_storage": ("分页与索引", "LRU、CRC、重复键、随机范围、转发、锁、真实进程崩溃"),
        "test_integration_storage": ("存储集成", "多页目录迁移、跨模块原子恢复、转发索引与提交故障"),
        "test_ai": ("AI 协议与边界", "兼容请求、总 deadline/网络线程上限、解码后密钥保护、限流与结果格式"),
        "test_ai_deadlines": ("网络时限回归", "慢 DNS/连接/响应头/滴流、到期取消与有界后台线程"),
        "test_integrity": ("全库一致性", "CRC 与逻辑损坏、堆/索引双向核对、循环、转发、约束和完全只读"),
        "test_diagnostics": ("诊断执行", "强制扫描/自动计划实际结果比较、SQL CHECK、失败时保留活动事务"),
        "test_advanced_server": ("增强 HTTP", "AI/诊断接口、编译校验、来源控制、活动事务及服务端上下文边界"),
        "test_ai_integration": ("兼容 API 集成", "真实本地兼容服务、请求体与 Bearer 鉴权、结构/计划上下文和临时启动配置"),
        "test_submission": ("提交包排密", "排除个人配置/密钥及任意目录数据库文件，保留公开示例和交付资产"),
        "test_research_optimizer": ("可信与学习优化", "规则证据、结果类型/排序、模板失效、统计、学习与分布回退"),
        "test_predictive_buffer": ("预测缓冲", "有界采样、热点与扫描、策略回退、事务隔离及注册释放"),
        "test_semantic": ("语义算子", "批处理、TTL、字段隔离、预算、协议类型、并发快照与 HTTP"),
        "test_research_audit": ("独立研究审计", "跨模块事务、候选验证、推理前检查和资源边界"),
    }
    core_modules = list(test_groups)[:6]
    extension_modules = [module for module in counts if module not in core_modules]
    def test_rows(modules):
        return [[test_groups.get(module,(module,"见对应测试源码及完整个案记录"))[0], counts[module],
                 test_groups.get(module,(module,"见对应测试源码及完整个案记录"))[1]] for module in modules]
    scan, index = bench["table_scan"]["cold"], bench["index_scan"]["cold"]
    warm_scan, warm_index = bench["table_scan"]["warm"], bench["index_scan"]["warm"]
    story = [Spacer(1, 20), CoverHero("设计与测试报告", ["自研关系数据库与可验证的诊断能力", "版本 1.2.0 / 软件工程过程与真实验证证据"],
             [("4096 B", "固定页格式"), (str(n), "自动化测试通过"), (f"{bench['scanned_row_reduction']:.0f}x", "基准堆记录扫描减少")]),
             Spacer(1, 20), p("项目摘要", "h2"),
              p("AuroraDB 实现了一个面向教学与单机实验的关系数据库管理系统。自行实现 SQL 前端、语义绑定、物理计划、关系执行、分页堆、持久化 B+ 树、缓冲与页映像 WAL，提供中文 Web 工作台及命令行。1.2.0 增加可信查询改写、统计与轻量学习、预测缓存及语义算子。SQL 的编译和执行始终由自研内核完成；运行时仅使用 Python 标准库。"),
             p(f"本报告依据源代码、{n} 项自动化测试、180 次固定种子 SQLite 差分查询，以及 {bench['rows']:,} 行索引实验编写。测试总耗时 {test['duration_seconds']:.3f} 秒，失败 {test['failures']}，错误 {test['errors']}。计时只反映本次运行环境。"),
             p("交付组成：源代码与可执行程序、使用说明、模块实现设计、测试与基准证据、实际界面截图及独立的分工填写表。组员姓名和个人完成情况未提供，因此不在报告中编造。", "small")]

    story += [PageBreak()] + heading("01", "选题与验收目标", "方案一要求与可检查证据逐项对应")
    story += [p("在三种题型中，方案一要求直接实现数据库内部组件，技术深度和模块间一致性风险最大。方案二偏向应用系统工程，方案三偏向专题研究与案例实现。本项目选择方案一，以可运行的数据库内核和故障恢复证据支撑技术难度。"),
              table(["课程要求", "实现位置", "验收证据"], [
                  ["关系数据模型的磁盘存储与高效访问", "storage.py / HeapTable / BPlusTree", "多页持久化、重复键范围查询、索引增删与重启测试"],
                  ["内存缓冲与磁盘 I/O 管理", "DiskPager / LRU / staged pages", "缓存容量与淘汰统计；no-steal 隔离与回滚测试"],
                  ["SQL 编译、计划生成与优化", "sql.py / engine.py", "定位语法错误、语义绑定、EXPLAIN、扫描路径比较"],
                  ["查询计划执行", "engine.py / evaluate / _select", "连接、聚合、NULL 逻辑、180 次结果差分"],
                  ["接收 SQL 并显示返回结果", "server.py / web / __main__.py", "实际工作台截图、CLI 与 HTTP 接口测试"],
                  ["源码、程序、说明、设计、测试与分工", "项目根目录 / docs / tools", "本报告、使用说明、证据 JSON、分工填写表"],
              ], [CONTENT_W * .30, CONTENT_W * .29, CONTENT_W * .41]),
              p("验收原则", "h2"),
              p("所有声明必须在代码、测试或现场演示中找到对应证据。展示的是有限 SQL 子集，避免以完整 SQL 标准、商业数据库级并发或绝对满分作为未经证实的结论。"),
              p("建议验收顺序：程序启动 → CRUD 与约束 → 多表聚合 → 计划与缓存 → 回滚与重启 → 自动化验证。", "small")]

    story += [PageBreak()] + heading("02", "系统架构与模块边界")
    story += [Diagram("architecture", 190), table(["模块", "职责", "核心接口"], [
        ["sql.py", "tokenize → 递归下降 AST；禁止 eval", "parse(sql) → statements"],
        ["engine.py", "目录、绑定、约束、优化、算子与事务边界", "Database.execute / schema / stats"],
        ["storage.py", "CRC 页、WAL、缓存、分页堆与 B+ 树", "DiskPager / HeapTable / BPlusTree"],
        ["integrity.py", "只读页/目录/堆/约束/索引一致性诊断", "check_database(db)"],
        ["ai.py", "可选模型 API；建议解析与配置保护", "AIService.assist / status"],
        ["server.py + web", "本机会话、编辑、结果、AI 与诊断", "POST query / integrity / compare / ai"],
        ["__main__.py", "CLI、UTF-8 文件执行与工作台启动", "python -m auroradb"],
        ["tests + tools", "断言、差分、崩溃注入与可复现证据", "verify.py / benchmark.py"],
    ], [CONTENT_W * .25, CONTENT_W * .43, CONTENT_W * .32]),
    p("模块契约与软件工程组织", "h2"),
    p("模块 API 固定 AST、表元数据、页管理、堆、索引及返回结果结构。SQL 前端不触碰磁盘，存储层不解释 SQL，界面只通过 Database API 使用引擎。接口边界使词法、存储和关系语义可以独立验证，再以集成测试检查跨模块不变量。")]

    story += [PageBreak()] + heading("03", "磁盘页格式与关系存储")
    story += [Diagram("page", 156), p("页格式", "h2"),
              p("每页固定 4096 字节。20 字节页头包含 8 字节 magic、4 字节 page id、4 字节有效载荷长度与 4 字节 CRC32；其余 4076 字节保存 UTF-8 JSON 及零填充。CRC 覆盖页标识、长度和整个填充后页体，能检测有效载荷、关键头字段及填充损坏。"),
              table(["持久化文件", "含义", "设计目的"], [
                  ["pages.dat", "目录、heap 和 B+ tree 的固定页文件", "按 page_id × 4096 定位"],
                  ["redo.wal", "单个事务的完整页映像日志", "先持久化日志再写数据页；恢复幂等"],
                  ["writer.lock", "操作系统级排他文件锁", "拒绝第二个进程同时打开同一数据库写入"],
              ], [CONTENT_W * .23, CONTENT_W * .37, CONTENT_W * .40]),
              p("关系元数据与记录身份", "h2"),
              p("Page 0 保存 catalog_root manifest；逻辑目录的 UTF-8 JSON 以 2900 原始字节分片并 Base64 编码后写入 catalog_chunk 页。目录含列定义、约束、堆页列表、行数和索引根，全部分片与堆/索引同事务提交；75 张表跨页目录已有测试。旧单页目录在下一次写入时原子迁移。"),
              p("堆记录以 [heap_page_id, slot_id] 作为稳定 RID。增大更新可存入 overflow 页，原槽留转发指针，索引身份不变；删除后槽位可以复用，因此 RID 不是永不复用的业务主键。"),
              p("容量边界", "h2"),
              p("单页有效载荷不超过 4076 字节，单个索引键上限为 1024 编码字节。过大记录或键在变更前失败。目录可跨页，但根 manifest 自身仍需装入一个页，目录分片数量因此有限；没有宣称无限容量。", "small")]

    story += [PageBreak()] + heading("04", "缓冲池与分页堆算法")
    story += [p("LRU 读缓存", "h2"), p("DiskPager 用 OrderedDict 保存已提交页的深拷贝。读请求先查事务 staging，再查 LRU，最后读取并校验磁盘页。命中时移动到队尾，超过 cache_size 时淘汰最久未使用页。返回深拷贝避免调用方修改缓存而绕过事务。"),
              code("read_page(pid):\n    if pid in staged: return deepcopy(staged[pid])\n    if pid in cache: touch(pid); return deepcopy(cache[pid])\n    page = decode_and_check_crc(read_4096_bytes(pid))\n    cache_put_with_lru_eviction(pid, page)\n    return deepcopy(page)"),
              p("事务 staging 与 no-steal", "h2"),
              p("write_page 和 allocate 必须在事务中调用。未提交页写入独立 staging 字典，不会因 LRU 淘汰而提前落盘；ROLLBACK 丢弃 staging 与新页编号，恢复目录快照。读缓存有界，但大型写事务的 staging 内存随修改页数增长，因此本设计没有宣称事务内存固定有界。"),
              p("堆插入、更新与删除", "h2"),
              table(["操作", "算法", "关键不变量"], [
                  ["插入", "追加页提示 + 删除空槽页提示；无空间则申请新页", "普通追加不扫描所有旧满页；先测容量"],
                  ["更新", "原页可容纳则原地替换；否则写 overflow 并保留原 RID", "原 RID 仍可读取；索引指向一致"],
                  ["删除", "清理逻辑记录及转发目标，行数同步递减", "不把不可见记录返回给扫描"],
                  ["扫描", "逐页枚举有效槽位，跟随需要的转发记录", "返回每条逻辑记录一次"],
              ], [CONTENT_W * .16, CONTENT_W * .45, CONTENT_W * .39]),
              p("insert_hint / reusable_pages 与表元数据一起事务化保存。普通追加读页量随行数线性增长；不同宽度的删除空槽仍可能需要多个候选页。测试覆盖追加读页上界、大小行空槽复用、缓存淘汰、回滚、增长更新和重启。", "small")]

    story += [PageBreak()] + heading("05", "持久化 B+ 树与索引一致性")
    story += [Diagram("tree", 187), p("键与节点组织", "h2"),
              p("B+ 树将 (SQL key, RID) 作为复合有序项。同一 SQL 键可以对应多条记录，并跨越多个叶页。内部节点保存分隔键与子页指针；叶页保存复合项及 next 指针，范围查询从下界叶页沿叶链前进。NULL 不进入索引，唯一索引仅约束非 NULL 键。"),
              table(["算法", "实现要点", "验证"], [
                  ["search / range", "按分隔键下降；跨叶搜索同键与边界项", "重复键跨叶、多层树、开闭区间"],
                  ["insert / split", "按序插入；依据序列化页容量拆分并向上扩展", "变长文本键；根分裂后重启"],
                  ["remove", "删除指定复合项；保留可遍历的结构与叶链", "随机删除、重复键删除、范围结果"],
                  ["索引维护", "INSERT 插入、UPDATE 删除旧项再插新项、DELETE 移除", "实际扫描路径和更新后的可达记录"],
              ], [CONTENT_W * .21, CONTENT_W * .47, CONTENT_W * .32]),
              p("索引局限与完整性", "h2"),
              p("索引目前为单列，删除没有实现节点合并与空页回收，DROP 后已分配页也保留。主键和 UNIQUE 对应受保护索引，禁止直接删除约束索引。所有堆与索引变更位于同一事务中，避免提交时两者分离。", "small")]

    story += [PageBreak()] + heading("06", "SQL 前端与语义绑定")
    story += [p("词法 → AST → 绑定", "h2"),
              p("词法器识别 SQL 字符串的双单引号转义、引号标识符、数字、运算符、单行及嵌套块注释，并为每个 token 保存偏移、行号和列号。递归下降解析器构建普通字典 AST；代码中没有 eval、exec 或 Python 表达式求值。"),
              code("OR\n  AND\n    NOT\n      comparison / IS NULL / IN / LIKE / BETWEEN\n        +  -\n          *  /  %\n            unary + / unary -\n              literal / column / function / (expression)"),
              table(["类别", "支持内容"], [
                  ["DDL / DML", "CREATE/DROP TABLE；CREATE [UNIQUE]/DROP INDEX；多行 INSERT；UPDATE；DELETE"],
                  ["SELECT", "DISTINCT、INNER/LEFT JOIN ON、WHERE、GROUP BY、HAVING、ORDER BY、LIMIT/OFFSET"],
                  ["表达式", "算术、布尔逻辑、比较、IS [NOT] NULL、[NOT] IN/LIKE/BETWEEN"],
                  ["函数", "COUNT/SUM/AVG/MIN/MAX；LOWER/UPPER/LENGTH/ABS/ROUND/COALESCE"],
                  ["类型与约束", "INTEGER/REAL/TEXT/BOOLEAN；VARCHAR(n)；inline PRIMARY KEY/UNIQUE/NOT NULL/DEFAULT literal"],
                  ["管理", "BEGIN/COMMIT/ROLLBACK；SHOW TABLES；DESCRIBE；CHECKPOINT；CHECK DATABASE；EXPLAIN [ANALYZE]"],
              ], [CONTENT_W * .22, CONTENT_W * .78]),
              p("绑定先于扫描，空表也会拒绝未知列、歧义列、非法函数和不合规则的分组表达式。标识符统一为小写；引号内含点的名字明确拒绝；AST 深度上限 128。SELECT 无 FROM 可用于常量与表达式实验。", "small")]

    story += [PageBreak()] + heading("07", "查询执行与可观察优化")
    story += [p("物理算子与执行顺序", "h2"),
              p("引擎构建并执行 TableScan / IndexScan、Filter、HashJoin / NestedLoopJoin、HashAggregate、Having、Project、Distinct、Sort 与 Limit。等值连接可构建右侧哈希桶，其他连接使用嵌套循环；LEFT JOIN 为无匹配左行补入右侧 NULL。"),
              p("优化策略", "h2"),
              p("将只依赖基表的 AND 谓词下推到基表扫描后。对可用索引的比较与同列 IN 条件读取候选 RID；默认候选数不超过 max(1, 总行数 × 35%) 时选择索引，否则保留表扫描。可选 adaptive 模式利用采集的列统计与读路径成本学习，覆盖不足时回退。没有连接顺序搜索；候选枚举本身也有开销。"),
              *figure("query-plan.jpg", "图：真实计划区域截取。EXPLAIN 展示计划；ANALYZE 同时返回执行结果与实际计数。", 81 * mm, crop_box=(.2,.445,.965,.847)),
              p("结果语义与计数", "h2"),
              p("NULL 参与表达式按三值逻辑传播；WHERE/HAVING 仅保留真值。COUNT(*) 计行数，其他聚合忽略 NULL；COALESCE 在首个非 NULL 参数处停止。ORDER BY 支持输出别名和位置序号，序号在执行前校验。rows_scanned 统计实际获取的堆记录；disk_reads 是应用层文件页读取次数，可能命中操作系统页缓存，不等同于设备读取。", "small")]
    story += [p("ROUND 按十进制半值远离 0；AVG 在普通求和溢出时以高精度 Decimal 求均值，保留正负抵消残差。DISTINCT、GROUP BY、JOIN 与 IN 统一类型敏感语义，布尔值不与数值 0/1 混同；JOIN 比较在选算法前绑定类型及恒 NULL。ORDER BY 引用不同表达式的重复输出别名会报歧义。", "small")]

    story += [PageBreak()] + heading("08", "事务、WAL 与恢复保证")
    story += [Diagram("wal", 133), p("提交协议", "h2"),
              p("事务修改先形成 staging，提交按页号生成完整 4096 字节页映像。WAL 的 BEGIN、页记录和 COMMIT 各有 CRC，提交记录包含数量、目标页数与 SHA-256 摘要。WAL fsync 后写数据页并 fsync，再发布缓存，全部完成才清理 WAL；缓存发布失败同样 poison 并保留日志。"),
              table(["中断/错误位置", "重启后的行为"], [
                  ["WAL 未形成完整有效提交", "不应用不完整事务；原已提交数据保留"],
                  ["完整提交 WAL 后、数据页未全部写入", "校验全部映像与提交摘要；整体 redo 并同步数据文件"],
                  ["数据 fsync 后、WAL 清理前", "可再次应用同一页映像；结果幂等"],
                  ["前缀/尾部异常或未知完整帧", "完整提交后异常尾部、前缀加尾部损坏及未知完整尾帧均拒绝恢复，保留日志与数据文件"],
                  ["提交 I/O 或结果发布失败", "实例 poisoned；发布完成前保留完整 WAL，禁止继续执行，须重开确认状态"],
              ], [CONTENT_W * .40, CONTENT_W * .60]),
              p("事务边界", "h2"),
              p("写语句默认自带事务，也支持显式 BEGIN/COMMIT/ROLLBACK。显式事务执行失败时撤销整个活动事务；关闭数据库时也撤销尚未提交的事务。一个进程内 API 串行化，一个数据库目录仅允许一个持有操作系统文件锁的实例。"),
              p("保证边界", "h2"),
              p("数据已持久化后，WAL 清理的 OSError 计数并保持提交成功；其他清理异常或中断仍 poison，重开确认状态。fsync 保证依赖文件系统和设备，测试不等于硬件断电认证。未实现 MVCC、行锁或分布式提交；Windows 未使用目录 fsync，日志文件预先存在。", "small")]

    story += [PageBreak()] + heading("09", "自动化测试与真实证据", f"证据时间：{escape(test['timestamp'])}")
    story += [table(["基础测试层", "用例数", "主要证据"], test_rows(core_modules),
                    [CONTENT_W * .22, CONTENT_W * .13, CONTENT_W * .65]),
    p(f"本页基础模块小计 {sum(counts[m] for m in core_modules)} 项；增强模块在下一页。全部模块合计 {n} 项，计数均来自同一份 test_results.json。", "small"),
    p("差分测试与运行时分离", "h2"),
    p("测试用 SQLite 作为 oracle，使用相同 DDL、150 行固定种子数据与相同变更，进行 180 次 SELECT 结果比较，覆盖筛选、分组聚合、去重、IN、NULL 与排序。SQLite 只出现在测试代码，AuroraDB 核心不调用 SQLite、DuckDB 或其他数据库执行查询。"),
    p("恢复与故障注入", "h2"),
    p("真实子进程 os._exit 覆盖日志、数据写入与 fsync 中断后重开。故障回归还覆盖 CRC 损坏、部分页恢复、完整提交后异常尾部、前缀加尾部损坏、持久提交后缓存分配失败、非 I/O 日志清理异常与 KeyboardInterrupt，检查拒绝继续执行及恢复证据。"),
    p("复现：python tools/verify.py；python -m unittest discover -s tests -v；python tools/benchmark.py。", "small"),
    p(f"当前汇总：{n} 项用例，全部通过；失败 {test['failures']} 项，错误 {test['errors']} 项，总耗时 {test['duration_seconds']:.3f} 秒。完整个案记录保存在 docs/test_results.json。测试通过说明已验证场景成立，不代表已证明不存在所有缺陷。", "small")]
    story += [PageBreak()] + heading("09B", "增强功能测试与交付核验", "新模块与原有测试使用相同的自动验证入口")
    story += [table(["增强测试层", "用例数", "主要证据"], test_rows(extension_modules),
                    [CONTENT_W * .22, CONTENT_W * .13, CONTENT_W * .65]),
              p(f"增强模块小计 {sum(counts[m] for m in extension_modules)} 项；与前页基础模块相加为 {n} 项。协议回归与校园真实模型验收分开记录，不把演示规则输出当作模型效果。", "small"),
              p("独立可执行程序", "h2"),
              p(f"Windows 可执行程序额外通过 {len(smoke['checks'])} 项实测，包含独立进程运行及内置 Web 资产；具体逐项记录见 docs/executable_smoke.json{numeric_smoke_note}。程序大小 {smoke['exe_bytes']:,} 字节。该检查项数独立于 unittest 用例总数。"),
              p("补充一致性审查", "h2"),
              p("本轮还独立执行 209 次异常元数据替换与 8 项真实 HTTP/SQL/Python 一致性入口复核，均通过。它们是一次性补充审查，不计入上述自动测试数，也不暗示 verify.py 会自动重跑；覆盖与限制见 docs/INTEGRITY_SPEC.txt。", "small")]
    if audit is not None:
        audit_labels = {"scan_index_consistency": "全表/索引一致性",
                        "sqlite_join_group_having": "SQLite 连接/分组对照"}
        descriptions = "；".join(
            f"{escape(audit_labels.get(suite['name'], suite['name']))} {suite['comparisons']} 次"
            for suite in audit["suites"]
        )
        story += [p(
            f"额外只读对照审计：{descriptions}。共 {audit['total_comparisons']} 次实际比较，"
            f"差异 {audit['total_mismatches']}。环境、覆盖范围和用例摘要见 docs/audit_queries.json，"
            "tools/audit_queries.py 可复现。该审计计数独立于 unittest 用例数。", "small")]

    story += [PageBreak()] + heading("10", "索引性能实验", "固定数据生成规则；相同查询、相同结果、相同 16 页缓存")
    story += [code(bench["query"]),
              BenchmarkBars(scan["stats"]["rows_scanned"], index["stats"]["rows_scanned"]),
              table(["指标", "全表冷 / 再执行", "索引冷 / 再执行"], [
                  ["实际堆记录扫描", f"{scan['stats']['rows_scanned']:,} / {warm_scan['stats']['rows_scanned']:,}", f"{index['stats']['rows_scanned']:,} / {warm_index['stats']['rows_scanned']:,}"],
                  ["应用层文件读页", f"{scan['stats']['disk_reads']} / {warm_scan['stats']['disk_reads']}", f"{index['stats']['disk_reads']} / {warm_index['stats']['disk_reads']}"],
                  ["查询耗时（ms）", f"{scan['elapsed_ms']:.3f} / {warm_scan['elapsed_ms']:.3f}", f"{index['elapsed_ms']:.3f} / {warm_index['elapsed_ms']:.3f}"],
                  ["返回结果", str(scan["rows"]), str(index["rows"])],
              ], [CONTENT_W * .34, CONTENT_W * .33, CONTENT_W * .33]),
              p("实验方法与解释", "h2"),
              p(f"构造 {bench['rows']:,} 行 measurements：code=(id × 17) mod 5000，reading=(id mod 997)/10，文本补充记录体积。先无 code 索引运行两次查询，关闭并重开测量连接；创建 code_idx 后再次关闭并重开，运行相同查询两次。两侧返回均为 [[3130, 13.9]]。"),
              p(f"索引将获取的堆记录从 {scan['stats']['rows_scanned']:,} 条降到 {index['stats']['rows_scanned']} 条，减少 {bench['scanned_row_reduction']:,.0f} 倍；冷读页从 {scan['stats']['disk_reads']} 页降到 {index['stats']['disk_reads']} 页。表扫描工作集超过 16 页 LRU，因此第二次完整扫描仍会产生缓存失配；不能仅凭“热查询”名称假设零 I/O。"),
              p("限制：这里只有一种选择性较高的等值点查，未覆盖所有查询负载或作统计显著性推断。耗时来自当前机器的一次运行，不承诺其他硬件速度；最稳定证据是相同结果、算子路径及实际记录/页读取计数。", "small")]

    story += [PageBreak()] + heading("11", "交互界面与演示验证")
    story += [*figure("workbench.jpg", "图：真实 SQL 工作台，展示表目录、编辑器、结果表格和物理计划/统计区。", 104 * mm),
              table(["交互能力", "验证方式"], [
                  ["SQL 编辑与预设", "输入任意支持语句；Ctrl+Enter 执行；多条结果可切换"],
                  ["Schema 导航", "表名、字段、类型、索引及目录状态来自 Database.schema"],
                  ["结果与错误", "网格显示 NULL/数字/文本；失败显示明确语法或约束原因"],
                  ["计划与缓冲计数", "展示执行返回的计划树和统计，不用界面伪造性能数字"],
                  ["持久化与事务", "显式回滚示例；停止后用同一 --db 重启核验数据"],
              ], [CONTENT_W * .30, CONTENT_W * .70]),
              p("本机访问控制", "h2"),
              p("服务绑定 127.0.0.1，写 API 要求会话 token；验证 Host、Origin 和跨站请求标记，限制请求体 512 KiB，拒绝无效 UTF-8、重复 JSON 键、非有限数及端点未知字段，固定静态资源路由并设置 CSP。设计面向本机教学工作台，没有用户账户、网络部署身份认证或审计系统。浏览器标签页共享同一个数据库事务会话。", "small")]
    if ui:
        story += [p(f"真实浏览器额外完成 {len(ui['checks'])} 项交互检查，覆盖桌面、窄屏与手机布局、移动目录、建议填入不执行、手动执行、事务保留与禁用 AI 的 SQL 行为；无控制台错误。记录见 docs/ui_smoke.json，独立于 {n} 项 unittest。", "small")]

    story += [PageBreak()] + heading("12", "增强诊断与真实扫描对比", "把数据一致性和访问路径收益转换成可复查证据")
    story += [p("全库一致性检查", "h2"),
              p("CHECK DATABASE 或工作台检查按钮调用只读诊断。独立文件句柄绕过暖 LRU，核验全部当前分配页的 CRC/基础结构；目录 chunk、堆槽/RID/转发、列字段/类型/NULL/UNIQUE、索引页不变量和每个非 NULL 键/RID 的双向对应均被检查。缺项、多项、错误键、共享页或循环会形成结构化错误。"),
              p("活动事务检查当前目录与 staged 优先的版本，明确标为 current transaction；暂存覆盖的旧物理页不另行核验。DROP、旧 overflow 与缩小目录留下的不可达页计为 retained/info，而 CRC 或基础结构损坏仍是错误。检查不修复、不分配、不改缓存/目录/暂存/WAL/统计，资源故障不会伪装成成功。"),
              *figure("integrity.jpg", "图：真实全库检查结果。页数、表数、行数与问题项来自诊断模块。", 73 * mm),
              p("同一 SELECT 的两种真实路径", "h2"),
              p("扫描对比先运行强制堆扫描，再运行当前模式的自动计划。两次均执行真实 SELECT；各次清空应用缓冲池，保留操作系统缓存与活动事务 staged 页，比较完整结果的 multiset（含重复行与输出类型）。显示真实堆记录数、文件读页、耗时和计划；相同结果才报告 matched。"),
              p("对比使用不带 LIMIT/OFFSET 的单个 SELECT，避免不同访问顺序选择不同合法子集；普通 SQL 仍支持 LIMIT n OFFSET m。多次计时有噪声，不能将本机一次结果解释成普遍加速承诺。目录被修改或损坏时，检查失败会报告问题，不自动回滚用户事务。详见 INTEGRITY_SPEC.txt。", "small")]

    story += [PageBreak()] + heading("13", "学校 AI API 与查询助手", "远程建议可选；数据库编译与执行仍由自研内核负责")
    story += [Diagram("ai", 154),
              p("能力与接入协议", "h2"),
              p("提供自然语言生成查询、解释 SQL 与优化建议。服务端可配置 mode、base_url、model、api_key、timeout、requests_per_minute，按 DeepSeek/OpenAI 兼容 chat completions 协议发送非流式 POST，读取 choices[0].message.content。仅处理最终答案；不执行工具调用，也不展示模型 reasoning_content。学校已提供部署名称，默认使用 deepseek-chat。"),
              p("建议与执行边界", "h2"),
              p("AIService 没有数据库句柄。返回建议经本地解析、名称绑定和 EXPLAIN 计划校验，仅单个 SELECT 可通过建议校验；不扫描结果行、不自动执行。无效建议预览不会撤销用户已有事务。用户审阅、填入编辑器后再手动运行。编译通过只证明有限的语法/绑定/计划成立，不保证业务含义、结果或优化收益。"),
              *figure("campus-ai-live.png", "图：真实校园 deepseek-chat 建议，经本地只读编译校验；仍需用户手动执行。", 68 * mm, crop_box=(.22,.63,.96,.98)),
              p("数据与真实验证范围", "h2"),
              p("远程请求包括用户问题、SQL、表结构及可用编译计划，不发送查询结果行或目录默认值；用户问题与 SQL 自身可能含字面值。调用等待受网络总 deadline 约束，未结束的网络工作线程最多 2 个；前端按配置 timeout 增加 15 秒余量。模型输出解码后再次检查密钥，包含标签字段。请求期间不持数据库锁，返回时重新校验建议，但不能保证 schema 未变。启动工作台.cmd 自动加载 ai.local.json；无配置时禁用。mock 为本地演示，校园实连另页记录。", "small")]

    story += [PageBreak()] + heading("13B", "校园模型接入与真实验收")
    story += campus_validation_content(campus)

    story += [PageBreak()] + heading("14", "已知边界与交付复现")
    story += [table(["边界", "当前状态与后续方向"], [
        ["SQL 范围", "无子查询/CTE、UNION、窗口函数、外键、复合键、ALTER TABLE、aggregate DISTINCT、RIGHT/FULL/CROSS JOIN"],
        ["类型语义", "严格插入类型；除法返回实数、零除返回 NULL；标识符大小写归一。并非完整标准 SQL 或 SQLite 兼容实现"],
        ["优化", "默认启发式；可选列统计与轻量学习；单列 IN 多探测。无连接重排、索引覆盖或多索引交并合并"],
        ["执行内存", "连接、分组、排序物化内存；无外排序、溢写或超大结果分页执行"],
        ["存储", "多页目录但根清单有限；有限记录/键大小；无页回收、B+ 删除合并或在线压缩"],
        ["事务/并发", "单进程实例、API 串行、共享会话；无 MVCC 与并发写事务"],
        ["AI / 诊断", "模型建议不自动执行；校园 deepseek-chat 通过小样本实连，非通用质量保证。完整检查持锁且占用与数据规模相关的内存"],
    ], [CONTENT_W * .23, CONTENT_W * .77]),
    p("复现与交付目录", "h2"),
    code("python -m auroradb --db data/demo --demo --web\npython tools/verify.py\npython tools/benchmark.py\npython tools/build_report.py"),
    p("验证命令只使用测试/临时目录，不应指向保存重要数据的已有目录。benchmark.py 自行建立临时实验库。报告构建读取 docs/test_results.json、docs/benchmark.json 及真实界面截图，不从文本手填测试计数或性能结果。"),
    p("核心源码：auroradb/sql.py、engine.py、storage.py、integrity.py、ai.py、server.py、__main__.py；教学数据：auroradb/examples/demo.sql；验证源码：tests/；运行证据：docs/；报告生成器：tools/build_report.py。组员姓名、角色和实际完成情况由团队在独立填写表中补充。", "small")]
    story += [PageBreak()] + heading("15", "理论依据与源码索引")
    references = [
        ("[1] Andy Pavlo, CMU 15-445/645 (Fall 2024), Lecture 06: Buffer Pools.",
         "https://15445.courses.cs.cmu.edu/fall2024/notes/06-bufferpool.pdf",
         "用于页缓存与淘汰原理。AuroraDB 使用复制对象的有界 LRU，加独立 no-steal staging。"),
        ("[2] Andy Pavlo, CMU 15-445/645 (Fall 2024), Lecture 08: Indexes &amp; Filters I.",
         "https://15445.courses.cs.cmu.edu/fall2024/notes/08-indexes1.pdf",
         "用于分裂、复合 RID 与范围扫描原理。本实现删除延迟合并，不维护传统半满约束。"),
        ("[3] Andy Pavlo, CMU 15-445/645 (Fall 2024), Lecture 20: Database Logging.",
         "https://15445.courses.cs.cmu.edu/fall2024/notes/20-logging.pdf",
         "用于 NO-STEAL/FORCE 与写前日志的理论关系。本实现为页映像 redo，不是完整 ARIES。"),
        ("[4] PostgreSQL Documentation, Write-Ahead Logging (WAL).",
         "https://www.postgresql.org/docs/current/wal-intro.html",
         "用于日志先于数据持久化的通用原则；代码、文件格式与并发模型均为项目自己的实现。"),
        ("[5] DeepSeek API Docs, Create Chat Completion.",
         "https://api-docs.deepseek.com/api/create-chat-completion",
         "用于兼容消息请求、非流式完成与最终 content 字段；校园 deepseek-chat 的兼容性已由独立实连证据核验。"),
    ]
    for title, url, relevance in references:
        story += [p(title, "h2"), p(f'<link href="{url}" color="#008a91">{url}</link>', "small"), p(relevance)]
    story += [p("源码导航与证据追溯", "h2"), table(["检查内容", "源码入口"], [
        ["字节格式 / 校验 / 提交 / 恢复", "storage.py: _encode_page, _read_committed_wal, commit, _recover"],
        ["稳定 RID / 追加 / 范围遍历", "storage.py: HeapTable.insert/update, BPlusTree.search/range"],
        ["语法 / 绑定 / 优化 / 执行", "sql.py: tokenize/parse; engine.py: _bind/_scan_choice/_select"],
        ["跨目录页与跨模块事务", "engine.py: catalog 加载/持久化；tests/test_integration_storage.py"],
        ["一致性与路径对比", "integrity.py: check_database；engine.py: integrity_check/compare_query"],
        ["AI 协议与只读预览", "ai.py: AIService；server.py: _assist；engine.py: preview_query"],
        ["真实证据生成", "tools/verify.py, benchmark.py, executable_smoke.py, build_report.py"],
    ], [CONTENT_W * .32, CONTENT_W * .68]),
    p("资料核对日期 2026-10-09。理论资料用于解释设计，并不作为本项目已经实现其全部能力的证明。实现证据以项目源码和自动化测试为准。", "small")]
    story += [PageBreak()] + heading("16", "研究扩展与学术引用")
    story += [table(["创新方向", "实现与引用依据"], [
        ["可信查询改写", "optimizer.py：三条可信规则证据、结果核对、多次实测、代次与策略失效。借鉴 QO-Verify（CIDR 2026）及 ReSequel（PVLDB 2026）。"],
        ["统计与学习", "learning.py：列统计、直方图、单调基数校准、读路径轻量回归和训练域回退。借鉴 PRICE（VLDB 2025）及 CardOOD（VLDB Journal 2026）。"],
        ["预测式缓冲", "storage.py：扫描提示、有限历史、采样淘汰、LRU回退。借鉴 Sampling-based Predictive Database Buffer Management（PVLDB 2025）。"],
        ["语义算子", "semantic.py：筛选、分类和标签计数；批处理、精确缓存与显式预算。借鉴 iPDB（2026 预印本）。"],
        ["公开数据集层", "保留中文教学库，新增 --dataset chinook 的 11 表、约 15,607 行可运行子集；来源为 Chinook 官方仓库与 DemoDB 统计页，MIT 许可。"],
    ], [CONTENT_W*.24,CONTENT_W*.76]),
    p("完整正文引用、参考文献 DOI/链接、可复现实验、正负结果和缩减实现的边界见同目录 AuroraDB_项目论文.pdf。相关研究思想属于原作者；本项目的创新定位是自研内核上的可检验工程集成，不宣称复现原系统或达到其公开性能。"),
    p("实验脚本为 tools/optimizer_experiment.py、buffer_experiment.py 和 semantic_experiment.py。批量/缓存对照来自本地假服务，不能当作真实模型准确率；校园小样本另由 tools/campus_api_smoke.py 验收，分别保存证据。", "small")]
    return story


def manual(test, campus=None):
    story = [Spacer(1, 20), CoverHero("使用说明与答辩演示", ["版本 1.2.0 / 自研数据库与四项研究扩展", "中文工作台 / 命令行 / 8 分钟演示脚本"],
             [("3.12+", "Python 源码运行版本"), ("127.0.0.1", "本机工作台"), ("8 min", "建议答辩演示时长")]),
             Spacer(1, 20), p("开始前", "h2"),
             p("Windows 64 位环境双击项目目录的“启动工作台.cmd”，无需安装 Python 或数据库；服务期间保持命令行窗口开启。也可直接运行下面的打包程序命令。源码方式需要 Python 3.12 或更新版本；核心运行无第三方依赖。PDF 报告构建才需要 reportlab。"),
             code('.\\dist\\AuroraDB.exe --db .\\data\\demo --demo --web\n\n# Source version:\npython -m auroradb --db data/demo --demo --web'),
             p("首次使用 --demo 时，仅在空目录/空目录表结构中加载 students、courses、enrollments 教学数据。已有表时保留已有数据并跳过初始化。默认浏览器打开控制台打印的地址；端口冲突可改为 --port 8766 或 --port 0。", "small")]

    story += [PageBreak()] + heading("01", "启动与数据目录")
    story += [table(["场景", "命令"], [
        ["本机 Web 工作台", "python -m auroradb --db data/demo --demo --web"],
        ["不自动打开浏览器", "python -m auroradb --db data/demo --web --no-browser"],
        ["交互式命令行", "python -m auroradb --db data/demo"],
        ["一次性 SQL 查询", 'python -m auroradb --db data/demo --execute "SHOW TABLES;"'],
        ["批量 SQL 文件", "python -m auroradb --db data/demo --file your_script.sql"],
        ["调整缓冲池", "python -m auroradb --db data/demo --web --cache-size 16"],
    ], [CONTENT_W * .29, CONTENT_W * .71]),
    p("Windows PowerShell", "h2"),
    p("源码方式若 python 未加入 PATH，可使用 Python 可执行程序的完整路径，并在带引号的路径前加 &。打包版本为 dist/AuroraDB.exe，可将示例中的 python -m auroradb 换成 .\\dist\\AuroraDB.exe。不要让两个程序同时打开同一 --db 目录。"),
    p("持久化目录", "h2"),
    p("--db 指定目录保存 pages.dat、redo.wal 与 writer.lock。同一目录重启后保留已提交表和记录。Ctrl+C 停止 Web 服务，关闭引擎时撤销未提交事务。备份时先关闭实例，再复制整个数据库目录；不要只复制活动中的 pages.dat。"),
    p("命令行交互", "h2"),
    p("SQL 用分号结束，可跨行输入。.tables 显示目录，.stats 显示统计，.help 查看帮助，.quit 退出。命令行最多显示前 100 行，但返回的 row_count 是全部结果行数。--file 读取 UTF-8/UTF-8 BOM 文本；批处理错误使用非零退出码。", "small")]

    story += [p("切换到公开 Chinook 数据集", "h2"),
    code("# 保留中文教学库；Chinook 使用独立目录\ndist\\AuroraDB.exe --dataset chinook --web --port 0 --db data\\chinook\n\n# 源码方式\npython -m auroradb --dataset chinook --web --port 0 --db data/chinook"),
    p("Chinook 是公开 MIT 数据集，导入后包含 11 张可运行关系表、约 15,607 行；导入映射和每表计数见 docs/chinook_import.json。其视图、触发器、存储过程和 BLOB/媒体列不进入当前有限 SQL 子集。中文库用于需求、事务、NULL 与 AI 语义演示，Chinook 用于多表连接、聚合、排序与索引演示。固定种子性能实验仍使用独立的 5000/6000 行数据。来源：官方仓库 https://github.com/lerocha/chinook-database；表规模统计 https://chinook.demodb.dev/downloads。", "small"),
    PageBreak()] + heading("02", "工作台操作与结果阅读")
    story += [*figure("workbench.jpg", "真实工作台截图；页面布局可能随窗口大小变化，内容全部来自运行引擎。", 112 * mm),
    table(["区域", "使用方式"], [
        ["左侧表目录", "检查字段、类型、索引与记录数；用于编写正确列名"],
        ["SQL 编辑器", "输入 SQL 或选择教学预设；Ctrl+Enter 执行；分号分隔多条语句"],
        ["结果网格", "读取列标题和行结果；NULL 是 SQL 空值；多结果逐个查看"],
        ["执行计划", "自顶向下看输出算子，自底向上理解数据来源与处理过程"],
        ["缓存/查询统计", "观察 rows_scanned、disk_reads、cache_hits 与 elapsed_ms；区分本次与累计指标"],
        ["事务与 AI 状态", "事务 badge 来自当前引擎状态；AI 明确区分禁用、本地规则演示与远程配置"],
    ], [CONTENT_W * .23, CONTENT_W * .77]),
    p("所有标签页共享一个显式事务会话。在一个标签页执行 BEGIN 后，其他标签页操作会进入同一事务；演示时建议只保留一个工作台标签页。", "small")]

    story += [PageBreak()] + heading("03", "SQL 操作速查")
    story += [p("查看结构与基础查询", "h2"),
    code("SHOW TABLES;\nDESCRIBE students;\nSELECT id,name,gpa FROM students\nWHERE gpa >= 3.8 ORDER BY gpa DESC,id;"),
    p("多表统计：数据库课程成绩", "h2"),
    code("SELECT s.department, COUNT(*) AS count_students,\n       ROUND(AVG(e.score),2) AS avg_score\nFROM students s\nJOIN enrollments e ON s.id=e.student_id\nJOIN courses c ON c.id=e.course_id\nWHERE c.title='数据库原理'\nGROUP BY s.department\nHAVING COUNT(*) >= 2\nORDER BY avg_score DESC;"),
    p("建立独立实验表", "h2"),
    code("CREATE TABLE notes (\n  id INTEGER PRIMARY KEY, title VARCHAR(60) NOT NULL,\n  done BOOLEAN DEFAULT FALSE\n);\nINSERT INTO notes(id,title) VALUES (1,'first note'),(2,'second');\nUPDATE notes SET done=TRUE WHERE id=1;\nSELECT * FROM notes ORDER BY id;\nDELETE FROM notes WHERE id=2;"),
    p("PRIMARY KEY / UNIQUE / NOT NULL 与 VARCHAR 长度均由引擎验证。字符串中的单引号用两个单引号转义，例如 'O''Brien'。SELECT 无 FROM 可以验证算术与 NULL：SELECT NULL IS NULL, COALESCE(NULL, 7);。", "small")]

    story += [PageBreak()] + heading("04", "事务、回滚与重启核验")
    story += [p("演示回滚：教学数据保持原状", "h2"),
    code("BEGIN;\nUPDATE students SET gpa=4.0 WHERE id=1001;\nSELECT id,name,gpa FROM students WHERE id=1001;\nROLLBACK;\nSELECT id,name,gpa FROM students WHERE id=1001;"),
    p("事务内查询可见 gpa=4.0，ROLLBACK 后恢复示例中的 3.92。提交则使用 COMMIT。显式事务中的执行错误会撤销整个活动事务；在下一次修改前重新 BEGIN。停止程序时尚未提交的修改也会撤销。"),
    p("持久化核验", "h2"),
    code("CREATE TABLE persist_probe(id INTEGER PRIMARY KEY, note TEXT);\nINSERT INTO persist_probe VALUES (1,'committed');\nSELECT * FROM persist_probe;"),
    p("完成上面隐式提交后 Ctrl+C 关闭，再用同一 --db 目录重启，执行 SELECT * FROM persist_probe;，应返回 [1, 'committed']。演示结束可执行 DROP TABLE persist_probe; 清理逻辑表。"),
    p("恢复异常的处理", "h2"),
    p("持久化后缓存/结果发布失败也要求重开；WAL 清理只容忍 OSError，其他异常或中断会禁止继续使用连接。异常日志保留证据，不自动忽略完整提交后的异常尾部。", "small"),
    table(["提示", "操作"], [
        ["already open by another writer", "先关闭另一个 CLI/Web 实例，再打开该数据库目录"],
        ["close and reopen for recovery", "提交结果待恢复；关闭实例并重启，让有效 WAL 完成 redo"],
        ["CRC mismatch / committed WAL corrupt", "停止使用并保留整个目录与错误信息；不要手工删日志掩盖证据"],
    ], [CONTENT_W * .48, CONTENT_W * .52]),
    p("WAL 故障演示应通过自动化测试的临时目录进行；无需在现场破坏演示数据库文件。", "small")]

    story += [PageBreak()] + heading("05", "解释计划与性能指标")
    story += [code("EXPLAIN SELECT id,name FROM students WHERE id=1001;\nEXPLAIN ANALYZE SELECT id,name FROM students WHERE id=1001;"),
    *figure("query-plan.jpg", "真实计划区域截取：路径由优化器根据可用索引与候选数量选择。", 91 * mm, crop_box=(.2,.445,.965,.847)),
    table(["指标/算子", "解释"], [
        ["TableScan / IndexScan", "遍历堆记录 / 从 B+ 树候选 RID 获取堆记录"],
        ["HashJoin / HashAggregate", "等值连接哈希桶 / 分组聚合桶"],
        ["rows_scanned", "此次实际获取的堆记录数，包括参与连接的表"],
        ["disk_reads / cache_hits", "应用层文件读页 / 已提交页 LRU 命中；不等同于硬件设备读取"],
        ["elapsed_ms", "本次 SQL 实际耗时；会受机器、缓存和后台负载影响"],
        ["estimated_rows", "启发式估计，不等于实测行数，不应作为严格预测"],
    ], [CONTENT_W * .37, CONTENT_W * .63]),
    p("EXPLAIN 不执行关系结果行；选择计划时仍可能读取索引候选以估算路径。ANALYZE 执行 SELECT 并返回结果。公平性能比较使用 tools/benchmark.py 的相同数据与查询，避免将小表点查当作全面性能结论。", "small")]

    story += [PageBreak()] + heading("06", "全库检查与扫描对比")
    story += [p("全库一致性检查", "h2"),
              code("CHECK DATABASE;\n\n# Python API:\nreport = db.integrity_check()\nprint(report['ok'], report['summary'], report['issues'])"),
              *figure("integrity.jpg", "真实检查界面；问题由页/目录/堆/约束/B+ 树及双向索引核对得出。", 70 * mm),
              p("点击全库检查可核对分配页、表与行数、可达/retained 页及错误。ok=false 时查看具体问题；本功能不自动修复。retained/info 通常来自尚未回收的 DROP 或旧 overflow，不单独视为损坏。活动事务报告 current transaction，不提交或回滚；关闭或 poisoned 连接需先重开。", "small"),
              p("扫描对比", "h2"),
              p("在编辑器保留一个 SELECT，然后点击扫描对比。程序真实运行强制堆扫描与自动计划，分别清应用 LRU，显示记录数、文件读页、耗时及 multiset 结果一致性。推荐主键点查 SELECT id,name FROM students WHERE id=1001;。"),
              p("对比使用不带 LIMIT/OFFSET 的单个 SELECT；普通查询仍支持 LIMIT n OFFSET m。不会冷却操作系统缓存或删除 staged 写集。扫描收益取决于现有索引与数据分布；小表没有索引收益时保留 TableScan 是正确行为。", "small")]

    story += [PageBreak()] + heading("07", "AI 查询助手与学校接口")
    story += [table(["启动方式", "真实行为"], [
                  ["启动工作台.cmd", "存在 ai.local.json 时自动加载；无配置时 AI 禁用"],
                  ["启动AI演示.cmd", "环境变量 mode=mock；有限本地规则，不调用远程模型"],
                  ["启动学校AI.cmd", "显式读取 ai.local.json；缺少配置则提示，按学校配置调用"],
              ], [CONTENT_W * .32, CONTENT_W * .68]),
              p("首次接入", "h2"),
              code("Copy-Item -LiteralPath .\\ai.example.json -Destination .\\ai.local.json\n# Edit ai.local.json, then launch:\n.\\dist\\AuroraDB.exe --web --demo --port 0 --db .\\data\\demo `\n  --ai-config .\\ai.local.json"),
              p("公开示例已预填学校基址 https://models.sjtu.edu.cn/api/v1，模型 deepseek-chat；只在 ai.local.json 填写自己的凭证。deepseek-reasoner、qwen / qwen3.8-27b 未逐一测量。基址自动追加 /chat/completions；timeout 为秒，代码默认 90、学校示例为 180；requests_per_minute 为 10。等待更久无法保证服务稳定，本轮观察过一次 90 秒配置下的解释请求超时。", "small"),
              p("操作顺序", "h2"),
              p("输入查询需求后选择生成查询；已有 SQL 可选择解释或优化。先阅读建议及本地编译校验，再填入编辑器、手动执行。助手不会自动运行 SQL；校验仅接受一个 SELECT，不保证查询含义正确或优化一定有效。错误建议预览不会撤销已有事务。"),
              *figure("campus-ai-live.png", "真实校园模型结果区：先校验建议，再填入编辑器、手动执行。", 69 * mm, crop_box=(.22,.63,.96,.98)),
              p("只发送问题、SQL、表结构与可用计划；不发送结果行或目录默认值。用户文本可能自带字面值。API Key 留在服务端配置，解码后响应与语义标签再次排密；本地配置及任意目录的数据库文件不进入提交包。前端等待按服务配置 timeout 加 15 秒，慢请求仍受后端总 deadline 与并发上限约束。学校服务要求校内网络或获准的校园 VPN；进程内 10 RPM 限流不跨设备共享。详细故障排查见 docs/AI_API接入说明.txt。", "small")]

    story += [PageBreak()] + heading("07B", "校园实连验收与演示准备")
    story += campus_validation_content(campus)

    story += [PageBreak()] + heading("08", "8 分钟答辩演示脚本")
    story += [table(["时间", "现场操作", "讲解与验收点"], [
        ["00:00-00:45", "启动 --demo --web；展示表目录", "自研 DBMS；三个教学表；真实持久化数据"],
        ["00:45-01:30", "SHOW TABLES；DESCRIBE students", "列类型与约束来自目录页，不由界面硬编码"],
        ["01:30-02:30", "筛选 GPA 查询；独立 notes 表 CRUD", "SQL → AST → 绑定 → 执行；约束与错误位置"],
        ["02:30-03:40", "执行第 03 节多表成绩统计", "JOIN、GROUP BY、聚合、HAVING、ORDER BY 的算子关系"],
        ["03:40-04:50", "EXPLAIN ANALYZE 主键点查", "查看 IndexScan 和 rows_scanned；区分估计与实测"],
        ["04:50-05:50", "执行第 04 节事务回滚", "事务内可见；ROLLBACK 恢复；数据与索引同事务"],
        ["05:50-06:35", "CHECK DATABASE；扫描对比", "完整性只读检查；真实路径与完整结果一致性"],
        ["06:35-07:15", "AI 生成建议后编译预览", "显示真实 AI 模式；建议由用户审阅后决定是否执行"],
        ["07:15-08:00", "展示测试/基准与持久化证据", f"{test['tests_run']} 项通过、180 次差分、5000 行索引实验；诚实说明边界"],
    ], [CONTENT_W * .20, CONTENT_W * .37, CONTENT_W * .43]),
    p("演示准备", "h2"),
    p("提前关闭重复实例并确认端口可用；保留一个工作台标签页；准备空实验表名，避免重复建表影响节奏。若需要重复演示，使用新的 --db 目录，或在确认无所需数据的实验表上执行 DROP；--demo 不会重置已有数据库。"),
    p("建议回答的技术问题", "h2"),
    p("为什么 WAL 要先 fsync？为什么缓存不能直接暴露可修改对象？重复 B+ 键为什么引入 RID？NULL 如何影响 WHERE 和 COUNT？为什么第二次大表扫描仍可能读盘？这五个问题分别对应恢复顺序、事务隔离、复合排序、三值逻辑和有限缓存工作集。", "small")]

    story += [PageBreak()] + heading("09", "复现检查与支持边界")
    story += [p("验证命令", "h2"),
    code("python tools/verify.py\npython tools/benchmark.py\npython tools/build_report.py"),
    p(f"verify.py 会重新运行全部测试并写入 docs/test_results.json。本版证据为 {test['tests_run']} 项通过；测试数可能随后续增加而变化。benchmark.py 在临时目录创建 5000 行实验并写入 docs/benchmark.json。报告构建从 JSON 自动引用最新计数与计时。"),
    p("API 使用示例", "h2"),
    code("from auroradb.engine import Database\n\nwith Database('data/example') as db:\n    result = db.execute('SELECT 1+2 AS value;')[0]\n    print(result['columns'], result['rows'])\n    print(db.stats())"),
    p("支持边界", "h2"),
    table(["可用", "当前不支持"], [
        ["本机单实例、显式/隐式事务、B+ 索引", "多个独立并发事务、MVCC、数据库联网部署"],
        ["CRUD、INNER/LEFT JOIN、分组聚合", "子查询、CTE、窗口、UNION、RIGHT/FULL JOIN"],
        ["inline 约束与单列索引", "外键、复合键、ALTER TABLE、节点删除合并"],
        ["标准库核心、有限 SQL 子集", "完整 SQL 标准兼容、超大数据外排序或磁盘溢写"],
        ["AI 建议、本地预览、校园小样本验收", "AI 自动执行、通用模型质量保证、自动修复与物理回收"],
    ], [CONTENT_W * .48, CONTENT_W * .52]),
    p("获取准确行为：以源码、错误提示和自动化测试为依据。测试可复现、执行计划可观察和限制可说明，是本项目用于课程验收的三个核心特点。", "small")]
    story += [PageBreak()] + heading("10", "研究实验操作")
    story += [p("在辅助工具中切换“研究实验”。所有候选导入只填入编辑器，最终是否执行仍由使用者选择。研究实验拒绝活动事务，不会替你回滚事务。", "h2"),
    table(["方向", "可重复操作"], [
        ["可信改写", "编辑器输入 SELECT id FROM students WHERE id=1001 OR id=1002; 候选留空，点验证并比较。观察规则、结果一致性、原/候选扫描计数。小表耗时不足时不推荐替换是预期结果。"],
        ["统计与学习", "采集统计后切换adaptive；查看学习样本/就绪、执行计划。课程小表通常缺少足够冷缓存样本；5000行训练及留出实验由 optimizer_experiment.py 重现。"],
        ["预测缓存", "选择predictive与采样数8，应用后执行查询观察计数；可随时切回LRU。切换会使旧成本模型与优化结论失效。"],
        ["语义管线", "启动AI演示后点填入课程示例，选择title，条件‘关键词：数据库|索引’，依次筛选、分类、标签计数。相同文本/条件再次运行可观察缓存命中。"],
        ["Chinook 数据集", "使用独立 data/chinook 目录启动 --dataset chinook；先 SHOW TABLES，再运行 Artist→Album→Track 的连接聚合，确认大数据集不会覆盖中文教学库。"],
    ],[CONTENT_W*.22,CONTENT_W*.78]),
    p("语义默认64行、每批8条、最多8批，总预算30秒；SQL执行协作式检查6秒、扫描10万、连接比较20万和中间2万行。规划耗时计入总时长但不能强制中断。只有所选TEXT字段和判断条件发给远程模型；NULL不发送。普通SQL独立于模型，网络等待不持数据库锁。"),
    p("项目论文与 docs/RESEARCH_DESIGN.txt 说明具体引用和边界。campus_api_smoke.json 记录真实校园模型的小样本与单次时延；更大人工标注集的质量评估和长期负载实验仍待后续开展。", "small")]
    return story


def main():
    test = json.loads((DOCS / "test_results.json").read_text(encoding="utf-8"))
    bench = json.loads((DOCS / "benchmark.json").read_text(encoding="utf-8"))
    smoke = json.loads((DOCS / "executable_smoke.json").read_text(encoding="utf-8"))
    if (not test["passed"] or test["failures"] or test["errors"]
            or len(test.get("cases", [])) != test["tests_run"]
            or any(case.get("status") != "passed" for case in test["cases"])):
        raise ValueError("Reports require a clean, real test evidence file")
    if not smoke["passed"]:
        raise ValueError("Reports require successful real executable smoke evidence")
    audit_path = DOCS / "audit_queries.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else None
    if audit is not None:
        suites = audit.get("suites")
        if (not isinstance(suites, list) or not suites
                or any(not isinstance(suite, dict)
                       or type(suite.get("comparisons")) is not int or suite["comparisons"] < 0
                       or type(suite.get("mismatches")) is not int or suite["mismatches"] != 0
                       or suite.get("passed") != suite["comparisons"]
                       or not isinstance(suite.get("name"), str) for suite in suites)
                or audit.get("total_comparisons") != sum(suite["comparisons"] for suite in suites)
                or audit.get("total_passed") != audit.get("total_comparisons")
                or audit.get("total_mismatches") != 0):
            raise ValueError("Reports require valid, zero-mismatch read-only audit evidence")
        if audit.get("engine_sha256") != hashlib.sha256((ROOT / "auroradb" / "engine.py").read_bytes()).hexdigest():
            raise ValueError("Read-only audit evidence must match the current engine source")
    design = DOCS / "INTERNAL_DESIGN.txt"
    if not design.exists():
        raise FileNotFoundError("Storage author must provide docs/INTERNAL_DESIGN.txt before final reports")
    design.read_text(encoding="utf-8")
    ui_path = DOCS / "ui_smoke.json"
    ui = json.loads(ui_path.read_text(encoding="utf-8")) if ui_path.is_file() else None
    if ui is not None and (not ui.get("passed") or not isinstance(ui.get("checks"), list)):
        raise ValueError("Reports require clean actual-browser evidence")
    campus = campus_evidence()
    outputs = [("AuroraDB_设计与测试报告.pdf", "设计与测试报告", report(test, bench, smoke, audit, ui, campus)),
               ("AuroraDB_使用说明.pdf", "使用说明", manual(test, campus))]
    for filename, title, story in outputs:
        path = DOCS / filename
        ReportDocument(path, title).build(story)
        print(str(path))
    template = DOCS / "分工说明填写表.txt"
    if not template.exists():
        template.write_text(
            "AuroraDB 小组分工与个人实际完成情况填写表\n"
            "请按真实情况填写；此表不表示以下工作已由某位同学完成。\n\n"
            "姓名：________  学号：________  角色：存储与恢复\n"
            "建议模块：DiskPager、LRU、HeapTable、BPlusTree、WAL 与崩溃测试\n"
            "实际完成内容与证据：________________________________\n\n"
            "姓名：________  学号：________  角色：SQL 与查询执行\n"
            "建议模块：词法/语法、语义绑定、关系算子、查询计划与约束\n"
            "实际完成内容与证据：________________________________\n\n"
            "姓名：________  学号：________  角色：界面与交付\n"
            "建议模块：Web/CLI、教学数据、打包、使用说明与演示\n"
            "实际完成内容与证据：________________________________\n\n"
            "姓名：________  学号：________  角色：验证与文档（3 人时可合并）\n"
            "建议模块：差分/回归测试、性能基准、设计报告与答辩资料\n"
            "实际完成内容与证据：________________________________\n",
            encoding="utf-8")


if __name__ == "__main__":
    main()
