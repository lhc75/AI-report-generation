"""最小真实模型验收：业务库只读，合成资料仅写入随机独立 MySQL 库。

显式运行本脚本才读取 .env 和调用默认 write/review 模型；pytest 不会收集。
不输出连接地址、API Key、加密密钥或任何业务材料，退出时删除独立测试库。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import io
import json
import logging
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import socket
import sys
import tempfile
import time
from uuid import uuid4
from urllib.parse import urlsplit

from dotenv import dotenv_values
import pymysql

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


class VerificationBlocked(Exception):
    pass


def _connectivity_worker(endpoint_url, output):
    """只记录 DNS/TCP 结果；不返回地址，不携带模型认证凭据。"""
    try:
        endpoint = urlsplit(endpoint_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            result = {"ok": False, "reason": "模型服务地址缺少合法 HTTP/HTTPS scheme 或主机"}
        else:
            port = endpoint.port or (443 if endpoint.scheme == "https" else 80)
            with socket.create_connection((endpoint.hostname, port), timeout=5):
                result = {"ok": True, "reason": "配置地址直连 TCP 连接成功"}
    except socket.gaierror as exc:
        result = {"ok": False, "reason": "配置地址 DNS 解析失败", "errno": exc.errno}
    except ConnectionRefusedError as exc:
        result = {"ok": False, "reason": "配置地址直连 TCP 连接被拒绝（服务没有监听该端口或主动拒绝）", "errno": exc.errno}
    except TimeoutError:
        result = {"ok": False, "reason": "配置地址直连 TCP 连接超时"}
    except (OSError, ValueError) as exc:
        result = {"ok": False, "reason": "配置地址直连 TCP 连接失败", "type": type(exc).__name__, "errno": getattr(exc, "errno", None)}
    output.put(result)


def endpoint_connectivity(config):
    # DNS 和多地址等待也受总限时；这里只诊断直连，不替代含代理设置的真实请求。
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    process = context.Process(target=_connectivity_worker, args=(config["base_url"], output))
    try:
        process.start()
        process.join(timeout=8)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
            result = {"ok": False, "reason": "配置地址直连 TCP/DNS 诊断超过 8 秒总限时"}
        else:
            try:
                result = output.get(timeout=1)
            except Exception:
                result = {"ok": False, "reason": "配置地址直连诊断进程没有返回结果"}
        return {"kind": "direct_tcp", **result}
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        output.close()
        output.join_thread()


def _read_source(connection_config, database, query, params=None):
    """每次读取单独建立强制只读事务，不持有业务连接等待网络诊断。"""
    with pymysql.connect(**connection_config, database=database,
                         cursorclass=pymysql.cursors.DictCursor) as source:
        with source.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute(query, params)
            rows = cursor.fetchall()
            source.rollback()
            return rows


def read_default_configs(connection_config, database, result, secrets):
    """排除明显模拟配置，选择同一用户真实且 TCP 可达的默认 write/review 配置。"""
    rows = _read_source(connection_config, database, """SELECT id, user_id, name, agent_type,
        base_url, api_key_enc, model, system_prompt, skills, params_json FROM model_config
        WHERE is_default = 1 AND agent_type IN ('', 'write', 'review') ORDER BY id""")
    result["default_config_count"] = len(rows)
    simulated, incomplete, candidates, by_user = 0, 0, [], {}
    for row in rows:
        endpoint = urlsplit(row["base_url"] or "")
        secrets.extend([row["base_url"] or "", row["api_key_enc"] or "", endpoint.hostname or "",
                        endpoint.username or "", endpoint.password or ""])
        model, name, host = str(row["model"] or "").lower(), str(row["name"] or "").lower(), str(endpoint.hostname or "").lower()
        if (host.endswith((".invalid", ".example"))
                or re.search(r"^(?:test|offline|mock|dummy|fake|synthetic)(?:[-_]|$)", model)
                or any(marker in name for marker in ("offline", "mock", "synthetic", "isolated", "模拟", "离线", "隔离验收"))):
            simulated += 1
            continue
        if not all(str(row.get(key) or "").strip() for key in ("base_url", "api_key_enc", "model")):
            incomplete += 1
            continue
        candidates.append(row)
        by_user.setdefault(row["user_id"], []).append(row)
    result["excluded_simulated_configs"], result["excluded_incomplete_configs"] = simulated, incomplete
    result["real_default_config_count"] = len(candidates)
    print(f"Read-only default config scan: {len(rows)} total, {simulated} simulated excluded, {len(candidates)} real candidates", flush=True)
    if not candidates:
        raise VerificationBlocked("缺少可用真实配置：默认模型均为明显模拟配置或配置不完整")
    probes, selected = {}, None
    for configs in by_user.values():
        pair = {}
        for role in ("write", "review"):
            preferred = [item for item in configs if item["agent_type"] == role]
            fallback = [item for item in configs if item["agent_type"] == ""]
            for item in preferred + fallback:
                url = item["base_url"]
                if url not in probes:
                    probes[url] = endpoint_connectivity(item)
                    print("Real config reachability:", probes[url]["reason"], flush=True)
                if probes[url]["ok"]:
                    pair[role] = item
                    break
        if len(pair) == 2:
            selected = pair
            break
    result["real_config_connectivity"] = list(probes.values())
    if selected is None:
        raise VerificationBlocked("缺少可用真实配置：未找到同一用户真实且 TCP 可达的默认 write/review 或全局配置")
    selected_ids = sorted({item["id"] for item in selected.values()})
    placeholders = ",".join(["%s"] * len(selected_ids))
    try:
        attached = _read_source(connection_config, database, f"""SELECT m.model_config_id, m.position,
            s.id, s.name, s.description, s.content, s.source_type FROM model_config_skill m
            JOIN skill s ON s.id = m.skill_id WHERE m.model_config_id IN ({placeholders})
            ORDER BY m.position, s.id""", selected_ids)
    except pymysql.err.ProgrammingError as exc:
        if exc.args[0] != 1146:
            raise
        attached = []
    return selected, attached, {role: probes[item["base_url"]] for role, item in selected.items()}


async def run_pipeline(user_id, project_id, result, artifact_dir):
    from sqlalchemy import select
    from app import assemble, evidence, generate, operations, storage
    from app.db import SessionLocal
    from app.models import ChapterTask, Project
    from app.routers import projects

    def pass_step(name, **details):
        result["steps"].append({"name": name, "ok": True, **details})
        print("PASS", name, flush=True)

    result["stage"] = "red_instruction_analysis"
    with operations.project_transaction(project_id) as (db, project):
        token = operations.begin_run(db, project, "analyzing")
    await projects.run_analysis(project_id, user_id, token)
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        if project.status != "analyzed":
            raise VerificationBlocked("红字分析失败：" + project.error_msg)
        tasks = db.scalars(select(ChapterTask).where(
            ChapterTask.project_id == project_id, ChapterTask.task_type == "text")).all()
        if len(tasks) != 1 or not project.evidence_path:
            raise VerificationBlocked("红字分析没有产生唯一文字任务和证据快照")
        task_id = tasks[0].id
    pass_step("红字模板分析及证据快照发布")

    result["stage"] = "real_write_generation"
    with operations.project_transaction(project_id) as (db, project):
        token = operations.begin_run(db, project, "generating")
    with SessionLocal() as db:
        project, task = db.get(Project, project_id), db.get(ChapterTask, task_id)
        row = await generate.generate_task(db, project, task, run_token=token)
        if row.status != "ok":
            raise VerificationBlocked("真实写作失败：" + row.error_msg)
        ok, note = evidence.validate_claims(json.loads(row.evidence_json),
            evidence.load_or_build_project_bundle(db, project), row.content)
        if not ok:
            raise VerificationBlocked("严格统计绑定复核失败：" + note)
        result["generated"] = {"content": row.content, "claims": json.loads(row.evidence_json)}
    operations.finish_run(project_id, token, "analyzed", {"succeeded": 1, "failed": 0, "total": 1})
    pass_step("真实 write 模型生成且严格统计绑定通过")

    result["stage"] = "real_review_and_export"
    with operations.project_transaction(project_id) as (db, project):
        token = operations.begin_run(db, project, "assembling")
    with SessionLocal() as db:
        project = db.get(Project, project_id)
        # 直接执行产品导出入口，含确定性审核、真实 AI 终审、装配与渲染门槛。
        object_path = await assemble.assemble_report(db, project, run_token=token)
        report_bytes = storage.download_bytes(object_path)
    (artifact_dir / "report.docx").write_bytes(report_bytes)
    operations.finish_run(project_id, token, "analyzed", {"succeeded": 1, "failed": 0, "total": 1})
    pass_step("真实 review 模型终审及实际 DOCX 导出")
    # 保留渲染产物，临时目录随后清理，不保留配置和模型请求日志。
    scratch = Path(tempfile.gettempdir()) / "ai_report_sandbox"
    render_outputs = sorted(scratch.glob(f"p{project_id}/t0/*/qa"))
    if render_outputs:
        qa = render_outputs[-1]
        for path in qa.iterdir():
            if path.is_file() and path.suffix in {".pdf", ".png", ".txt"}:
                shutil.copy2(path, artifact_dir / path.name)
    pass_step("DOCX 渲染门槛通过，保留 PDF 和页面图像")


def main():
    started = time.monotonic()
    artifact_dir = ROOT / "validation_artifacts" / (
        "real_model_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8])
    artifact_dir.mkdir(parents=True)
    result = {"ok": False, "stage": "configuration", "steps": [],
              "model_requests_are_real": True, "business_database_access": "read_only",
              "materials": "synthetic 6-row CSV and red-instruction DOCX"}
    secrets = []

    def safe(value):
        text = str(value)
        for secret in sorted((item for item in secrets if item), key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = re.sub(r"https?://[^\s\"'<>]+", "[endpoint]", text)
        text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[redacted]", text)
        return text[:2500]

    engine, created, exit_code, connection_config = None, False, 1, None
    test_database = "ai_report_real_test_" + uuid4().hex
    assert re.fullmatch(r"ai_report_real_test_[0-9a-f]{32}", test_database)
    logging.disable(logging.CRITICAL)  # 业务异常日志可能带服务 URL，验收只输出脱敏结果。
    with tempfile.TemporaryDirectory(prefix="ai-report-real-") as temporary:
        old_tempdir = tempfile.tempdir
        try:
            values = dotenv_values(ROOT / ".env")
            connection_config = {
                "host": str(values.get("MYSQL_HOST") or "127.0.0.1"),
                "port": int(values.get("MYSQL_PORT") or 3306),
                "user": str(values.get("MYSQL_USER") or "root"),
                "password": str(values.get("MYSQL_PASSWORD") or ""),
                "charset": str(values.get("MYSQL_CHARSET") or "utf8mb4"),
                "connect_timeout": 5, "read_timeout": 10, "write_timeout": 10, "autocommit": True,
            }
            business_database = str(values.get("MYSQL_DATABASE") or "ai_report_platform")
            secrets.extend([connection_config["password"], connection_config["host"],
                            str(values.get("MODEL_CONFIG_ENCRYPTION_KEY") or "")])
            selected, attached, connectivity = read_default_configs(connection_config, business_database, result, secrets)
            for config in selected.values():
                secrets.extend([config["base_url"], config["api_key_enc"]])
                endpoint = urlsplit(config["base_url"])
                secrets.extend([endpoint.hostname or "", endpoint.username or "", endpoint.password or ""])
            result["models"] = {role: config["model"] for role, config in selected.items()}
            result["config_selection"] = "exclude simulated configs; first user with both real TCP-reachable default roles; role default precedes global default"
            result["connectivity"] = connectivity
            print("Default write/review configurations selected using read-only connection", flush=True)
            for role, probe in result["connectivity"].items():
                print("Model service", role, probe["reason"], flush=True)
            with pymysql.connect(**connection_config) as admin:
                with admin.cursor() as cursor:
                    cursor.execute(f"CREATE DATABASE `{test_database}` CHARACTER SET utf8mb4")
            created = True
            temporary_path = Path(temporary)
            (temporary_path / "scratch").mkdir()
            tempfile.tempdir = str(temporary_path / "scratch")
            os.environ.update({
                "AI_REPORT_ENV_FILE": "", "DB_ENGINE": "mysql", "MYSQL_DATABASE": test_database,
                "MYSQL_HOST": connection_config["host"], "MYSQL_PORT": str(connection_config["port"]),
                "MYSQL_USER": connection_config["user"], "MYSQL_PASSWORD": connection_config["password"],
                "MYSQL_CHARSET": connection_config["charset"], "MYSQL_RESET_ON_STARTUP": "false",
                "STORAGE_BACKEND": "local", "STORAGE_LOCAL_DIR": str(temporary_path / "objects"),
                "MODEL_CONFIG_ENCRYPTION_KEY": str(values.get("MODEL_CONFIG_ENCRYPTION_KEY") or ""),
                "LLM_CA_BUNDLE": str(values.get("LLM_CA_BUNDLE") or ""),
                "LLM_REQUEST_TIMEOUT": "180", "LLM_FIRST_CHUNK_TIMEOUT": "180",
                "LLM_STREAM_IDLE_TIMEOUT": "90", "LLM_MAX_RETRIES": "0", "REACT_MAX_TURNS": "3",
            })
            from docx import Document
            from docx.shared import RGBColor
            from app import storage
            from app.db import SessionLocal, engine, init_db
            from app.models import ModelConfig, ModelConfigSkill, Project, ProjectFile, Skill, User
            from app.security import decrypt_key

            init_db()
            for config in selected.values():
                secrets.append(decrypt_key(config["api_key_enc"]))
            doc = Document()
            doc.add_paragraph("最小真实模型验收报告")
            doc.add_heading("调查结果", level=1)
            instruction = doc.add_paragraph().add_run(
                "【填写：依据态度调查数据写一段简短、客观的描述。写明总体样本量、支持和反对的人数及占比；"
                "不得把这个小样本外推到其他群体。统计结果可由数据文件复算，可直接 finish 提交正文和逐数字统计绑定。】")
            instruction.font.color.rgb = RGBColor(255, 0, 0)
            template_buffer = io.BytesIO()
            doc.save(template_buffer)
            template_raw = template_buffer.getvalue()
            data_raw = "态度\n支持\n支持\n支持\n支持\n反对\n反对\n".encode()
            with SessionLocal() as db:
                user = User(username="real-model-test-" + uuid4().hex, password_hash="unused")
                db.add(user)
                db.flush()
                source_to_copy, skill_copies = {}, {}
                for role, config in selected.items():
                    copied = ModelConfig(user_id=user.id, name="隔离验收 " + role, agent_type=role,
                        base_url=config["base_url"], api_key_enc=config["api_key_enc"], model=config["model"],
                        system_prompt=config["system_prompt"] or "", skills=config["skills"] or "[]",
                        params_json=config["params_json"] or "{}", is_default=True)
                    db.add(copied)
                    db.flush()
                    source_to_copy.setdefault(config["id"], []).append(copied.id)
                for item in attached:
                    if item["id"] not in skill_copies:
                        skill = Skill(user_id=user.id, name=item["name"], description=item["description"] or "",
                            content=item["content"] or "", source_type=item["source_type"] or "manual")
                        db.add(skill)
                        db.flush()
                        skill_copies[item["id"]] = skill.id
                    for config_id in source_to_copy[item["model_config_id"]]:
                        db.add(ModelConfigSkill(model_config_id=config_id, skill_id=skill_copies[item["id"]],
                            position=item["position"]))
                project = Project(user_id=user.id, name="最小真实模型验收", status="created")
                db.add(project)
                db.flush()
                for kind, name, raw in (("template", "fixture.docx", template_raw), ("data", "fixture.csv", data_raw)):
                    object_path = f"{user.id}/{project.id}/{kind}/{name}"
                    storage.upload_bytes(object_path, raw)
                    db.add(ProjectFile(project_id=project.id, file_type=kind, file_name=name,
                        minio_path=object_path, size=len(raw)))
                db.commit()
                ids = user.id, project.id
            asyncio.run(asyncio.wait_for(run_pipeline(*ids, result, artifact_dir), timeout=660))
            result["ok"], result["stage"] = True, "complete"
            print("ALL REAL MODEL CHECKS PASSED", flush=True)
            exit_code = 0
        except pymysql.MySQLError as exc:
            result["error"] = "MySQL 访问或独立测试库权限失败，code=" + str(exc.args[0] if exc.args else "unknown")
            print("BLOCKED", result["error"], flush=True)
            exit_code = 2
        except Exception as exc:
            result["error"] = safe(type(exc).__name__ + ": " + str(exc))
            print("BLOCKED", result["stage"], result["error"], flush=True)
            exit_code = 1
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                except Exception as exc:
                    result["engine_cleanup_error"] = safe(type(exc).__name__)
                    exit_code = 1
            if created:
                try:
                    # 长模型调用后旧连接可能闲置断线，删除采用新短连接。
                    with pymysql.connect(**connection_config) as cleanup:
                        with cleanup.cursor() as cursor:
                            cursor.execute(f"DROP DATABASE `{test_database}`")
                    result["isolated_database_deleted"] = True
                    print("Dropped isolated real-model test database", flush=True)
                except Exception as exc:
                    result["isolated_database_deleted"] = False
                    result["cleanup_error"] = safe(type(exc).__name__)
                    result["cleanup_database"] = test_database
                    print("FAIL isolated test database cleanup:", type(exc).__name__, flush=True)
                    exit_code = 1
            tempfile.tempdir = old_tempdir
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            if exit_code:
                result["ok"] = False
            # 对所有产物字段扫描已知密钥/端点，避免模型复述配置提示中的敏感值。
            serialized = json.dumps(result, ensure_ascii=False, indent=2)
            for secret in sorted((item for item in secrets if item), key=len, reverse=True):
                serialized = serialized.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[redacted]")
            (artifact_dir / "result.json").write_text(serialized, encoding="utf-8")
            print("Validation result:", str(artifact_dir / "result.json"), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
