"""SQLite database setup via SQLModel."""

from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = "sqlite:///data/rules.db"

engine = create_engine(DATABASE_URL, echo=False)


def create_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
