"""Products, banks, stores CRUD."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import json
import re

from ..auth import CurrentUser, get_current_user, require_role
from ..database import get_db
from ..models import Bank, HOWarehouse, Inventory, Product, Setting, Store
from ..schemas import BankIn, BankOut, ProductIn, ProductOut, StoreIn, StoreOut
from ..services.inventory import get_stock, set_ho_warehouse_qty

router = APIRouter(prefix="/api", tags=["catalog"])

CATEGORIES_SETTING_KEY = "product_categories"
DEFAULT_CATEGORIES = ["Running", "Casual", "Basketball", "Training", "Kids", "Slippers", "Other"]


def _load_categories(db: Session) -> list[str]:
    row = db.query(Setting).filter(Setting.key == CATEGORIES_SETTING_KEY).first()
    if not row or not row.value:
        return list(DEFAULT_CATEGORIES)
    try:
        cats = json.loads(row.value)
        if isinstance(cats, list) and cats:
            return [str(c) for c in cats]
    except Exception:  # noqa: BLE001
        pass
    return list(DEFAULT_CATEGORIES)


def _save_categories(db: Session, cats: list[str]) -> None:
    row = db.query(Setting).filter(Setting.key == CATEGORIES_SETTING_KEY).first()
    value = json.dumps(cats)
    if row:
        row.value = value
    else:
        db.add(Setting(key=CATEGORIES_SETTING_KEY, value=value))


@router.get("/categories")
def list_categories(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    return {"ok": True, "status": "ok", "categories": _load_categories(db)}


@router.post("/categories")
def add_category(
    body: dict,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    name = str((body or {}).get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    cats = _load_categories(db)
    if not any(c.lower() == name.lower() for c in cats):
        cats.append(name)
        _save_categories(db, cats)
        db.commit()
    return {"ok": True, "status": "ok", "categories": cats}


@router.delete("/categories/{name}")
def delete_category(
    name: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    cats = _load_categories(db)
    cats = [c for c in cats if c.lower() != name.lower()]
    if not cats:
        cats = list(DEFAULT_CATEGORIES)
    _save_categories(db, cats)
    db.commit()
    return {"ok": True, "status": "ok", "categories": cats}


@router.get("/products", response_model=list[ProductOut])
def list_products(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
    q: Optional[str] = None,
    store_id: Optional[str] = None,
    active_only: bool = True,
):
    query = db.query(Product)
    if active_only:
        query = query.filter(Product.active.is_(True))
    if q:
        like = f"%{q}%"
        query = query.filter((Product.name.ilike(like)) | (Product.barcode.ilike(like)))
    rows = query.order_by(Product.name).all()
    sid = store_id or user.store_id
    out = []
    for p in rows:
        stock = get_stock(db, p.barcode, sid) if sid and sid != "HO" else None
        out.append(
            ProductOut(
                barcode=p.barcode,
                name=p.name,
                brand=p.brand or "ANTA",
                category=p.category or "",
                size=p.size or "",
                color=getattr(p, "color", "") or "",
                department=getattr(p, "department", "") or "",
                season=getattr(p, "season", "") or "",
                gender=getattr(p, "gender", "") or "",
                cost=p.cost or 0,
                retail=p.retail or 0,
                reorder=p.reorder or 5,
                opening=p.opening or 0,
                active=bool(p.active),
                stock=stock,
            )
        )
    return out


@router.post("/products", response_model=ProductOut)
def save_product(
    body: ProductIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    lookup_barcode = body.old_barcode or body.barcode
    row = db.query(Product).filter(Product.barcode == lookup_barcode).first()
    renaming = bool(body.old_barcode and body.old_barcode != body.barcode)
    if renaming:
        clash = db.query(Product).filter(Product.barcode == body.barcode).first()
        if clash and (not row or clash.id != row.id):
            raise HTTPException(status_code=400, detail=f"Barcode {body.barcode} is already used by another product")
    if row:
        if renaming:
            row.barcode = body.barcode
            db.query(Inventory).filter(Inventory.barcode == lookup_barcode).update(
                {Inventory.barcode: body.barcode}, synchronize_session=False
            )
            db.query(HOWarehouse).filter(HOWarehouse.barcode == lookup_barcode).update(
                {HOWarehouse.barcode: body.barcode}, synchronize_session=False
            )
        row.name = body.name
        row.brand = body.brand
        row.category = body.category
        row.size = body.size
        row.color = body.color
        row.department = body.department
        row.season = body.season
        row.gender = body.gender
        row.cost = body.cost
        row.retail = body.retail
        row.reorder = body.reorder
        row.opening = body.opening
        row.active = body.active
    else:
        row = Product(
            barcode=body.barcode,
            name=body.name,
            brand=body.brand,
            category=body.category,
            color=body.color,
            department=body.department,
            season=body.season,
            gender=body.gender,
            size=body.size,
            cost=body.cost,
            retail=body.retail,
            reorder=body.reorder,
            opening=body.opening,
            active=body.active,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    if body.qty is not None:
        set_ho_warehouse_qty(db, body.barcode, body.name, body.qty)
        db.commit()
    return ProductOut(
        barcode=row.barcode,
        name=row.name,
        brand=row.brand,
        category=row.category,
        size=row.size,
        color=getattr(row, "color", "") or "",
        department=getattr(row, "department", "") or "",
        season=getattr(row, "season", "") or "",
        gender=getattr(row, "gender", "") or "",
        cost=row.cost,
        retail=row.retail,
        reorder=row.reorder,
        opening=row.opening,
        active=row.active,
    )


@router.post("/products/bulk")
def bulk_save_products(
    body: list[dict],
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    """Upsert many products, row by row, so ONE bad row never costs the rest.

    Each row is validated and committed on its own using a SAVEPOINT
    (db.begin_nested). If a row fails — bad value, duplicate barcode,
    DB constraint, anything — only that row's savepoint is rolled back;
    every other row in the same request still gets saved. This is what
    makes a 1000+ row bulk upload safe: a handful of bad rows can no
    longer silently wipe out an entire batch of otherwise-good data.

    Returns per-row results (`results`) in the same order as the input,
    so the caller can build a full pass/fail event log — not just totals.
    """
    raw_barcodes = [str((r or {}).get("barcode", "")).strip() for r in body]
    existing = {
        p.barcode: p
        for p in db.query(Product).filter(Product.barcode.in_([b for b in raw_barcodes if b])).all()
    }
    created = 0
    updated = 0
    errors: list[str] = []
    results: list[dict] = []
    qty_updates: dict[str, tuple[int, str]] = {}

    for raw in body:
        bc_for_error = str((raw or {}).get("barcode", "?")).strip() or "?"
        name_for_error = str((raw or {}).get("name", "")).strip()
        try:
            item = ProductIn(**(raw or {}))
        except Exception as e:  # noqa: BLE001 — pydantic ValidationError etc.
            # Extract a cleaner error message from Pydantic validation errors
            error_msg = str(e)
            if 'validation error' in error_msg.lower():
                try:
                    if 'Input should be' in error_msg:
                        match = re.search(r'(\w+)\s+Input should be', error_msg)
                        if match:
                            field = match.group(1)
                            error_msg = f"Invalid {field} — {error_msg.split('Input should be')[1].split('[')[0].strip()}"
                except Exception:
                    pass
            msg = f"{bc_for_error}: {error_msg}"
            errors.append(msg)
            results.append({"barcode": bc_for_error, "name": name_for_error, "status": "failed", "reason": error_msg})
            continue
        if not item.barcode or not item.name:
            msg = f"{bc_for_error}: missing barcode or name"
            errors.append(msg)
            results.append({"barcode": bc_for_error, "name": name_for_error, "status": "failed", "reason": "missing barcode or name"})
            continue

        # Each row gets its own savepoint so a failure here can't drag
        # down any of the other rows already staged in this request.
        savepoint = db.begin_nested()
        try:
            row = existing.get(item.barcode)
            is_update = row is not None
            if row:
                row.name = item.name
                row.brand = item.brand
                row.category = item.category
                row.size = item.size
                row.color = item.color
                row.department = item.department
                row.season = item.season
                row.gender = item.gender
                row.cost = item.cost
                row.retail = item.retail
                row.reorder = item.reorder
                row.opening = item.opening
                row.active = item.active
            else:
                row = Product(
                    barcode=item.barcode, name=item.name, brand=item.brand, category=item.category,
                    size=item.size, color=item.color, department=item.department, season=item.season,
                    gender=item.gender, cost=item.cost, retail=item.retail, reorder=item.reorder,
                    opening=item.opening, active=item.active,
                )
                db.add(row)
            db.flush()
            savepoint.commit()
        except Exception as e:  # noqa: BLE001 — IntegrityError etc, scoped to this row only
            savepoint.rollback()
            msg = f"{item.barcode}: {e}"
            errors.append(msg)
            results.append({"barcode": item.barcode, "name": item.name, "status": "failed", "reason": str(e)})
            continue

        existing[item.barcode] = row
        if is_update:
            updated += 1
            results.append({"barcode": item.barcode, "name": item.name, "status": "updated", "reason": ""})
        else:
            created += 1
            results.append({"barcode": item.barcode, "name": item.name, "status": "created", "reason": ""})
        if item.qty is not None:
            qty_updates[item.barcode] = (item.qty, item.name)

    db.commit()

    # Warehouse quantity sync also gets its own per-row savepoint — a bad
    # quantity on one barcode must not undo the product rows above, which
    # are already safely committed by this point.
    for bc, (qty, name) in qty_updates.items():
        savepoint = db.begin_nested()
        try:
            set_ho_warehouse_qty(db, bc, name, qty)
            savepoint.commit()
        except Exception as e:  # noqa: BLE001
            savepoint.rollback()
            errors.append(f"{bc}: stock qty not updated — {e}")
    if qty_updates:
        db.commit()

    return {"ok": True, "status": "ok", "created": created, "updated": updated, "errors": errors, "results": results}


@router.delete("/products/{barcode}")
def delete_product(
    barcode: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    row = db.query(Product).filter(Product.barcode == barcode).first()
    if not row:
        raise HTTPException(status_code=404, detail="Product not found")
    db.delete(row)
    db.commit()
    return {"ok": True, "status": "ok"}


@router.post("/products/bulk-delete")
def bulk_delete_products(
    barcodes: list[str],
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin", "manager"))],
):
    n = db.query(Product).filter(Product.barcode.in_(barcodes)).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "status": "ok", "deleted": n}


@router.get("/banks", response_model=list[BankOut])
def list_banks(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    rows = db.query(Bank).filter(Bank.active.is_(True)).order_by(Bank.id).all()
    return [
        BankOut(
            bank_id=b.bank_id,
            name=b.name,
            account_no=b.account_no or "",
            device=b.device or "",
            active=b.active,
            icon=b.icon or "💳",
        )
        for b in rows
    ]


@router.post("/banks", response_model=BankOut)
def save_bank(
    body: BankIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
):
    bid = body.bank_id or f"B{int(__import__('time').time())}"
    row = db.query(Bank).filter(Bank.bank_id == bid).first()
    if row:
        row.name = body.name
        row.account_no = body.account_no
        row.device = body.device
        row.active = body.active
        row.icon = body.icon
    else:
        row = Bank(
            bank_id=bid,
            name=body.name,
            account_no=body.account_no,
            device=body.device,
            active=body.active,
            icon=body.icon,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return BankOut(
        bank_id=row.bank_id,
        name=row.name,
        account_no=row.account_no or "",
        device=row.device or "",
        active=row.active,
        icon=row.icon or "💳",
    )


@router.get("/stores/all", response_model=list[StoreOut])
def all_stores(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(get_current_user)],
):
    rows = db.query(Store).order_by(Store.store_id).all()
    return [
        StoreOut(
            store_id=r.store_id,
            name=r.name,
            city=r.city or "",
            address=r.address or "",
            manager=r.manager or "",
            phone=r.phone or "",
            active=r.active,
        )
        for r in rows
    ]


@router.post("/stores", response_model=StoreOut)
def save_store(
    body: StoreIn,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[CurrentUser, Depends(require_role("admin"))],
):
    row = db.query(Store).filter(Store.store_id == body.store_id).first()
    if row:
        row.name = body.name
        row.city = body.city
        row.address = body.address
        row.manager = body.manager
        row.phone = body.phone
        row.active = body.active
    else:
        row = Store(
            store_id=body.store_id,
            name=body.name,
            city=body.city,
            address=body.address,
            manager=body.manager,
            phone=body.phone,
            active=body.active,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return StoreOut(
        store_id=row.store_id,
        name=row.name,
        city=row.city or "",
        address=row.address or "",
        manager=row.manager or "",
        phone=row.phone or "",
        active=row.active,
    )
