from sqlalchemy import JSON, BigInteger, Column, DateTime, Numeric, String, func
from sqlalchemy.orm import declarative_base

# Standalone base — table is pre-created in MySQL by Laravel migrations.
# FastAPI does NOT auto-migrate this schema.
Base = declarative_base()


class AgentTrustAuditLog(Base):
    __tablename__ = "agent_score_audits"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    agent_id      = Column(BigInteger, nullable=False, index=True)
    old_score     = Column(Numeric(5, 2), nullable=True)
    new_score     = Column(Numeric(5, 2), nullable=True)
    score_delta   = Column(Numeric(5, 2), nullable=True)
    old_tier      = Column(String(20), nullable=True)
    new_tier      = Column(String(20), nullable=True)
    event_type    = Column(String(50), nullable=True)
    # 'metadata' is reserved by SQLAlchemy declarative API — map to MySQL column via name=
    audit_context = Column("metadata", JSON, nullable=True)
    created_at    = Column(DateTime, server_default=func.now(), nullable=True)

