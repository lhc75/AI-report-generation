"""真实 MySQL 隔离验收：仅操作随机新建的 ai_report_test_* 数据库。

连接配置只在进程内读取；不输出凭据、不调用模型、不运行后台生成任务。
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pymysql
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def concurrent_request(project_id, user_id, barrier, results):
    from fastapi import BackgroundTasks, HTTPException
    from app.db import SessionLocal, engine
    from app.routers import projects
    try:
        barrier.wait(timeout=20)
        with SessionLocal() as db:
            reply = asyncio.run(projects.generate_all(
                project_id, BackgroundTasks(), SimpleNamespace(id=user_id), db))
        results.put(("accepted", reply["run_id"]))
    except HTTPException as exc:
        results.put(("http", exc.status_code))
    except Exception as exc:
        results.put(("error", type(exc).__name__))
    finally:
        engine.dispose()


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS", message, flush=True)


def main():
    # 不导入业务配置，直到连接明确指向独立测试库。
    values = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    connection_config = {
        "host": str(values.get("MYSQL_HOST") or "127.0.0.1"),
        "port": int(values.get("MYSQL_PORT") or 3306),
        "user": str(values.get("MYSQL_USER") or "root"),
        "password": str(values.get("MYSQL_PASSWORD") or ""),
        "charset": str(values.get("MYSQL_CHARSET") or "utf8mb4"),
        "connect_timeout": 5, "read_timeout": 10, "write_timeout": 10,
        "autocommit": True,
    }
    test_database = "ai_report_test_" + uuid4().hex
    assert re.fullmatch(r"ai_report_test_[0-9a-f]{32}", test_database)
    admin, created, engine = None, False, None
    children = []
    with tempfile.TemporaryDirectory(prefix="ai-report-mysql-") as temporary:
        try:
            admin = pymysql.connect(**connection_config)
            with admin.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE `{test_database}` CHARACTER SET utf8mb4")
            created = True
            print("Created isolated MySQL test database", flush=True)
            os.environ.update({
                "AI_REPORT_ENV_FILE": "", "DB_ENGINE": "mysql", "MYSQL_DATABASE": test_database,
                "MYSQL_HOST": connection_config["host"], "MYSQL_PORT": str(connection_config["port"]),
                "MYSQL_USER": connection_config["user"], "MYSQL_PASSWORD": connection_config["password"],
                "MYSQL_CHARSET": connection_config["charset"], "MYSQL_RESET_ON_STARTUP": "false",
                "STORAGE_BACKEND": "local", "STORAGE_LOCAL_DIR": str(Path(temporary) / "objects"),
            })
            from fastapi import BackgroundTasks, HTTPException
            from sqlalchemy import inspect, select, text
            from app import operations
            from app.db import SessionLocal, _ensure_columns, engine, init_db
            from app.models import ChapterTask, ModuleContent, Project, ProjectFile, User
            from app.routers import projects

            init_db()
            with SessionLocal() as db:
                user = User(username="isolated-" + uuid4().hex, password_hash="offline")
                db.add(user)
                db.flush()
                project = Project(user_id=user.id, name="独立 MySQL 验收", status="analyzed")
                db.add(project)
                db.flush()
                task = ChapterTask(project_id=project.id, title="模块", task_type="text", status="pending")
                file = ProjectFile(project_id=project.id, file_type="data", file_name="data.csv", minio_path="unused")
                db.add_all([task, file])
                db.flush()
                content = ModuleContent(chapter_task_id=task.id, version=1, content_type="text",
                                        content="历史内容必须保留", evidence_json="[]", status="ok", is_current=True)
                db.add(content)
                db.commit()
                ids = user.id, project.id, task.id, content.id, file.id
            user_id, project_id, task_id, content_id, file_id = ids

            # 在这个新测试库内模拟旧 schema，随后验证非空历史表的增量迁移。
            legacy_columns = {
                "project": ["operation_token", "material_revision", "last_run_id", "last_run_result"],
                "project_file": ["statistics_config"],
                "chapter_task": ["instruction_revision", "template_title", "template_instruction"],
                "module_content": ["material_revision", "instruction_revision", "evidence_json"],
            }
            with engine.begin() as connection:
                for table, columns in legacy_columns.items():
                    for column in columns:
                        connection.execute(text(f"ALTER TABLE `{table}` DROP COLUMN `{column}`"))
            _ensure_columns()
            _ensure_columns()  # 重复启动迁移必须幂等。
            with SessionLocal() as db:
                project = db.get(Project, project_id)
                task = db.get(ChapterTask, task_id)
                content = db.get(ModuleContent, content_id)
                file = db.get(ProjectFile, file_id)
                check(project.material_revision == task.instruction_revision == content.material_revision == content.instruction_revision == 0,
                      "旧行版本号零初值迁移及重复迁移")
                check(content.content == "历史内容必须保留" and content.evidence_json == "[]", "旧正文和引用初始化保留")
                check(task.template_title == task.template_instruction == "", "旧任务模板定位元数据兼容迁移")
                check(json.loads(file.statistics_config) == {"fields": []}, "旧数据文件默认统计配置")
                check(project.operation_token == project.last_run_id == "" and json.loads(project.last_run_result) == {},
                      "旧项目运行身份默认值")
            for table, columns in legacy_columns.items():
                column_info = {column["name"]: column for column in inspect(engine).get_columns(table)}
                check(all(not column_info[column]["nullable"] for column in columns), f"{table} 新增列为非空")

            # 两个独立进程没有共享 Python RLock，真正检验 MySQL FOR UPDATE。
            context = multiprocessing.get_context("spawn")
            barrier, results = context.Barrier(2), context.Queue()
            children = [context.Process(target=concurrent_request, args=(project_id, user_id, barrier, results)) for _ in range(2)]
            for child in children:
                child.start()
            answers = [results.get(timeout=45) for _ in children]
            for child in children:
                child.join(timeout=10)
            accepted = [answer[1] for answer in answers if answer[0] == "accepted"]
            check(len(accepted) == 1 and ("http", 409) in answers, "跨进程重复批量生成只有一个请求被接受")
            run_token = accepted[0]
            with SessionLocal() as db:
                user = db.get(User, user_id)
                try:
                    projects.delete_task(project_id, task_id, user, db)
                except HTTPException as exc:
                    check(exc.status_code == 409, "运行中编辑/删除互斥")
                else:
                    raise AssertionError("运行中删除未被拒绝")
            with operations.project_transaction(project_id) as (_db, project):
                project.operation_token = "newer-operation"
            operations.finish_run(project_id, run_token, "analyzed", {"succeeded": 1})
            with SessionLocal() as db:
                project = db.get(Project, project_id)
                check(project.operation_token == "newer-operation", "过期 token 不能结束新运行")
                try:
                    operations.ensure_run(db, project_id, run_token)
                except RuntimeError:
                    check(True, "过期 token 不能提交结果")
                else:
                    raise AssertionError("过期提交未被拒绝")
            with SessionLocal() as db:
                existing_ids = set(db.scalars(select(ModuleContent.id)).all())
            asyncio.run(projects.run_generate_one(project_id, task_id, run_token=run_token))
            with SessionLocal() as db:
                check(set(db.scalars(select(ModuleContent.id)).all()) == existing_ids,
                      "过期后台任务不生成内容或调用真实模型")
            print("ALL ISOLATED MYSQL CHECKS PASSED", flush=True)
            return 0
        except pymysql.MySQLError as exc:
            code = exc.args[0] if exc.args else "unknown"
            print(f"BLOCKED: MySQL connection or isolated-database permission failed (code {code})", flush=True)
            return 2
        except Exception as exc:
            # 不打印异常参数，连接异常可能包含私有地址或凭据。
            print(f"FAIL: isolated MySQL acceptance ({type(exc).__name__})", flush=True)
            return 1
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(timeout=5)
            if engine is not None:
                engine.dispose()
            if admin is not None:
                try:
                    if created:
                        with admin.cursor() as cursor:
                            cursor.execute(f"DROP DATABASE `{test_database}`")
                        print("Dropped isolated MySQL test database", flush=True)
                finally:
                    admin.close()


if __name__ == "__main__":
    raise SystemExit(main())
