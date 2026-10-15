from __future__ import annotations

import json
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from llm_memory.models import AgentSession, Handoff, utcnow
from llm_memory.schemas import AgentSessionCreate, HandoffCreate
from llm_memory.service import new_id


def session_dict(item: AgentSession) -> dict[str, object]:
    return {
        "id": item.id,
        "external_id": item.external_id,
        "owner_id": item.owner_id,
        "workspace_id": item.workspace_id,
        "project_id": item.project_id,
        "agent_id": item.agent_id,
        "title": item.title,
        "status": item.status,
        "started_at": item.started_at,
        "updated_at": item.updated_at,
        "ended_at": item.ended_at,
    }


def handoff_dict(item: Handoff) -> dict[str, object]:
    return {
        "id": item.id,
        "session_id": item.session_id,
        "owner_id": item.owner_id,
        "workspace_id": item.workspace_id,
        "project_id": item.project_id,
        "from_agent": item.from_agent,
        "to_agent": item.to_agent,
        "summary": item.summary,
        "decisions": json.loads(item.decisions),
        "blockers": json.loads(item.blockers),
        "next_steps": json.loads(item.next_steps),
        "status": item.status,
        "claimed_by_agent": item.claimed_by_agent,
        "claimed_at": item.claimed_at,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
    }


def create_session(session: Session, owner_id: str, data: AgentSessionCreate) -> AgentSession:
    if data.external_id:
        existing = session.scalar(select(AgentSession).where(
            AgentSession.owner_id == owner_id,
            AgentSession.external_id == data.external_id,
        ))
        if existing:
            if existing.status == "active":
                if (existing.workspace_id != data.workspace_id
                        or existing.project_id != data.project_id
                        or existing.agent_id != data.agent_id):
                    raise HTTPException(status_code=409, detail={
                        "code": "session_scope_mismatch", "message": "External session ID belongs to another scope"
                    })
                return existing
            raise HTTPException(status_code=409, detail={
                "code": "session_id_reused", "message": "External session ID has already been closed"
            })
    item = AgentSession(
        id=new_id(), owner_id=owner_id, external_id=data.external_id,
        workspace_id=data.workspace_id, project_id=data.project_id,
        agent_id=data.agent_id, title=data.title,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def create_handoff(
    session: Session, owner_id: str, session_id: str, data: HandoffCreate
) -> Handoff:
    agent_session = session.scalar(select(AgentSession).where(
        AgentSession.id == session_id,
        AgentSession.owner_id == owner_id,
    ))
    if agent_session is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "Session not found"})
    if agent_session.status != "active":
        raise HTTPException(status_code=409, detail={"code": "session_closed", "message": "Session is already closed"})
    now = utcnow()
    claimed = session.execute(
        update(AgentSession)
        .where(AgentSession.id == agent_session.id, AgentSession.status == "active")
        .values(status="handed_off", updated_at=now, ended_at=now)
    )
    if claimed.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail={"code": "session_closed", "message": "Session is already closed"})
    item = Handoff(
        id=new_id(), session_id=agent_session.id, owner_id=owner_id,
        workspace_id=agent_session.workspace_id, project_id=agent_session.project_id,
        from_agent=agent_session.agent_id, to_agent=data.to_agent,
        summary=data.summary.strip(), decisions=json.dumps(data.decisions, ensure_ascii=False),
        blockers=json.dumps(data.blockers, ensure_ascii=False),
        next_steps=json.dumps(data.next_steps, ensure_ascii=False),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def claim_next_handoff(
    session: Session, owner_id: str, workspace_id: str, project_id: str, agent_id: str
) -> Handoff:
    while True:
        candidate = session.scalar(
            select(Handoff)
            .where(
                Handoff.owner_id == owner_id,
                Handoff.workspace_id == workspace_id,
                Handoff.project_id == project_id,
                Handoff.status == "pending",
                (Handoff.to_agent.is_(None)) | (Handoff.to_agent == agent_id),
            )
            .order_by(Handoff.created_at, Handoff.id)
            .limit(1)
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail={
                "code": "no_pending_handoff", "message": "No pending handoff for this project"
            })
        now = utcnow()
        result = session.execute(
            update(Handoff)
            .where(Handoff.id == candidate.id, Handoff.status == "pending")
            .values(status="claimed", claimed_by_agent=agent_id, claimed_at=now)
        )
        session.commit()
        if result.rowcount == 1:
            session.refresh(candidate)
            return candidate


def complete_handoff(session: Session, owner_id: str, handoff_id: str, agent_id: str) -> Handoff:
    item = session.scalar(select(Handoff).where(
        Handoff.id == handoff_id,
        Handoff.owner_id == owner_id,
    ))
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "handoff_not_found", "message": "Handoff not found"})
    if item.status != "claimed" or item.claimed_by_agent != agent_id:
        raise HTTPException(status_code=409, detail={
            "code": "handoff_not_claimed", "message": "Handoff must be claimed by this agent first"
        })
    result = session.execute(
        update(Handoff)
        .where(Handoff.id == handoff_id, Handoff.status == "claimed", Handoff.claimed_by_agent == agent_id)
        .values(status="completed", completed_at=utcnow())
    )
    session.commit()
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail={"code": "handoff_state_changed", "message": "Handoff state changed"})
    session.refresh(item)
    return item
