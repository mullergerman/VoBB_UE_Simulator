"""Motor SQLite + seed inicial (perfil + 4 abonados) + resolución de perfiles."""
import os

from sqlmodel import Session, SQLModel, create_engine, select

from . import config
from .models import SHARED_FIELDS, Abonado, Profile, User

os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate()
    _seed()
    _seed_admin()


def _migrate() -> None:
    """Agrega columnas nuevas a bases existentes (SQLite ADD COLUMN idempotente).

    create_all() no altera tablas ya creadas, así que las bases de datos previas
    (volumen productivo) no tendrían las columnas agregadas después. Aquí las
    añadimos sin perder los abonados existentes.
    """
    new_cols = {
        "registrar_uri": "VARCHAR DEFAULT ''",
        "profile_id": "INTEGER",
    }
    with engine.connect() as conn:
        existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(abonado)").fetchall()}
        for col, ddl in new_cols.items():
            if col not in existing:
                conn.exec_driver_sql(f"ALTER TABLE abonado ADD COLUMN {col} {ddl}")
        conn.commit()


def _seed() -> None:
    """Crea un perfil por defecto + 4 abonados (1001-1004) si no hay abonados."""
    with Session(engine) as session:
        if session.exec(select(Abonado)).first():
            return
        prof = Profile(
            name="Local (Kamailio)",
            domain=config.LOCAL_DOMAIN,
            pcscf_addr=config.LOCAL_REGISTRAR,
            pcscf_port=5060,
            transport="udp",
            auth_realm="",
            registrar_uri="",
            codec_pref="PCMU,PCMA",
            alerting_delay_s=3,
            echo_enabled=True,
            reg_expires=600,
        )
        session.add(prof)
        session.commit()
        session.refresh(prof)
        for i in range(1, 5):
            num = str(1000 + i)
            session.add(
                Abonado(
                    display_name=f"Abonado {num}",
                    line_number=num,
                    auth_user=num,
                    auth_password=f"pass{num}",
                    profile_id=prof.id,
                )
            )
        session.commit()


def _seed_admin() -> None:
    """Crea un usuario admin inicial (admin/admin) si no hay usuarios."""
    # Import local para evitar dependencia circular (auth importa db).
    from .auth import hash_password
    with Session(engine) as session:
        if session.exec(select(User)).first():
            return
        user = os.environ.get("ADMIN_USER", "admin")
        pwd = os.environ.get("ADMIN_PASSWORD", "admin")
        salt, h = hash_password(pwd)
        session.add(User(
            username=user, display_name="Administrador",
            password_salt=salt, password_hash=h, is_admin=True, enabled=True,
        ))
        session.commit()
        print(f"[seed] Usuario admin creado: '{user}' (CAMBIAR EL PASSWORD por defecto)", flush=True)


def resolve_abonado(ab: Abonado, session: Session) -> Abonado:
    """Devuelve una copia del abonado con los campos compartidos resueltos desde
    su perfil (si referencia uno). No persiste; es solo para el motor SIP.
    """
    if ab.profile_id is None:
        return ab
    prof = session.get(Profile, ab.profile_id)
    if prof is None:
        return ab
    data = ab.model_dump()
    for f in SHARED_FIELDS:
        data[f] = getattr(prof, f)
    return Abonado(**data)


def get_session() -> Session:
    return Session(engine)
