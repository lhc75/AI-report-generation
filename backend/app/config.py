"""应用配置：全部从项目根目录的 .env 读取。"""
from pathlib import Path
import os

from sqlalchemy.engine import URL
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("AI_REPORT_ENV_FILE", str(ROOT_DIR / ".env")) or None,
        env_file_encoding="utf-8", extra="ignore"
    )

    # 数据库：桌面客户端默认使用内置 SQLite；服务器部署可通过 .env 切回 MySQL
    DB_ENGINE: str = "sqlite"
    SQLITE_PATH: str = str(ROOT_DIR / "data" / "app.db")
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DATABASE: str = "ai_report_platform"
    MYSQL_CHARSET: str = "utf8mb4"
    MYSQL_RESET_ON_STARTUP: bool = False

    # 文件存储：桌面客户端默认写入本地数据目录；服务器部署可切回 MinIO
    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_DIR: str = str(ROOT_DIR / "data" / "storage")
    OSS_ENDPOINT: str = "http://127.0.0.1:9000"
    OSS_ACCESS_KEY: str = "minioadmin"
    OSS_SECRET_KEY: str = "minioadmin"
    OSS_BUCKET: str = "knowledge-media"
    OSS_PRESIGN_EXPIRE_SECONDS: int = 3600

    # 安全
    MODEL_CONFIG_ENCRYPTION_KEY: str = ""
    JWT_SECRET_KEY: str = "change-me"
    JWT_EXPIRE_MINUTES: int = 720

    # 服务
    BACKEND_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173"

    # 生成
    GENERATE_CONCURRENCY: int = 5  # 同时执行的生成任务上限，超出排队
    REVIEW_CONCURRENCY: int = 5  # 导出前逐模块 AI 复核的并发上限（网关不稳时可调小）
    # ReAct 智能体：默认不设轮次上限（0），由时间预算与卡死检测终止；旧配置可继续设正整数作硬上限。
    REACT_MAX_TURNS: int = 0  # ReAct 单任务最大思考-行动轮次；0 = 不限制
    REACT_MAX_SECONDS: float = 3600.0  # 单任务实际运行时间上限（秒），0 关闭；防止无限重试烧额度
    REACT_MAX_RUN_FAILURES: int = 8  # 连续 run_python 执行失败达到此次数判定任务不可完成
    REACT_STUCK_REPEAT_LIMIT: int = 3  # 同一动作与同一观察连续重复达到该次数判定卡死
    REACT_STALE_FINISH_NUDGE: int = 6  # 连续仅执行代码未提交成果达到该轮数时注入 finish 提醒
    REACT_FAILURE_NUDGE: int = 2  # run_python 连续失败达到该次数时提示模型先分析原因再换做法
    # ReAct 上下文保护：接近模型窗口时自动压缩历史（东亚字符按 1 token 保守估计）
    REACT_CONTEXT_WINDOW_TOKENS: int = 128000  # 模型上下文窗口（token），按实际模型调整
    REACT_CONTEXT_COMPRESS_RATIO: float = 0.75  # 估算值超过窗口的该比例即触发压缩
    REACT_CONTEXT_KEEP_TURNS: int = 4  # 压缩后保留的最近「助手回复+观察」组数

    # 模版 AI 分析
    ANALYSIS_JSON_REPAIR_ATTEMPTS: int = 2  # 分析输出非法 JSON 时让模型自修复的额外次数（0 关闭）

    # 本机生成脚本隔离（macOS sandbox-exec / Windows 作业对象）；配置仅影响默认值，调用方可显式覆盖。
    SCRIPT_SANDBOX_BACKEND: str = "auto"
    SCRIPT_SANDBOX_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    SCRIPT_SANDBOX_CPU_SECONDS: int = Field(default=15, gt=0)
    SCRIPT_SANDBOX_MEMORY_MB: int = Field(default=512, gt=0)
    SCRIPT_SANDBOX_MAX_OUTPUT_BYTES: int = Field(default=10 * 1024 * 1024, gt=0)
    SCRIPT_SANDBOX_MAX_LOG_BYTES: int = Field(default=1024 * 1024, gt=0)

    # 应用版本与更新（桌面端会把真实版本写入 APP_VERSION）
    APP_VERSION: str = "0.0.0"  # 当前客户端版本；0.0.0 表示开发版
    UPDATE_REPO: str = "lhc75/AI-report-generation"  # GitHub 仓库（owner/repo），用于检查 Release
    UPDATE_CHECK_TIMEOUT: float = 12.0  # 访问 GitHub API 的超时时间（秒）
    UPDATE_CACHE_SECONDS: int = 300  # 版本检查结果缓存时间，避免频繁请求触发限流
    AI_REPORT_DESKTOP: bool = False  # 由桌面端进程写入，浏览器部署保持 False

    # 模型调用
    LLM_CA_BUNDLE: str = ""  # 可选 PEM CA 文件；相对路径以项目根目录为基准
    LLM_TRUST_ENV: bool = True  # 读取环境及 macOS 系统代理；False 仅让模型请求直连
    LLM_REQUEST_TIMEOUT: float = 1800.0  # 单次模型请求整体限时（秒），本地/推理模型可适当调大
    LLM_FIRST_CHUNK_TIMEOUT: float = 600.0  # 等待模型首个响应数据的上限（秒），覆盖本地慢模型的长 prefill
    LLM_STREAM_IDLE_TIMEOUT: float = 120.0  # 流式响应相邻数据块的最大间隔（秒），超过判定连接停滞并重试
    LLM_MAX_RETRIES: int = 2  # 模型请求停滞或服务端临时故障时的自动重试次数

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def mysql_url(self) -> URL:
        return URL.create(
            "mysql+pymysql", username=self.MYSQL_USER, password=self.MYSQL_PASSWORD,
            host=self.MYSQL_HOST, port=self.MYSQL_PORT, database=self.MYSQL_DATABASE,
            query={"charset": self.MYSQL_CHARSET},
        )

    @property
    def minio_endpoint(self) -> str:
        return self.OSS_ENDPOINT.replace("http://", "").replace("https://", "")

    @property
    def minio_secure(self) -> bool:
        return self.OSS_ENDPOINT.startswith("https")


settings = Settings()
