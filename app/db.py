from datetime import date, datetime
from typing import Optional

from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import DB_PATH

STORES = ["amazon", "ezydvd", "ebay", "jbhifi", "umbrella", "dvdhub"]
STORE_NAMES = {
    "amazon": "Amazon AU",
    "ezydvd": "EzyDVD",
    "ebay": "eBay AU",
    "jbhifi": "JB Hi-Fi AU",
    "umbrella": "Umbrella Entertainment",
    "dvdhub": "DVD Hub",
}


class Title(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    # blu-ray.com is the source of truth for release information.
    bluray_id: Optional[str] = Field(default=None, index=True)
    bluray_url: Optional[str] = None
    name: str
    year: Optional[str] = None
    edition: Optional[str] = None  # e.g. "JB Hi-Fi Exclusive SteelBook"
    country: Optional[str] = None
    is_4k: bool = False
    upc: Optional[str] = Field(default=None, index=True)
    release_date: Optional[date] = None
    cover_url: Optional[str] = None
    studio: Optional[str] = None
    runtime: Optional[str] = None
    rating: Optional[str] = None
    region: Optional[str] = None

    # Wishlist bookkeeping
    source: str = "manual"  # manual | amazon
    source_title: Optional[str] = None  # original title as seen at the source
    amazon_asin: Optional[str] = None
    search_terms: Optional[str] = None  # user override for store searches
    target_price: Optional[float] = None
    notes: Optional[str] = None
    needs_review: bool = False  # blu-ray.com match needs confirming
    purchased: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_checked: Optional[datetime] = None

    # Denormalised cache of the current best offer
    best_price: Optional[float] = None
    best_store: Optional[str] = None
    best_url: Optional[str] = None
    lowest_ever: Optional[float] = None
    last_notified_price: Optional[float] = None


class Listing(SQLModel, table=True):
    """The product a title is matched to at a store."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True)
    store: str = Field(index=True)
    url: Optional[str] = None
    product_title: Optional[str] = None
    image: Optional[str] = None
    price: Optional[float] = None
    shipping: Optional[float] = None
    in_stock: Optional[bool] = None
    condition: Optional[str] = None
    match_score: Optional[float] = None
    # pinned = user confirmed this URL; never auto-replace it
    pinned: bool = False
    status: str = "pending"  # pending | ok | review | not_found | error
    error: Optional[str] = None
    checked_at: Optional[datetime] = None
    # JSON list of other plausible matches the user can pick from
    candidates_json: Optional[str] = None


class ExcludedUrl(SQLModel, table=True):
    """URLs the user rejected as wrong matches."""

    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True)
    store: str
    url: str


class PricePoint(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title_id: int = Field(foreign_key="title.id", index=True)
    store: str
    price: float  # item price
    total: float  # price + shipping
    in_stock: bool = True
    url: Optional[str] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow, index=True)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


engine = create_engine(
    f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False, "timeout": 30}
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    # WAL lets the web UI read/write while the background checker is working.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        migration_key = "enabled_stores_umbrella_dvdhub_v1"
        if session.get(Setting, migration_key) is None:
            setting = session.get(Setting, "enabled_stores")
            if setting:
                enabled = setting.value.split(",")
                for store in ("umbrella", "dvdhub"):
                    if store not in enabled:
                        enabled.append(store)
                setting.value = ",".join(store for store in enabled if store in STORES)
            session.add(Setting(key=migration_key, value="1"))
            session.commit()

        remove_zavvi_key = "enabled_stores_remove_zavvi_v1"
        if session.get(Setting, remove_zavvi_key) is None:
            setting = session.get(Setting, "enabled_stores")
            if setting:
                setting.value = ",".join(
                    store for store in setting.value.split(",") if store in STORES
                )
            session.add(Setting(key=remove_zavvi_key, value="1"))
            session.commit()


def get_session() -> Session:
    return Session(engine)


def get_setting(key: str, default: str = "") -> str:
    with get_session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with get_session() as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()


def enabled_stores() -> list[str]:
    raw = get_setting("enabled_stores", ",".join(STORES))
    return [x for x in raw.split(",") if x in STORES]


__all__ = [
    "Title", "Listing", "ExcludedUrl", "PricePoint", "Setting", "engine",
    "init_db", "get_session", "get_setting", "set_setting", "enabled_stores",
    "STORES", "STORE_NAMES", "select",
]
