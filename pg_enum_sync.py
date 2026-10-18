"""PostgreSQL ENUM turlariga keyinroq qo'shilgan qiymatlarni bazaga qo'shadi.

create_all mavjud enum turini O'ZGARTIRMAYDI. Shuning uchun modelga yangi qiymat qo'shilgach (masalan
TransactionType.ORDER_FEE), eski bazada "invalid input value for enum transactiontype: ORDER_FEE" xatosi chiqadi.
Server ishga tushganda chaqiriladi: await sync_pg_enums(engine, Base)."""
from sqlalchemy import Enum as SAEnum, text


async def sync_pg_enums(engine, Base):
    try:
        if engine.dialect.name != "postgresql":
            return
        wanted = {}
        for table in Base.metadata.sorted_tables:
            for col in table.columns:
                t = col.type
                if isinstance(t, SAEnum) and getattr(t, "name", None):
                    wanted.setdefault(t.name, set()).update(list(t.enums or []))
        async with engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            for tname, labels in wanted.items():
                rows = (await conn.execute(text(
                    "SELECT e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid WHERE t.typname = :n"),
                    {"n": tname})).all()
                if not rows:
                    continue
                have = {r[0] for r in rows}
                for lab in sorted(labels - have):
                    safe = lab.replace("'", "''")
                    await conn.execute(text(f"ALTER TYPE \"{tname}\" ADD VALUE IF NOT EXISTS '{safe}'"))
                    print(f"[ENUM MIGRATSIYA] {tname} ga '{lab}' qo'shildi")
    except Exception as e:
        print(f"[ENUM MIGRATSIYA XATOSI] {e!r}")
