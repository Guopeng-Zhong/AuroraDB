"""AuroraDB command-line entry point and local workbench launcher."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from .engine import Database
from .ai import AIService
from .server import resource_directory, serve


def display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in value)


def print_result(result):
    columns, rows = result.get("columns", []), result.get("rows", [])
    if columns:
        def cell(value):
            text = "NULL" if value is None else str(value)
            text = text.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            return text if len(text) <= 72 else text[:69] + "..."
        formatted = [[cell(value) for value in row] for row in rows[:100]]
        widths = [display_width(str(column)) for column in columns]
        for row in formatted:
            for i, value in enumerate(row):
                widths[i] = max(widths[i], display_width(value))
        def line(row):
            return " | ".join(str(value) + " " * (widths[i] - display_width(str(value))) for i, value in enumerate(row))
        print(line(columns))
        print("-+-".join("-" * width for width in widths))
        for row in formatted:
            print(line(row))
        if len(rows) > 100:
            print(f"… 命令行仅显示前 100 行，共 {len(rows)} 行。")
    if result.get("plan"):
        print(json.dumps(result["plan"], ensure_ascii=False, indent=2))
    message = result.get("message") or f"{result.get('row_count', len(rows))} 行"
    print(f"{message}  ({result.get('elapsed_ms', 0):.3f} ms)")


def execute_and_print(database, sql):
    for result in database.execute(sql):
        print_result(result)


def load_ai_config(path):
    """Local credentials are backend-only and excluded from the submission ZIP."""
    if path is None:
        return AIService.from_env()
    try:
        if path.stat().st_size > 16 * 1024:
            raise ValueError("AI 配置文件不能超过 16 KiB。")
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        allowed = {'mode','base_url','model','api_key','timeout','requests_per_minute'}
        if not isinstance(value,dict) or set(value)-allowed:
            raise ValueError("AI 配置仅支持 mode、base_url、model、api_key、timeout、requests_per_minute 字段。")
        return AIService(**value)
    except (OSError, ValueError, TypeError):
        raise ValueError("无法读取 AI 配置；请检查文件是否存在，以及 JSON 字段和格式。") from None


def complete_statement(sql: str) -> bool:
    """Recognize a terminator using the lexer's quote and comment boundaries."""
    quote = None
    line_comment = False
    block_depth = 0
    last = ""
    i = 0
    while i < len(sql):
        ch, nxt = sql[i], sql[i:i + 2]
        if line_comment:
            if ch == "\n":
                line_comment = False
        elif block_depth:
            if nxt == "/*":
                block_depth += 1
                i += 1
            elif nxt == "*/":
                block_depth -= 1
                i += 1
        elif quote:
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    i += 1
                else:
                    quote = None
        elif nxt == "--":
            line_comment = True
            i += 1
        elif nxt == "/*":
            block_depth = 1
            i += 1
        elif ch in "'\"`[":
            quote = "]" if ch == "[" else ch
            last = ch
        elif not ch.isspace():
            last = ch
        i += 1
    return last == ";" and quote is None and block_depth == 0


def repl(database):
    print("AuroraDB · 自研关系数据库")
    print("SQL 以分号结束；.help 查看命令；.quit 退出。")
    pending = []
    while True:
        try:
            line = input("aurora> " if not pending else "    ... ")
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            pending.clear()
            print("\n已取消当前输入。")
            continue
        if not pending and line.strip().startswith("."):
            command = line.strip().lower()
            if command in {".quit", ".exit"}:
                return
            if command == ".help":
                print(".tables  表结构    .stats  缓冲区统计    .quit  退出\n支持 SHOW TABLES;、DESCRIBE 表名;、CHECK DATABASE;、EXPLAIN SELECT ...; 等 SQL。")
            elif command == ".tables":
                print(json.dumps(database.schema(), ensure_ascii=False, indent=2))
            elif command == ".stats":
                print(json.dumps(database.stats(), ensure_ascii=False, indent=2))
            else:
                print("未知命令。输入 .help 查看帮助。")
            continue
        if not pending and not line.strip():
            continue
        pending.append(line)
        sql = "\n".join(pending)
        if complete_statement(sql):
            pending.clear()
            try:
                execute_and_print(database, sql)
            except Exception as exc:
                print(f"错误：{exc}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AuroraDB：自研关系数据库与 SQL 工作台")
    parser.add_argument("--db", default="data/aurora", help="数据库目录（默认 data/aurora）")
    parser.add_argument("--web", action="store_true", help="启动本机浏览器 SQL 工作台")
    parser.add_argument("--port", type=int, default=8765, help="工作台端口（默认 8765；0 为自动分配）")
    parser.add_argument("--demo", action="store_true", help="仅在数据库为空时加载中文教学示例（students/courses/enrollments）")
    parser.add_argument("--chinook", action="store_true", help="仅在数据库为空时加载 Chinook 公开数据集（--dataset chinook 的别名）")
    parser.add_argument("--dataset", choices=("demo", "chinook"), help="选择空数据库的初始化数据集；默认不加载")
    parser.add_argument("--file", type=Path, help="执行 UTF-8 SQL 文件")
    parser.add_argument("--execute", "-e", help="执行 SQL 文本")
    parser.add_argument("--cache-size", type=int, default=64, help="缓冲池页数（默认 64）")
    parser.add_argument("--no-browser", action="store_true", help="启动工作台时不自动打开浏览器")
    parser.add_argument("--ai-config", type=Path, help="本地 AI JSON 配置文件；未指定时读取 AURORA_AI_* 环境变量")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0–65535 之间")
    if args.cache_size < 1:
        parser.error("缓冲池页数必须大于 0")
    selected_dataset = args.dataset
    if args.demo:
        if selected_dataset and selected_dataset != "demo":
            parser.error("--demo 与 --dataset chinook 不能同时使用")
        selected_dataset = "demo"
    if args.chinook:
        if selected_dataset and selected_dataset != "chinook":
            parser.error("--chinook 与 --dataset demo 不能同时使用")
        selected_dataset = "chinook"
    database = None
    try:
        database = Database(args.db, cache_size=args.cache_size)
        if selected_dataset:
            if database.schema():
                print("已有表，保留现有数据，跳过示例初始化。")
            else:
                dataset_file = resource_directory() / "examples" / f"{selected_dataset}.sql"
                database.execute(dataset_file.read_text(encoding="utf-8"))
                if selected_dataset == "chinook":
                    print("Chinook 公开数据集已创建：11 张表，约 15,607 行。")
                else:
                    print("中文教学示例已创建：students / courses / enrollments。")
        if args.file:
            execute_and_print(database, args.file.read_text(encoding="utf-8-sig"))
        if args.execute:
            execute_and_print(database, args.execute)
        if args.web:
            serve(database, args.port, open_browser=not args.no_browser, ai_service=load_ai_config(args.ai_config))
        elif not args.file and not args.execute:
            repl(database)
        return 0
    except KeyboardInterrupt:
        print("\n操作已终止。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
