"""采集分诊服务层测试：批次幂等、补正重试、撤回不丢记录、流水线闸口与银行清单。"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.models.agency import Agency
from src.models.ingest_batch import IngestBatch
from src.models.listing import Listing
from src.models.listing_correction import ListingCorrection
from src.models.match_run import MatchRun
from src.models.property import Property
from src.services import export_service, intake_service, pipeline_service


def _agency(db, code="demo", name="演示中介") -> Agency:
    agency = Agency(code=code, name=name, kind="broker", owner="测试")
    db.add(agency)
    db.flush()
    return agency


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


def _run_pipeline(db) -> dict:
    sequence = (db.scalar(select(func.count()).select_from(MatchRun)) or 0) + 1
    run = MatchRun(
        run_no=f"RTEST-{sequence:04d}", trigger="manual", status="running", operator_email="t@example.io"
    )
    db.add(run)
    db.flush()
    result = pipeline_service.run_pipeline(db, run, threshold=0.80)
    db.flush()
    return result


# --------------------------------------------------------------------------- #
# 分诊纯函数
# --------------------------------------------------------------------------- #


def test_classify_complete_pending_invalid():
    from src.services import normalizer

    complete = intake_service.classify(normalizer.normalize_listing(_item("A")))
    assert complete == ("complete", [])

    pending_price = intake_service.classify(
        normalizer.normalize_listing(_item("B", price_raw="价格面议"))
    )
    assert pending_price[0] == "incomplete"
    assert "price_missing" in pending_price[1]

    pending_area = intake_service.classify(
        normalizer.normalize_listing(_item("C", area_raw="面积待核实"))
    )
    assert pending_area[0] == "incomplete"
    assert "area_missing" in pending_area[1]

    invalid_area = intake_service.classify(normalizer.normalize_listing(_item("D", area_raw="9999㎡")))
    assert invalid_area[0] == "invalid"
    assert "area_out_of_range" in invalid_area[1]

    invalid_community = intake_service.classify(
        normalizer.normalize_listing(_item("E", community_raw=""))
    )
    assert invalid_community[0] == "invalid"
    assert "community_missing" in invalid_community[1]


# --------------------------------------------------------------------------- #
# 批次入库与幂等
# --------------------------------------------------------------------------- #


def test_ingest_batch_idempotent_no_duplicate_and_raw_archived(db):
    agency = _agency(db)
    items = [_item("L-1"), _item("L-2", price_raw="价格面议"), _item("L-3", area_raw="9999㎡")]
    batch = intake_service.ingest_items(
        db, batch_no="B-1", agency_code=agency.code, items=items, submitted_by="a@x.io"
    )
    assert batch.received_count == 3
    assert (batch.complete_count, batch.incomplete_count, batch.invalid_count) == (1, 1, 1)
    assert batch.created_count == 3
    assert db.scalar(select(func.count()).select_from(Listing)) == 3

    # 原文快照留档
    first = db.scalar(select(Listing).where(Listing.lid == "L-1"))
    assert first.raw_payload["price_raw"] == "598万"
    assert first.ingest_batch == "B-1"

    # 同批重试：内容未变 -> 0 新增、3 unchanged
    again = intake_service.ingest_items(
        db, batch_no="B-1", agency_code=agency.code, items=items, submitted_by="a@x.io"
    )
    assert again.created_count == 0
    assert again.unchanged_count == 3
    assert db.scalar(select(func.count()).select_from(Listing)) == 3
    assert db.scalar(select(func.count()).select_from(IngestBatch)) == 1


def test_ingest_batch_update_vs_unchanged_and_duplicate_lid_rejected(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db, batch_no="B-2", agency_code=agency.code, items=[_item("L-9")], submitted_by="a@x.io"
    )
    changed = intake_service.ingest_items(
        db,
        batch_no="B-2",
        agency_code=agency.code,
        items=[_item("L-9", price_raw="605万")],
        submitted_by="a@x.io",
    )
    assert changed.updated_count == 1
    assert changed.unchanged_count == 0
    listing = db.scalar(select(Listing).where(Listing.lid == "L-9"))
    assert listing.price_raw == "605万"
    assert listing.raw_payload["price_raw"] == "598万"  # 原文快照不被覆盖

    with pytest.raises(Exception):
        intake_service.ingest_items(
            db,
            batch_no="B-3",
            agency_code=agency.code,
            items=[_item("X"), _item("X")],
            submitted_by="a@x.io",
        )
    db.rollback()


def test_unknown_agency_404(db):
    with pytest.raises(Exception):
        intake_service.ingest_items(
            db, batch_no="B-X", agency_code="nope", items=[_item("L")], submitted_by="a@x.io"
        )
    db.rollback()


# --------------------------------------------------------------------------- #
# 流水线闸口
# --------------------------------------------------------------------------- #


def test_pipeline_excludes_pending_and_invalid_from_clusters(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-4",
        agency_code=agency.code,
        items=[
            _item("OK-1"),
            _item("OK-2", price_raw="602万"),
            _item("PEND-1", price_raw="价格面议"),
            _item("BAD-1", area_raw="9999㎡"),
        ],
        submitted_by="a@x.io",
    )
    result = _run_pipeline(db)
    metrics = result["metrics"]
    assert metrics["listings"] == 4
    assert metrics["eligible"] == 2
    assert metrics["excluded"] == 2
    assert metrics["incomplete"] == 1
    assert metrics["invalid"] == 1
    assert metrics["clusters"] == 1  # 两条完整记录归并成一个簇

    clustered = db.scalars(select(Listing).where(Listing.property_id.is_not(None))).all()
    assert {row.lid for row in clustered} == {"OK-1", "OK-2"}
    blocked = db.scalars(select(Listing).where(Listing.property_id.is_(None))).all()
    assert {row.lid for row in blocked} == {"PEND-1", "BAD-1"}
    # 被拦记录仍然留档
    assert all(row.ingest_stage in {"incomplete", "invalid"} for row in blocked)


def test_correction_readmits_listing_without_changing_other_clusters(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-5",
        agency_code=agency.code,
        items=[
            _item("OK-1"),
            _item("OK-2", price_raw="602万"),
            _item("PEND-1", price_raw="价格面议"),
        ],
        submitted_by="a@x.io",
    )
    first = _run_pipeline(db)
    assert first["metrics"]["clusters"] == 1
    prop_before = db.scalar(select(Property))
    golden_before = (prop_before.price_median, prop_before.area_mode)

    # 补正价格：待补 -> 完整
    pending = db.scalar(select(Listing).where(Listing.lid == "PEND-1"))
    outcome = intake_service.apply_correction(
        db,
        pending,
        {"price_raw": "606万"},
        submitted_by="a@x.io",
        batch_no="B-5",
        reason="门店补回价格",
    )
    assert outcome["stage"] == "complete"
    assert outcome["can_merge"] is True

    second = _run_pipeline(db)
    assert second["metrics"]["readmitted"] >= 1
    assert second["metrics"]["eligible"] == 3
    db.expire_all()
    # 原两条完整记录仍在同一个簇；面积众数不变，价格中位按新成员确定性重算
    ok_rows = db.scalars(select(Listing).where(Listing.lid.in_(["OK-1", "OK-2"]))).all()
    new_property_id = ok_rows[0].property_id
    assert new_property_id is not None
    assert len({row.property_id for row in ok_rows}) == 1
    prop_after = db.scalar(select(Property).where(Property.id == new_property_id))
    assert prop_after is not None
    assert prop_after.area_mode == golden_before[1]
    # 补正记录已重新入列，且与原记录同簇（缺项补齐后没有另起炉灶）
    refreshed = db.scalar(select(Listing).where(Listing.lid == "PEND-1"))
    assert refreshed.property_id == prop_after.id
    # 补正留痕
    corrections = db.scalars(select(ListingCorrection)).all()
    assert len(corrections) == 1
    assert corrections[0].status == "applied"
    assert corrections[0].before == "价格面议"


def test_failed_correction_keeps_record_and_logs_rejected(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-6",
        agency_code=agency.code,
        items=[_item("PEND-1", price_raw="价格面议")],
        submitted_by="a@x.io",
    )
    listing = db.scalar(select(Listing).where(Listing.lid == "PEND-1"))
    original_price = listing.price_raw
    # 补正成另一个无法解析的价格 -> 驳回，原记录不动
    outcome = intake_service.reject_correction(
        db,
        listing,
        {"price_raw": "面谈"},
        submitted_by="a@x.io",
        batch_no="B-6",
        reason="补正后仍缺价格",
    )
    assert outcome["stage"] == "incomplete"
    assert listing.price_raw == original_price
    assert listing.ingest_stage == "incomplete"
    row = db.scalar(select(ListingCorrection))
    assert row.status == "rejected"
    assert row.reason == "补正后仍缺价格"
    # 记录没有丢
    assert db.scalar(select(func.count()).select_from(Listing)) == 1


def test_withdraw_removes_listing_from_clusters_but_keeps_record(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-7",
        agency_code=agency.code,
        items=[_item("OK-1"), _item("OK-2", price_raw="602万")],
        submitted_by="a@x.io",
    )
    _run_pipeline(db)
    target = db.scalar(select(Listing).where(Listing.lid == "OK-1"))
    assert target.property_id is not None

    intake_service.withdraw(db, target, submitted_by="a@x.io", reason="业主撤牌")
    assert target.status == "off_market"
    assert target.withdrawn_at is not None
    assert target.property_id is None

    result = _run_pipeline(db)
    assert result["metrics"]["withdrawn"] == 1
    assert result["metrics"]["eligible"] == 1
    # 记录仍在库中
    assert db.scalar(select(func.count()).select_from(Listing)) == 2
    again = db.scalar(select(Listing).where(Listing.lid == "OK-1"))
    assert again.property_id is None


def test_repeated_pipeline_is_stable(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-8",
        agency_code=agency.code,
        items=[_item("OK-1"), _item("OK-2", price_raw="602万"), _item("PEND-1", price_raw="价格面议")],
        submitted_by="a@x.io",
    )
    first = _run_pipeline(db)
    keys_first = sorted(prop.cluster_key for prop in db.scalars(select(Property)).all())
    second = _run_pipeline(db)
    keys_second = sorted(prop.cluster_key for prop in db.scalars(select(Property)).all())
    # 未变化数据保持稳定：簇集合一致，计数一致（耗时与重入台账除外）
    volatile = {"duration_ms", "readmitted"}
    assert {k: v for k, v in first["metrics"].items() if k not in volatile} == {
        k: v for k, v in second["metrics"].items() if k not in volatile
    }
    assert keys_first == keys_second


# --------------------------------------------------------------------------- #
# 银行清单
# --------------------------------------------------------------------------- #


def test_bank_book_excludes_incomplete_invalid_withdrawn_and_shows_coverage(db):
    agency = _agency(db, code="demo", name="演示中介")
    intake_service.ingest_items(
        db,
        batch_no="B-9",
        agency_code=agency.code,
        items=[
            _item("OK-1"),
            _item("OK-2", price_raw="602万"),
            _item("PEND-1", price_raw="价格面议"),
            _item("BAD-1", area_raw="9999㎡"),
        ],
        submitted_by="a@x.io",
    )
    withdrawn = db.scalar(select(Listing).where(Listing.lid == "OK-2"))
    intake_service.withdraw(db, withdrawn, submitted_by="a@x.io", reason="撤牌")
    _run_pipeline(db)

    content = export_service.bank_book_csv(db).decode("utf-8")
    # 覆盖范围与待补数量
    assert "覆盖范围" in content
    assert "待补 1 条" in content
    assert "不合规 1 条" in content
    assert "已撤回/下架 1 条" in content
    # 未完成 / 不合规 / 撤回房源编号不出现
    assert "PEND-1" not in content
    assert "BAD-1" not in content
    assert "OK-2" not in content
    # 合规在售簇保留
    assert "翠湖名邸" in content

    clusters_csv = export_service.clusters_csv(db).decode("utf-8")
    assert "待补数量" in clusters_csv
    assert "PEND-1" not in clusters_csv


def test_listings_csv_keeps_full_archive_with_stage_columns(db):
    agency = _agency(db)
    intake_service.ingest_items(
        db,
        batch_no="B-10",
        agency_code=agency.code,
        items=[_item("OK-1"), _item("PEND-1", price_raw="价格面议")],
        submitted_by="a@x.io",
    )
    content = export_service.listings_csv(db).decode("utf-8")
    assert "采集分诊" in content
    assert "待补" in content
    assert "完整" in content
    assert "B-10" in content
    assert "PEND-1" in content  # 内部全量留档导出仍包含待补记录
