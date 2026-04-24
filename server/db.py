from contextlib import contextmanager
from sqlalchemy import create_engine, Column, Integer, String, Numeric, DateTime, ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, relationship, Session
from sqlalchemy.sql import func
import os

DATABASE_URL = os.getenv(
    "CARVALHAES_DATABASE_URL",
    "postgresql://carvalhaes:carvalhaes123@localhost:5433/carvalhaes",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Fornecedor(Base):
    __tablename__ = "fornecedores"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    nome               = Column(String(200), nullable=False)
    nome_representante = Column(String(200))
    whatsapp           = Column(String(50))
    email_cotacao      = Column(String(200))
    email_pedido       = Column(String(200))
    contato_nome       = Column(String(200))
    contato_tel        = Column(String(50))
    contato_email      = Column(String(200))
    prazo_entrega      = Column(Integer)
    compra_minima      = Column(Numeric(12, 2))
    cond_pagamento     = Column(String(500))
    desconto_volume    = Column(Text)
    criado_em          = Column(DateTime, server_default=func.now())

    tabelas = relationship("TabelaPreco", back_populates="fornecedor", cascade="all, delete-orphan")


class TabelaPreco(Base):
    __tablename__ = "tabelas_preco"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    fornecedor_id = Column(Integer, ForeignKey("fornecedores.id", ondelete="CASCADE"), nullable=False)
    data_upload   = Column(DateTime, server_default=func.now())
    arquivo_nome  = Column(String(500))
    arquivo_path  = Column(String(1000))
    arquivo_tipo  = Column(String(10))   # pdf, xls, xlsx, txt, jpg, jpeg, png
    desconto      = Column(Numeric(5, 2), default=0)   # %
    ipi           = Column(Numeric(5, 2), default=0)   # %
    icms_entrada  = Column(Numeric(5, 2), default=0)   # % — informativo
    st            = Column(Numeric(5, 2), default=0)   # %
    # aguardando | processando | processado | erro
    status        = Column(String(20), default="aguardando")

    fornecedor = relationship("Fornecedor", back_populates="tabelas")
    produtos   = relationship("ProdutoTabela", back_populates="tabela", cascade="all, delete-orphan")


class ProdutoTabela(Base):
    __tablename__ = "produtos_tabela"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    tabela_id      = Column(Integer, ForeignKey("tabelas_preco.id", ondelete="CASCADE"), nullable=False)
    codigo         = Column(String(100))
    descricao      = Column(String(1000), nullable=False)
    descricao_completa = Column(Text)
    observacao         = Column(Text)
    ncm                = Column(String(20))
    unidade        = Column(String(20))
    preco_base          = Column(Numeric(12, 4))
    preco_desconto      = Column(Numeric(12, 4))
    preco_custo         = Column(Numeric(12, 4))
    ipi                 = Column(Numeric(5, 2))
    icms_entrada        = Column(Numeric(5, 2))
    st                  = Column(Numeric(5, 2))
    descricao_generica  = Column(String(300))
    url_produto         = Column(String(500))
    imagens             = Column(Text)   # JSON array de paths/URLs

    tabela = relationship("TabelaPreco", back_populates="produtos")


@contextmanager
def get_session():
    with Session(engine) as session:
        yield session


def init_db():
    Base.metadata.create_all(engine)
