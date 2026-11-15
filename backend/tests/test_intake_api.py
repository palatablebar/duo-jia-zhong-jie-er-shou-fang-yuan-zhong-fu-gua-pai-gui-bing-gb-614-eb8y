"""HTTP 端到端：采集批次 → 分诊 → 重算 → 补正重试 → 撤回 → 银行清单（含鉴权与 RBAC）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database as database_module
from src.config import schema_bootstrap as schema_bootstrap
from src.config.settings import settings
from src.main import create_app
from src.models import Base
from src.models.agency import Agency
from src.models.listing import Listing
from src.utils.security import create_access_token


def _item(lid: str, **overrides) -> dict:
    base = {
        "lid": lid,
        "community_raw": "翠湖名邸",
        "layout_raw": "3室2厅1卫",
        "area_raw": "89.5㎡",
        "price_raw": "598万",
        "floor_raw": "2/18",
        "orientation_raw": "南北通透",
        "title": "3-2-1 89.5平方米 南北 2层 满五唯一",
        "status": "active",
        "listed_days": 30,
        "views": 5,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    originals = (database_module.engine, database_module.SessionLocal, schema_bootstrap.engine)
    database_module.engine = engine
    database_module.SessionLocal = factory
    schema_bootstrap.engine = engine

    session = factory()
    session.add(Agency(code="demo", name="演示中介", kind="broker", enabled=True))
    session.commit()
    session.close()

    app = create_app()
    # 测试内存库不触发演示 seed
    settings.seed_demo = False
    with TestClient(app) as test_client:
        yield test_client, factory
    settings.seed_demo = True
    database_module.engine, database_module.SessionLocal, schema_bootstrap.engine = originals
    engine.dispose()


def _auth(role: str = "analyst") -> dict:
    token, _ = create_access_token(f"{role}@listingmerge.io", role)
    return {"Authorization": f"Bearer {token}"}


def test_full_intake_to_bank_book_flow(client):
    test_client, factory = client

    # 1) 批次入库：完整 / 待补 / 不合规
    resp = test_client.post(
        "/api/intake/batches",
        headers=_auth("analyst"),
        json={
            "batch_no": "B-HTTP-1",
            "agency_code": "demo",
            "items": [
                _item("OK-1"),
                _item("OK-2", price_raw="602万"),
                _item("PEND-1", price_raw="价格面议"),
                _item("BAD-1", area_raw="9999㎡"),
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert (batch["complete_count"], batch["incomplete_count"], batch["invalid_count"]) == (2, 1, 1)

    # 同批重试不新增
    again = test_client.post(
        "/api/intake/batches",
        headers=_auth("analyst"),
        json={"batch_no": "B-HTTP-1", "agency_code": "demo", "items": [
            _item("OK-1"), _item("OK-2", price_raw="602万"),
            _item("PEND-1", price_raw="价格面议"), _item("BAD-1", area_raw="9999㎡"),
        ]},
    )
    assert again.status_code == 201
    assert again.json()["created_count"] == 0
    assert again.json()["unchanged_count"] == 4

    # 2) 挂牌明细支持 stage 过滤
    pending = test_client.get("/api/listings", headers=_auth(), params={"stage": "incomplete"})
    assert pending.status_code == 200
    assert [row["lid"] for row in pending.json()["items"]] == ["PEND-1"]
    assert pending.json()["items"][0]["can_merge"] is False

    # 3) 全量重算：待补/不合规不入簇
    run = test_client.post("/api/runs", headers=_auth("analyst"), json={"trigger": "manual", "threshold": 0.8})
    assert run.status_code == 201, run.text
    metrics = run.json()["metrics"]
    assert metrics["eligible"] == 2 and metrics["excluded"] == 2
    assert metrics["clusters"] == 1

    # 4) 补正：先失败（rejected，记录不丢），再成功重新入列
    with factory() as db:
        pend_id = db.scalar(select(Listing.id).where(Listing.lid == "PEND-1"))
    bad_correction = test_client.post(
        f"/api/listings/{pend_id}/corrections",
        headers=_auth("analyst"),
        json={"changes": {"price_raw": "面谈"}},
    )
    assert bad_correction.status_code == 200
    assert bad_correction.json()["stage"] == "incomplete"
    assert bad_correction.json()["corrections"][0]["status"] == "rejected"

    good_correction = test_client.post(
        f"/api/listings/{pend_id}/corrections",
        headers=_auth("analyst"),
        json={"changes": {"price_raw": "606万"}, "reason": "门店补回"},
    )
    assert good_correction.status_code == 200
    assert good_correction.json()["stage"] == "complete"
    assert good_correction.json()["can_merge"] is True

    rerun = test_client.post("/api/runs", headers=_auth("analyst"), json={"trigger": "manual"})
    assert rerun.json()["metrics"]["readmitted"] >= 1
    assert rerun.json()["metrics"]["eligible"] == 3

    # 5) 撤回一条：出簇且不进银行清单
    with factory() as db:
        ok2_id = db.scalar(select(Listing.id).where(Listing.lid == "OK-2"))
    withdrawn = test_client.post(
        f"/api/listings/{ok2_id}/withdraw", headers=_auth("analyst"), json={"reason": "业主撤牌"}
    )
    assert withdrawn.status_code == 200
    test_client.post("/api/runs", headers=_auth("analyst"), json={"trigger": "manual"})

    # 6) 银行清单：覆盖范围 + 待补数量，无未完成/已撤回
    #    （PEND-1 已补正为完整；当前剩 1 条不合规 BAD-1，撤回 1 条 OK-2）
    bank = test_client.get("/api/exports/bank-book.csv", headers=_auth("analyst"))
    assert bank.status_code == 200
    text = bank.text
    assert "覆盖范围" in text and "不合规 1 条" in text and "已撤回/下架 1 条" in text
    assert "PEND-1" not in text and "BAD-1" not in text and "OK-2" not in text

    # 7) 总览带分诊计数（PEND-1 已补正为完整，仅剩 BAD-1 不合规）
    overview = test_client.get("/api/overview", headers=_auth()).json()
    assert overview["stage"]["incomplete"] == 0
    assert overview["stage"]["invalid"] == 1
    assert overview["withdrawn_total"] == 1

    # 8) 批次台账可读
    batches = test_client.get("/api/intake/batches", headers=_auth()).json()
    assert batches["total"] == 1 and batches["items"][0]["batch_no"] == "B-HTTP-1"


def test_rbac_viewer_cannot_ingest_or_export(client):
    test_client, _ = client
    denied = test_client.post(
        "/api/intake/batches",
        headers=_auth("viewer"),
        json={"batch_no": "B-X", "agency_code": "demo", "items": [_item("L")]},
    )
    assert denied.status_code == 403
    assert test_client.get("/api/exports/bank-book.csv", headers=_auth("viewer")).status_code == 403
    # 只读仍可看批次台账与挂牌
    assert test_client.get("/api/intake/batches", headers=_auth("viewer")).status_code == 200
    assert test_client.get("/api/listings", headers=_auth("viewer")).status_code == 200


def test_unauthenticated_requests_are_401(client):
    test_client, _ = client
    assert test_client.get("/api/listings").status_code == 401
    assert test_client.get("/api/intake/batches").status_code == 401
