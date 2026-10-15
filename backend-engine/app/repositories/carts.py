import uuid

from sqlmodel import Session, select

from app.models import AgentCart


class CartRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, merchant_id: uuid.UUID, cart_id: uuid.UUID) -> AgentCart | None:
        return self.session.exec(
            select(AgentCart).where(AgentCart.id == cart_id, AgentCart.merchant_id == merchant_id)
        ).first()

    def save(self, cart: AgentCart) -> AgentCart:
        self.session.add(cart)
        self.session.commit()
        self.session.refresh(cart)
        return cart
