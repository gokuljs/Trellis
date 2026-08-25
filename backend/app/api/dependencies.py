from typing import Annotated

from fastapi import Depends, Request

from app.application.chat import ChatService
from app.infrastructure.database import Database
from app.infrastructure.secrets import SecretStore


def get_database(request: Request) -> Database:
    return request.app.state.database


DatabaseDep = Annotated[Database, Depends(get_database)]


def get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store


SecretStoreDep = Annotated[SecretStore, Depends(get_secret_store)]


def get_chat_service(request: Request) -> ChatService:
    return request.app.state.chat_service


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]
