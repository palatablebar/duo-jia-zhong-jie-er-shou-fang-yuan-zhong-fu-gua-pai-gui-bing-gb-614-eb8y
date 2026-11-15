"""测试基座：sqlite 内存库 + 全量建表，替换运行时 engine/session（含延迟导入模块的绑定）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database as database_module
from src.models import Base


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = session_factory()

    # database 模块的公共绑定，与其它模块 `from ... import engine` 后持有的引用都要替换
    import src.config.schema_bootstrap as schema_bootstrap

    originals = {
        "database.engine": database_module.engine,
        "database.SessionLocal": database_module.SessionLocal,
        "schema_bootstrap.engine": schema_bootstrap.engine,
    }
    database_module.engine = engine
    database_module.SessionLocal = session_factory
    schema_bootstrap.engine = engine
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        database_module.engine = originals["database.engine"]
        database_module.SessionLocal = originals["database.SessionLocal"]
        schema_bootstrap.engine = originals["schema_bootstrap.engine"]
