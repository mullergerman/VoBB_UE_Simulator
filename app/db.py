"""Motor SQLite + seed inicial de 4 abonados."""
import os

from sqlmodel import Session, SQLModel, create_engine, select

from . import config
from .models import Abonado

os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _seed()


def _seed() -> None:
    """Crea 4 abonados por defecto (1001-1004) si la tabla está vacía."""
    with Session(engine) as session:
        existing = session.exec(select(Abonado)).first()
        if existing:
            return
        for i in range(1, 5):
            num = str(1000 + i)
            session.add(
                Abonado(
                    display_name=f"Abonado {num}",
                    line_number=num,
                    domain=config.LOCAL_DOMAIN,
                    pcscf_addr=config.LOCAL_REGISTRAR,
                    pcscf_port=5060,
                    transport="udp",
                    auth_user=num,
                    auth_password=f"pass{num}",
                    auth_realm="",   # vacío => comodín "*" (responde cualquier realm)
                    codec_pref="PCMU,PCMA",
                    alerting_delay_s=3,
                    echo_enabled=True,
                    reg_expires=600,
                )
            )
        session.commit()


def get_session() -> Session:
    return Session(engine)
