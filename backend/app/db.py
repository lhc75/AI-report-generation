"""数据库连接与初始化。"""
import pymysql
from sqlalchemy import create_engine, BigInteger, event, inspect
from sqlalchemy.ext.compiler import compiles
from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


@compiles(BigInteger, "sqlite")
def _sqlite_primary_key(element, compiler, **kw):
    return "INTEGER"


def ensure_database() -> None:
    """数据库不存在时自动创建。"""
    conn = pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        charset=settings.MYSQL_CHARSET,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.MYSQL_DATABASE}` "
                f"DEFAULT CHARACTER SET {settings.MYSQL_CHARSET}"
            )
        conn.commit()
    finally:
        conn.close()


if settings.DB_ENGINE == "sqlite":
    if not settings.SQLITE_PATH:
        raise ValueError("SQLite 测试环境必须显式设置 SQLITE_PATH")
    from pathlib import Path as _Path
    _sqlite_file = _Path(settings.SQLITE_PATH)
    _sqlite_file.parent.mkdir(parents=True, exist_ok=True)
    # Windows 路径的反斜杠不能直接拼进 SQLAlchemy URL，统一转成正斜杠。
    _sqlite_url = "sqlite:///" + _sqlite_file.as_posix()
    engine = create_engine(_sqlite_url, connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def _sqlite_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
else:
    engine = create_engine(settings.mysql_url, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    if settings.DB_ENGINE == "mysql":
        ensure_database()
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_column_width()


# 已有表的增量列：(表名, 列名, DDL)
_COLUMN_MIGRATIONS = [
    ("project", "report_path", "ALTER TABLE `project` ADD COLUMN `report_path` VARCHAR(512) NOT NULL DEFAULT ''"),
    ("project", "evidence_path", "ALTER TABLE `project` ADD COLUMN `evidence_path` VARCHAR(512) NOT NULL DEFAULT ''"),
    ("chapter_task", "slot_key", "ALTER TABLE `chapter_task` ADD COLUMN `slot_key` VARCHAR(512) NOT NULL DEFAULT ''"),
    ("chapter_task", "anchor_task_id", "ALTER TABLE `chapter_task` ADD COLUMN `anchor_task_id` BIGINT NULL"),
    ("module_content", "evidence_json", ""),
    ("module_content", "review_json", "ALTER TABLE `module_content` ADD COLUMN `review_json` TEXT NOT NULL DEFAULT '{}'"),
]

for _table, _column, _type, _default in [
    ("project", "operation_token", "VARCHAR(36)", "''"),
    ("project", "material_revision", "INTEGER", "0"),
    ("project", "last_run_id", "VARCHAR(36)", "''"),
    ("project", "last_run_result", "TEXT", "'{}'"),
    ("project", "progress_json", "TEXT", "'{}'"),
    ("project_file", "statistics_config", "TEXT", "'{\"fields\":[]}'"),
    ("chapter_task", "instruction_revision", "INTEGER", "0"),
    ("chapter_task", "template_title", "VARCHAR(512)", "''"),
    ("chapter_task", "template_instruction", "TEXT", "''"),
    ("module_content", "material_revision", "INTEGER", "0"),
    ("module_content", "instruction_revision", "INTEGER", "0"),
    ("module_content", "failure_digest", "TEXT", "''"),
]:
    _COLUMN_MIGRATIONS.append((_table, _column,
        f"ALTER TABLE `{_table}` ADD COLUMN `{_column}` {_type} NOT NULL DEFAULT {_default}"))


# 生成内容可能超过 TEXT 的 64KB 上限；存量 MySQL 表启动时拓宽为 MEDIUMTEXT。
_WIDEN_TO_MEDIUMTEXT = [
    ("module_content", "content"),
    ("module_content", "evidence_json"),
]


def _ensure_column_width() -> None:
    if engine.dialect.name != "mysql":
        return
    with engine.connect() as conn:
        for table, column in _WIDEN_TO_MEDIUMTEXT:
            row = conn.execute(sqlalchemy_text(
                "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t AND COLUMN_NAME = :c"
            ), {"t": table, "c": column}).first()
            if row and str(row[0]).lower() == "text":
                conn.execute(sqlalchemy_text(
                    f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` MEDIUMTEXT NOT NULL"))
        conn.commit()


def _ensure_columns() -> None:
    """create_all 不会给已存在的表补列，这里按 information_schema 做增量补齐。"""
    known = {table: {column['name'] for column in inspect(engine).get_columns(table)}
             for table in {item[0] for item in _COLUMN_MIGRATIONS}}
    with engine.connect() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            exists = column in known[table]
            if not exists:
                if table == "module_content" and column == "evidence_json":
                    if engine.dialect.name == "sqlite":
                        conn.execute(sqlalchemy_text(
                            "ALTER TABLE `module_content` ADD COLUMN `evidence_json` TEXT NOT NULL DEFAULT '[]'"))
                        continue
                    # MySQL 对已有行直接 ADD TEXT NOT NULL 的兼容性不一致：
                    # 先允许 NULL、回填旧内容，再收紧为非空，旧版本内容会被
                    # 导出审核识别为缺少证据而阻止导出。
                    conn.execute(
                        sqlalchemy_text(
                            "ALTER TABLE `module_content` "
                            "ADD COLUMN `evidence_json` TEXT NULL"
                        )
                    )
                    conn.execute(
                        sqlalchemy_text(
                            "UPDATE `module_content` SET `evidence_json` = '[]' "
                            "WHERE `evidence_json` IS NULL"
                        )
                    )
                    conn.execute(
                        sqlalchemy_text(
                            "ALTER TABLE `module_content` "
                            "MODIFY COLUMN `evidence_json` TEXT NOT NULL"
                        )
                    )
                else:
                    if " TEXT NOT NULL DEFAULT " in ddl and engine.dialect.name == "mysql":
                        declaration, default = ddl.rsplit(" NOT NULL DEFAULT ", 1)
                        conn.execute(sqlalchemy_text(declaration + " NULL"))
                        conn.execute(sqlalchemy_text(
                            f"UPDATE `{table}` SET `{column}` = {default} WHERE `{column}` IS NULL"))
                        conn.execute(sqlalchemy_text(
                            f"ALTER TABLE `{table}` MODIFY COLUMN `{column}` TEXT NOT NULL"))
                    else:
                        conn.execute(sqlalchemy_text(ddl))
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
