import os
from dotenv import load_dotenv
import sqlalchemy as sa
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import urllib.parse
from datetime import datetime, timedelta
from database.resilience import setup_retry_events
import re
import unicodedata

load_dotenv()

# Configuração de Conexão
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME")

# Encode password for special characters (@, etc)
if DB_PASSWORD is None:
    print("WARNING: DB_PASSWORD is None. Check if .env is loaded correctly.")
    DB_PASSWORD = ""
encoded_password = urllib.parse.quote_plus(DB_PASSWORD)

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{encoded_password}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Configurar timezone no MySQL (GMT-3)
engine = sa.create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=5,
    pool_recycle=60,         # Reciclar a cada 60 segundos
    pool_pre_ping=True,      # Verificar se a conexão está viva antes de usar
    pool_use_lifo=True,
    connect_args={
        "init_command": "SET time_zone = '-03:00'",
        "connect_timeout": 10,
    }
)

# Configurar eventos de resiliência e reconexão automática
setup_retry_events(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_local_now():
    """Retorna o horário atual ajustado para GMT-3"""
    return datetime.utcnow() - timedelta(hours=3)

class Category(Base):
    __tablename__ = "categories"
    id       = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name     = sa.Column(sa.String(100), nullable=False)
    slug     = sa.Column(sa.String(80), unique=True)
    icon     = sa.Column(sa.String(10))
    parent_id = sa.Column(sa.Integer, sa.ForeignKey("categories.id"))
    children  = relationship("Category")
    products  = relationship("Product", back_populates="category")

class CategoryRawMap(Base):
    """Mapeia raw_category dos scrapers → categoria canônica."""
    __tablename__ = "category_raw_map"
    id           = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    raw_category = sa.Column(sa.String(255), nullable=False, unique=True)
    category_id  = sa.Column(sa.Integer, sa.ForeignKey("categories.id"), nullable=False)
    category     = relationship("Category")

class Product(Base):
    __tablename__ = "products"
    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    ean = sa.Column(sa.String(20), unique=True, nullable=True)
    sku_fallback = sa.Column(sa.String(100), unique=True, nullable=True)
    slug = sa.Column(sa.String(255), unique=True, index=True)
    name_canonical = sa.Column(sa.String(255), nullable=False)
    brand_canonical = sa.Column(sa.String(100))
    category_id = sa.Column(sa.Integer, sa.ForeignKey("categories.id"))
    category    = relationship("Category", back_populates="products")
    description = sa.Column(sa.Text)
    image_url = sa.Column(sa.String(512))
    unit_magnitude = sa.Column(sa.Numeric(10, 3))
    unit_measure = sa.Column(sa.String(10))
    weight_volume = sa.Column(sa.String(50))
    metadata_json = sa.Column(sa.JSON)
    created_at = sa.Column(sa.DateTime, default=get_local_now)
    updated_at = sa.Column(sa.DateTime, default=get_local_now, onupdate=get_local_now)

    @staticmethod
    def slugify(text: str):
        if not text: return None
        # Remover acentos
        n = "".join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn').lower()
        # Substituir caracteres não alfanuméricos por hifen
        n = re.sub(r'[^a-z0-9]+', '-', n)
        # Remover hifens duplicados e hifens no início/fim
        n = re.sub(r'-+', '-', n).strip('-')
        return n

@sa.event.listens_for(Product, 'before_insert')
def receive_before_insert(mapper, connection, target):
    if target.name_canonical:
        if not target.slug:
            target.slug = Product.slugify(target.name_canonical)

@sa.event.listens_for(Product, 'before_update')
def receive_before_update(mapper, connection, target):
    # Get the state of the object to check if name_canonical changed
    try:
        state = sa.inspect(target)
        if state:
            history = state.get_history('name_canonical', True)
            if history.has_changes():
                # Update slug if the name changed significantly
                target.slug = Product.slugify(target.name_canonical)
    except Exception:
        pass # Better safe than sorry in event listeners

class ProductRawSource(Base):
    __tablename__ = "product_raw_sources"
    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    product_id = sa.Column(sa.BigInteger, sa.ForeignKey("products.id"))
    store_id = sa.Column(sa.Integer, sa.ForeignKey("stores.id"))
    raw_sku = sa.Column(sa.String(100))
    raw_name = sa.Column(sa.String(255))
    raw_brand = sa.Column(sa.String(100))
    raw_category = sa.Column(sa.String(255))
    raw_image_url = sa.Column(sa.String(512))
    store_url = sa.Column(sa.String(512))   # URL da página do produto na loja de origem
    first_seen = sa.Column(sa.DateTime, default=get_local_now)
    last_seen = sa.Column(sa.DateTime, default=get_local_now, onupdate=get_local_now)

    product = relationship("Product")
    store   = relationship("Store")

class Store(Base):
    __tablename__ = "stores"
    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name = sa.Column(sa.String(100), nullable=False)
    slug = sa.Column(sa.String(150), unique=True, index=True)
    unit = sa.Column(sa.String(100), nullable=False)
    city = sa.Column(sa.String(100))
    state = sa.Column(sa.String(2))
    platform = sa.Column(sa.String(50))
    external_id = sa.Column(sa.String(100))
    # Migração 2026_11_001: fuso IANA e país (ISO-3166-1 alfa-2) da loja.
    timezone = sa.Column(sa.String(64), nullable=False, server_default="America/Sao_Paulo")
    country = sa.Column(sa.CHAR(2), nullable=False, server_default="BR")

class PriceHistory(Base):
    __tablename__ = "price_history"
    id = sa.Column(sa.BigInteger, primary_key=True, autoincrement=True)
    product_id = sa.Column(sa.BigInteger, sa.ForeignKey("products.id"), nullable=False)
    store_id = sa.Column(sa.Integer, sa.ForeignKey("stores.id"), nullable=False)
    price = sa.Column(sa.Numeric(10, 2), nullable=False)
    price_regular = sa.Column(sa.Numeric(10, 2))
    unit_price_normalized = sa.Column(sa.Numeric(10, 2))
    is_promotion = sa.Column(sa.Boolean, default=False)
    store_url = sa.Column(sa.String(512))   # URL da página do produto no momento da coleta
    is_replicated = sa.Column(sa.Boolean, default=False) # Indica se o preço foi replicado de outra loja
    captured_at = sa.Column(sa.DateTime, default=get_local_now)

class CurrentPrice(Base):
    """
    Tabela materializada com o ÚLTIMO preço de cada (produto, loja).
    Atualizada por UPSERT a cada nova coleta — substitui MAX(captured_at)
    nas consultas de "preço atual", sem tocar em price_history.
    """
    __tablename__ = "current_prices"

    product_id            = sa.Column(sa.BigInteger, sa.ForeignKey("products.id"), primary_key=True)
    store_id              = sa.Column(sa.Integer,    sa.ForeignKey("stores.id"),   primary_key=True)
    price                 = sa.Column(sa.Numeric(10, 2), nullable=False)
    price_regular         = sa.Column(sa.Numeric(10, 2))
    unit_price_normalized = sa.Column(sa.Numeric(10, 2))
    is_promotion          = sa.Column(sa.Boolean, default=False)
    store_url             = sa.Column(sa.String(512))
    captured_at           = sa.Column(sa.DateTime, nullable=False)
    is_replicated         = sa.Column(sa.Boolean, default=False)

    product = relationship("Product")
    store   = relationship("Store")


class Basket(Base):
    """Cestas pré-definidas (Ex: Família 3 pessoas, Churrasco, etc)"""
    __tablename__ = "baskets"
    id          = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    name        = sa.Column(sa.String(100), nullable=False)
    slug        = sa.Column(sa.String(100), unique=True)
    description = sa.Column(sa.Text)
    icon        = sa.Column(sa.String(20)) # Emoji ou classe FontAwesome
    items       = relationship("BasketItem", back_populates="basket")

class BasketItem(Base):
    """Produtos que compõem uma cesta e suas quantidades sugeridas"""
    __tablename__ = "basket_items"
    id         = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    basket_id  = sa.Column(sa.Integer, sa.ForeignKey("baskets.id"), nullable=False)
    product_id = sa.Column(sa.BigInteger, sa.ForeignKey("products.id"), nullable=False)
    quantity   = sa.Column(sa.Numeric(10, 2), default=1.0) # Quantidade mensal/evento
    
    basket     = relationship("Basket", back_populates="items")
    product    = relationship("Product")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
