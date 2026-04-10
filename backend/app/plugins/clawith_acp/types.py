from __future__ import annotations
from typing import Any, Dict, List, Optional, Union
from uuid import UUID
from pydantic import BaseModel, Field


# === Protocol Constants ===
PROTOCOL_VERSION = "1.0.0"


# === Capability Types ===
class PromptCapabilities(BaseModel):
    image: bool = True
    audio: bool = True
    embeddedContext: bool = True


class MCPCapabilities(BaseModel):
    http: bool = True
    sse: bool = True


class AgentCapabilities(BaseModel):
    loadSession: bool = True
    promptCapabilities: PromptCapabilities = Field(default_factory=PromptCapabilities)
    mcpCapabilities: MCPCapabilities = Field(default_factory=MCPCapabilities)


class AuthMethod(BaseModel):
    id: str
    name: str
    description: str
    _meta: Optional[Dict[str, Any]] = None


class AgentInfo(BaseModel):
    name: str = "clawith"
    title: str = "Clawith"
    version: str


# === Request/Response Types ===
class InitializeRequest(BaseModel):
    clientCapabilities: Dict[str, Any]
    protocolVersion: str


class InitializeResponse(BaseModel):
    protocolVersion: str
    authMethods: List[AuthMethod]
    agentInfo: AgentInfo
    agentCapabilities: AgentCapabilities


class AuthenticateRequest(BaseModel):
    methodId: str
    _meta: Optional[Dict[str, Any]] = None


class NewSessionRequest(BaseModel):
    cwd: str
    mcpServers: Optional[Dict[str, Any]] = None


class NewSessionResponse(BaseModel):
    sessionId: UUID
    availableModels: List[Dict[str, str]]
    currentModelId: str


class LoadSessionRequest(BaseModel):
    sessionId: UUID


class LoadSessionResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class PromptRequest(BaseModel):
    sessionId: UUID
    prompt: str
    images: Optional[List[str]] = None  # base64 encoded


class CancelRequest(BaseModel):
    sessionId: UUID


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[int, str]]
    method: str
    params: Optional[Dict[str, Any]] = None


class RequestError(Exception):
    """JSON-RPC error exception."""
    code: int
    message: str

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)
