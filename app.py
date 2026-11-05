"""Quotex affiliate journal: backend API (v0).

Run:  uvicorn app:create_app --factory --reload
Env:  DATABASE_URL  default sqlite:///journal.db (use a postgresql+psycopg:// URL in production)
      JWT_SECRET    REQUIRED in production (a random one is generated per start otherwise)
      ADMIN_TOKEN   protects the referred-trader endpoint (disabled when unset)
      CORS_ORIGINS  comma-separated frontend origins
A user may log in freely, but only users whose Quotex trader ID is in `referred_traders` pass the
referral gate. The postback endpoint (later) and the admin endpoint below both fill that table.
"""
from __future__ import annotations

import datetime as dt
import io
import secrets

import bcrypt
import jwt
import os
import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Numeric, String, UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from importer import DEFAULT_TZ, daily_stats, parse_quotex_csv, summary
from fastapi.responses import FileResponse
from pathlib import Path

MAX_UPLOAD_BYTES = 5_000_000
Money = Numeric(14, 2, asdecimal=False)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    password_hash: Mapped[str] = mapped_column(String)
    quotex_trader_id: Mapped[str] = mapped_column(String, unique=True)
    timezone: Mapped[str] = mapped_column(String, default=DEFAULT_TZ)
    consented_at: Mapped[dt.datetime] = mapped_column(DateTime)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class ReferredTrader(Base):
    __tablename__ = "referred_traders"
    quotex_trader_id: Mapped[str] = mapped_column(String, primary_key=True)
    click_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (UniqueConstraint("user_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String)
    account_type: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String(3))
    asset: Mapped[str] = mapped_column(String)
    is_otc: Mapped[bool] = mapped_column(Boolean)
    direction: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Money)
    profit: Mapped[float] = mapped_column(Money)          # NET (return minus stake)
    result: Mapped[str] = mapped_column(String)
    payout_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime)   # stored as naive UTC
    closed_at: Mapped[dt.datetime] = mapped_column(DateTime)


class SignupIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    password: str = Field(min_length=8, max_length=72)
    quotex_trader_id: str = Field(pattern=r"^\d{4,15}$")     # provisional: IDs seen so far are numeric
    consent: bool


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class ReferredIn(BaseModel):
    trader_ids: list[str] = Field(max_length=5000)


def records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict("records")


def check_filters(account_type: str, currency: str) -> tuple[str, str]:
    if account_type not in ("live", "demo"):
        raise HTTPException(422, "account_type must be 'live' or 'demo'")
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(422, "currency must be a 3-letter code")
    return account_type, currency.upper()


def create_app(database_url: str | None = None) -> FastAPI:
    url = database_url or os.environ.get("DATABASE_URL", "sqlite:///journal.db")
    kw: dict = {}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw["poolclass"] = StaticPool
    engine = create_engine(url, **kw)
    Base.metadata.create_all(engine)
    make_session = sessionmaker(engine, expire_on_commit=False)
    secret = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
    admin_token = os.environ.get("ADMIN_TOKEN")

    app = FastAPI(title="Quotex affiliate journal")
    origins = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def get_db():
        with make_session() as db:
            yield db

    def token_for(user: User) -> str:
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)
        return jwt.encode({"sub": str(user.id), "exp": exp}, secret, algorithm="HS256")

    def is_verified(db: Session, user: User) -> bool:
        return db.get(ReferredTrader, user.quotex_trader_id) is not None

    bearer = HTTPBearer(auto_error=False)      # gives the /docs page an "Authorize" button

    def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                     db: Session = Depends(get_db)) -> User:
        if creds is None:
            raise HTTPException(401, "not_authenticated")
        try:
            uid = int(jwt.decode(creds.credentials, secret, algorithms=["HS256"])["sub"])
        except (jwt.PyJWTError, KeyError, ValueError):
            raise HTTPException(401, "invalid_token")
        user = db.get(User, uid)
        if user is None:
            raise HTTPException(401, "invalid_token")
        return user

    def verified_user(user: User = Depends(current_user), db: Session = Depends(get_db)) -> User:
        if not is_verified(db, user):
            raise HTTPException(403, "not_referred")      # trader ID not found under your affiliate link
        return user

    def load_trades(db: Session, user: User, account_type: str, currency: str) -> pd.DataFrame:
        rows = db.scalars(select(Trade).where(Trade.user_id == user.id, Trade.account_type == account_type,
                                              Trade.currency == currency)).all()
        cols = ["external_id", "asset", "is_otc", "direction", "amount", "profit", "result",
                "account_type", "currency", "opened_at", "closed_at"]
        df = pd.DataFrame([{c: getattr(t, c) for c in cols} for t in rows], columns=cols)
        for c in ("opened_at", "closed_at"):
            df[c] = pd.to_datetime(df[c], utc=True)
        return df
    

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(
            Path(__file__).parent / "static" / "dashboard.html"
        )
    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/signup", status_code=201)
    def signup(body: SignupIn, db: Session = Depends(get_db)):
        if not body.consent:
            raise HTTPException(422, "consent_required")
        pw = body.password.encode()
        if len(pw) > 72:
            raise HTTPException(422, "password_too_long")
        email = body.email.lower()
        taken = db.scalar(select(User).where((User.email == email) | (User.quotex_trader_id == body.quotex_trader_id)))
        if taken:
            raise HTTPException(409, "email_or_trader_id_already_registered")
        user = User(email=email, password_hash=bcrypt.hashpw(pw, bcrypt.gensalt()).decode(),
                    quotex_trader_id=body.quotex_trader_id, consented_at=utcnow())
        db.add(user)
        db.commit()
        return {"token": token_for(user), "verified": is_verified(db, user)}

    @app.post("/api/login")
    def login(body: LoginIn, db: Session = Depends(get_db)):
        user = db.scalar(select(User).where(User.email == body.email.lower()))
        if user is None or not bcrypt.checkpw(body.password.encode()[:72], user.password_hash.encode()):
            raise HTTPException(401, "invalid_credentials")
        return {"token": token_for(user), "verified": is_verified(db, user)}

    @app.get("/api/me")
    def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
        return {"email": user.email, "quotex_trader_id": user.quotex_trader_id,
                "timezone": user.timezone, "verified": is_verified(db, user)}

    @app.post("/api/uploads")
    async def upload(file: UploadFile = File(...), account_type: str = Form(...), currency: str = Form("INR"),
                     user: User = Depends(verified_user), db: Session = Depends(get_db)):
        account_type, currency = check_filters(account_type, currency)
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file_too_large")
        try:
            df, warnings = parse_quotex_csv(io.BytesIO(data), account_type, currency, user.timezone)
        except ValueError as e:                                # includes ImportProblem and CSV parse errors
            raise HTTPException(400, str(e) if type(e).__name__ == "ImportProblem" else "Could not read this file as a Quotex CSV export")
        have = set(db.scalars(select(Trade.external_id).where(Trade.user_id == user.id)))
        new = df[~df["external_id"].isin(have)].copy()
        for c in ("opened_at", "closed_at"):
            new[c] = new[c].dt.tz_localize(None)               # keep as naive UTC
        try:
            db.add_all([Trade(user_id=user.id, **r) for r in records(new)])
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "another_upload_in_progress")
        return {"trades_in_file": len(df), "new": len(new), "already_had": len(df) - len(new), "warnings": warnings}

    @app.get("/api/stats/daily")
    def stats_daily(account_type: str = "live", currency: str = "INR", start: str | None = None,
                    end: str | None = None, user: User = Depends(verified_user), db: Session = Depends(get_db)):
        account_type, currency = check_filters(account_type, currency)
        try:
            for d in (start, end):
                if d:
                    dt.date.fromisoformat(d)
        except ValueError:
            raise HTTPException(422, "start and end must be YYYY-MM-DD")
        df = load_trades(db, user, account_type, currency)
        if df.empty:
            return []
        out = daily_stats(df, user.timezone)
        out["day"] = out["day"].astype(str)
        if start:
            out = out[out["day"] >= start]
        if end:
            out = out[out["day"] <= end]
        return records(out.drop(columns=["account_type", "currency"]))

    @app.get("/api/stats/summary")
    def stats_summary(account_type: str = "live", currency: str = "INR",
                      user: User = Depends(verified_user), db: Session = Depends(get_db)):
        account_type, currency = check_filters(account_type, currency)
        df = load_trades(db, user, account_type, currency)
        return summary(df) if not df.empty else {"trades": 0}

    @app.get("/api/stats/pairs")
    def stats_pairs(account_type: str = "live", currency: str = "INR",
                    user: User = Depends(verified_user), db: Session = Depends(get_db)):
        """Return combined pair stats plus separate OTC and regular-market stats."""
        account_type, currency = check_filters(account_type, currency)
        df = load_trades(db, user, account_type, currency)
        if df.empty:
            return {"pairs": [], "market_breakdown": []}

        work = df.copy()
        work["asset"] = work["asset"].fillna("Unknown").astype(str).str.strip()
        work.loc[work["asset"] == "", "asset"] = "Unknown"
        work["profit"] = pd.to_numeric(work["profit"], errors="coerce").fillna(0.0)
        work["is_otc"] = work["is_otc"].fillna(False).astype(bool)

        def metrics(group: pd.DataFrame) -> dict:
            wins = int((group["profit"] > 0).sum())
            losses = int((group["profit"] < 0).sum())
            draws = int((group["profit"] == 0).sum())
            trades = int(len(group))
            pnl = round(float(group["profit"].sum()), 2)
            decided = wins + losses
            return {
                "pnl": pnl,
                "net_pnl": pnl,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "accuracy_pct": round(100.0 * wins / decided, 2) if decided else None,
                "avg_pnl": round(pnl / trades, 2) if trades else 0.0,
            }

        pairs = []
        market_breakdown = []
        for asset, group in work.groupby("asset", dropna=False, sort=False):
            combined = metrics(group)
            pairs.append({
                "pair": str(asset), "symbol": str(asset), "asset": str(asset),
                **combined,
            })
            for is_otc, market_name in ((True, "OTC"), (False, "Regular")):
                market_group = group[group["is_otc"] == is_otc]
                if market_group.empty:
                    continue
                market_breakdown.append({
                    "pair": str(asset),
                    "market": market_name,
                    **metrics(market_group),
                })

        pairs.sort(key=lambda row: row["pnl"], reverse=True)
        market_breakdown.sort(key=lambda row: (row["pair"], row["market"]))
        return {"pairs": pairs, "market_breakdown": market_breakdown}

    @app.post("/api/admin/referred-traders")
    def add_referred(body: ReferredIn, x_admin_token: str | None = Header(None), db: Session = Depends(get_db)):
        if not admin_token:
            raise HTTPException(503, "admin_disabled")
        if not x_admin_token or not secrets.compare_digest(x_admin_token.encode(), admin_token.encode()):
            raise HTTPException(401, "bad_admin_token")
        ids = {i.strip() for i in body.trader_ids if i.strip()}
        have = set(db.scalars(select(ReferredTrader.quotex_trader_id).where(ReferredTrader.quotex_trader_id.in_(ids))))
        db.add_all([ReferredTrader(quotex_trader_id=i) for i in ids - have])
        db.commit()
        return {"added": len(ids - have), "already_present": len(have)}

    return app
app = create_app()
