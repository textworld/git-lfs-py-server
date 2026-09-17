"""应用配置：从环境变量 / .env 文件读取"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ---- Server ----
    server_base_url: str = "http://localhost:8000"
    storage_prefix: str = "lfs"
    max_object_size: int = 5 * 1024 * 1024 * 1024  # 5GB

    # ---- Auth ----
    lfs_user: str = "lfs"
    lfs_password: str = "change-me-please"

    # ---- Qiniu ----
    qiniu_access_key: str = ""
    qiniu_secret_key: str = ""
    qiniu_bucket: str = ""
    qiniu_bucket_domain: str = ""
    qiniu_bucket_private: bool = True

    # ---- Metadata ----
    metadata_db_path: str = "./lfs_metadata.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def api_base(self) -> str:
        """LFS API 的基础路径，所有对象 URL 都拼这个前缀"""
        return f"{self.server_base_url.rstrip('/')}/api/v1"

    def validate_qiniu(self) -> None:
        """启动时校验七牛云必填配置"""
        missing = [
            name
            for name, value in [
                ("QINIU_ACCESS_KEY", self.qiniu_access_key),
                ("QINIU_SECRET_KEY", self.qiniu_secret_key),
                ("QINIU_BUCKET", self.qiniu_bucket),
                ("QINIU_BUCKET_DOMAIN", self.qiniu_bucket_domain),
            ]
            if not value or value.startswith("your_")
        ]
        if missing:
            raise RuntimeError(
                f"七牛云配置缺失或不正确: {', '.join(missing)}。请检查 .env 文件。"
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    """获取全局 settings 单例（带校验）"""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
        _settings.validate_qiniu()
    return _settings


def reload_settings() -> Settings:
    """重新加载（用于测试）"""
    global _settings
    _settings = None
    return get_settings()
